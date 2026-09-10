"""Read-only ECR evidence for containers referenced by one workflow.

No image publication, repository creation, authentication tokens, or policy writes.
Policy evidence is deliberately narrower than an IAM authorization simulation.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

from preflight import check

ALLOWED_ECR_METHODS = frozenset({"describe_images", "get_repository_policy",
                               "batch_get_image", "get_download_url_for_layer"})
_IMAGE = re.compile(r"(?P<account>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com(?:\.cn)?/(?P<image>[^\s]+)")
_PULL_ACTIONS = {"ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"}


class ECRMethodNotAllowed(RuntimeError):
    pass


class ECROperations:
    def __init__(self, *, _boto: Any):
        self._boto = _boto

    def call(self, method: str, **kwargs: Any) -> Any:
        if method not in ALLOWED_ECR_METHODS:
            raise ECRMethodNotAllowed(f"{method} is not in the read-only ECR allowlist.")
        return getattr(self._boto, method)(**kwargs)


def build_ecr_client(region: str | None = None, profile: str | None = None) -> Any:
    import boto3
    from botocore.config import Config
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client("ecr", region_name=region,
                          config=Config(retries={"mode": "adaptive", "max_attempts": 5}))


def exception_code(exc: Exception) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__))


def parse_image(uri: str) -> dict[str, Any]:
    match = _IMAGE.fullmatch(uri.removeprefix("docker://"))
    if not match:
        raise ValueError("Image must resolve to a private ECR URI.")
    value = match.group("image")
    if "@" in value:
        repository, digest = value.rsplit("@", 1)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("Invalid image digest.")
        image_id = {"imageDigest": digest}
    elif ":" in value:
        repository, tag = value.rsplit(":", 1)
        image_id = {"imageTag": tag}
    else:
        repository, image_id = value, {"imageTag": "latest"}
    if not re.fullmatch(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", repository):
        raise ValueError("Invalid ECR repository name.")
    return {"registryId": match.group("account"), "repositoryName": repository,
            "region": match.group("region"), "imageId": image_id}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def workflow_arn_parts(arn: str | None) -> tuple[str, str, str] | None:
    match = re.fullmatch(r"arn:(aws|aws-us-gov|aws-cn):omics:([a-z0-9-]+):(\d{12}):workflow/[0-9]+", arn or "")
    return match.groups() if match else None


def _scope_matches(condition: Any, source_arn: str | None, source_account: str | None) -> bool:
    if not isinstance(condition, dict):
        return False
    context = {"aws:sourcearn": source_arn if workflow_arn_parts(source_arn) else None,
               "aws:sourceaccount": source_account if re.fullmatch(r"\d{12}", source_account or "") else None}
    for operator, clauses in condition.items():
        if operator not in {"StringEquals", "ArnEquals", "StringLike", "ArnLike"} or not isinstance(clauses, dict) or not clauses:
            return False
        for key, values in clauses.items():
            actual = context.get(key.lower())
            patterns = _list(values)
            if not actual or not patterns or any(not isinstance(p, str) or "${" in p for p in patterns):
                return False
            # IAM wildcards are only '*' and '?', not shell character classes.
            if operator.endswith("Like"):
                matched = any(re.fullmatch(re.escape(p).replace(r"\*", ".*").replace(r"\?", "."), actual)
                              for p in patterns)
            else:
                matched = actual in patterns
            if not matched:
                return False
    return True


def policy_evidence(policy: dict[str, Any], *, source_arn: str | None = None,
                    source_account: str | None = None) -> dict[str, Any]:
    statements = _list(policy.get("Statement", []))
    granted: set[str] = set()
    for statement in statements:
        # Denies and alternative semantics remain outside this evidence subset.
        if not isinstance(statement, dict) or statement.get("Effect") == "Deny":
            return check("repository_policy", "UNKNOWN", "Policy contains a deny or unsupported statement.")
        if any(key in statement for key in ("NotPrincipal", "NotAction", "NotResource")):
            return check("repository_policy", "UNKNOWN", "Conditional policy requires environment review.")
        if "Condition" in statement and not _scope_matches(statement["Condition"], source_arn, source_account):
            return check("repository_policy", "UNKNOWN", "Policy scope is unsupported, mismatched or missing context; effective authorization is unknown.")
        principal = statement.get("Principal", {})
        if not isinstance(principal, dict) or "omics.amazonaws.com" not in _list(principal.get("Service", [])):
            continue
        if statement.get("Effect") != "Allow" or statement.get("Resource", "*") != "*":
            continue
        for action in _PULL_ACTIONS:
            if any(fnmatch.fnmatchcase(action.lower(), str(pattern).lower())
                   for pattern in _list(statement.get("Action", []))):
                granted.add(action)
    if granted == _PULL_ACTIONS:
        return check("repository_policy", "PASS", "Explicit HealthOmics pull grant with matching supported scope found; this is policy evidence, not effective authorization.")
    return check("repository_policy", "UNKNOWN", "No supported complete HealthOmics pull grant found; review the repository policy.")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_aws_metadata(url: str, *, limit: int = 2_000_000) -> bytes:
    """Bounded reads of AWS-generated metadata URLs; never log signed URLs."""
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.username or parsed.password or
            parsed.port not in {None, 443} or not (parsed.hostname or "").endswith(
                (".amazonaws.com", ".amazonaws.com.cn"))):
        raise ValueError("Unsupported metadata URL.")
    try:
        with build_opener(_NoRedirect()).open(url, timeout=30) as response:
            payload = response.read(limit + 1)
    except Exception:
        raise ValueError("AWS metadata download failed; signed URL omitted.") from None
    if len(payload) > limit:
        raise ValueError("AWS metadata exceeds inspection limit.")
    return payload


def inspect_image(*, client: Any, uri: str, region: str, source_arn: str | None = None,
                  source_account: str | None = None) -> list[dict[str, Any]]:
    try:
        ref = parse_image(uri)
    except ValueError as exc:
        return [check("image_reference", "UNKNOWN", f"{uri}: {exc}")]
    if ref["region"] != region:
        return [check("image_region", "FAIL", f"{uri}: repository region differs from run region.")]
    target = {k: ref[k] for k in ("registryId", "repositoryName")}
    try:
        response = client.call("describe_images", **target, imageIds=[ref["imageId"]])
        images = response.get("imageDetails", [])
        if not images:
            return [check("image_exists", "FAIL", f"{uri}: image not found.", code="ImageNotFoundException")]
        digest = images[0]["imageDigest"]
    except Exception as exc:
        code = exception_code(exc)
        status = "FAIL" if code in {"RepositoryNotFoundException", "ImageNotFoundException"} else "UNKNOWN"
        return [check("image_exists", status, f"{uri}: {code}", code=code)]
    checks = [check("image_exists", "PASS", f"{uri}: resolved {digest}")]
    try:
        raw = client.call("get_repository_policy", **target)
        checks.append(policy_evidence(json.loads(raw["policyText"]), source_arn=source_arn,
                                      source_account=source_account))
    except Exception as exc:
        code = exception_code(exc)
        checks.append(check("repository_policy", "FAIL" if code == "RepositoryPolicyNotFoundException" else "UNKNOWN",
                            f"{uri}: {code}", code=code))
    try:
        response = client.call("batch_get_image", **target, imageIds=[{"imageDigest": digest}])
        manifest = json.loads(response["images"][0]["imageManifest"])
        if "manifests" in manifest:
            platforms = [m.get("platform", {}) for m in manifest["manifests"]]
        else:
            config_digest = manifest["config"]["digest"]
            url = client.call("get_download_url_for_layer", **target, layerDigest=config_digest)["downloadUrl"]
            config_bytes = read_aws_metadata(url)
            if "sha256:" + hashlib.sha256(config_bytes).hexdigest() != config_digest:
                raise ValueError("Image config digest mismatch.")
            platforms = [json.loads(config_bytes)]
        supported = any(p.get("architecture") == "amd64" and p.get("os") == "linux" for p in platforms)
        known = bool(platforms) and all(p.get("architecture") and p.get("os") for p in platforms)
        state = "PASS" if supported else "FAIL" if known else "UNKNOWN"
        checks.append(check("image_architecture", state, f"{uri}: linux/amd64 {'found' if supported else 'not established'} in image metadata."))
    except Exception as exc:
        checks.append(check("image_architecture", "UNKNOWN", f"{uri}: metadata inspection unavailable ({exception_code(exc)})."))
    for item in checks:
        item["image_uri"] = uri
        item["image_digest"] = digest
    return checks
