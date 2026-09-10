#!/usr/bin/env python3
"""healthomics-bridge — submit, monitor and import AWS HealthOmics runs via boto3.

Talks to the HealthOmics API directly, behind an allow-listed client, a
fail-closed egress gate and a cost gate on submission. The allowlist is the
point: boto3 exposes all 107 ``omics`` operations, and an agent handed that
surface can delete a run, cancel in-flight work, or mutate shared account
configuration as easily as it can list runs. A narrow set of operations is reachable
here; destruction and shared-config mutation are barred by name and no flag
unlocks them.

Two things the raw SDK does not do for you, which this skill does:

* **Show the request before it costs anything.** ``--start-run`` builds the
  exact ``StartRun`` payload, prices it where AWS publishes a flat fee, and
  submits only behind a second explicit confirmation.
* **Derive the idempotency token.** ``StartRun`` requires ``requestId`` and AWS
  deduplicates submissions that reuse one. Deriving it from the request's own
  content means an accidentally repeated command is a no-op rather than a
  second charge.

Read-only live readiness inspects workflow containers and run-specific S3 paths.
Infrastructure provisioning, policy writes and performance analysis remain
outside the run lifecycle.

Offline demo (no AWS account, no credentials, no network, no boto3 needed):

    uv run python skills/healthomics-bridge/healthomics_bridge.py --demo --output /tmp/ho
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from clawbio.common.checksums import sha256_file  # noqa: E402
from clawbio.common.report import (  # noqa: E402
    generate_report_footer,
    generate_report_header,
    write_result_json,
)
from clawbio.common.textio import write_text_lf  # noqa: E402
from healthomics_pricing import estimated_cost_line  # noqa: E402
import error_codes as _error_codes  # noqa: E402
import params_template as _params_template  # noqa: E402
import preflight as _preflight  # noqa: E402
import recommendations as _recommendations  # noqa: E402
import readiness as _readiness  # noqa: E402
import ecr_client as _ecr  # noqa: E402
import registration as _registration  # noqa: E402
import s3_client as _s3  # noqa: E402
import submission as _submission  # noqa: E402
import output_manifest as _outputs  # noqa: E402
import observability as _observability  # noqa: E402
from omics_client import (  # noqa: E402
    ALLOWED_OPERATIONS,
    PERMANENTLY_EXCLUDED,
    OmicsCallError,
    OmicsClient,
    OperationNotAllowed,
    build_boto_client,
)

from contracts import SKILL_NAME, SKILL_VERSION
from reporting import _report_markdown, _LIST_MODES
from monitoring import fetch_run_bundle, wait_for_run, _enrich_failed_tasks

_SKILL_DIR = Path(__file__).resolve().parent
_REL_SCRIPT = Path("skills") / _SKILL_DIR.name / Path(__file__).name
_DEMO_BUNDLE = _SKILL_DIR / "tests" / "fixtures" / "demo_run_bundle.json"

DISCLAIMER_MARKER = "not a medical device"

_TASK_FIELDS = ["taskId", "name", "status", "cpus", "memory", "startTime", "stopTime"]
_MAX_TASKS_TO_ENRICH = 25


class EgressRefused(RuntimeError):
    """A submission was attempted without acknowledging that data leaves the machine."""


class OmicsOperations:
    """Allow-listed wrapper over a boto3 omics client.

    The allowlist is checked BEFORE dispatch, so a refused operation never
    reaches AWS even when the underlying client is live.
    """

    def __init__(self, *, _boto: Any) -> None:
        self._boto = _boto

    def call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        if operation not in ALLOWED_OPERATIONS:
            reason = PERMANENTLY_EXCLUDED.get(operation)
            if reason:
                raise OperationNotAllowed(f"{operation} is refused: {reason}.")
            raise OperationNotAllowed(
                f"{operation} is not in this skill's allowlist. healthomics-bridge "
                f"may call only: {', '.join(sorted(ALLOWED_OPERATIONS))}."
            )
        # botocore exposes operations as snake_case methods; the allowlist is
        # kept in the API's own PascalCase so it reads the same as AWS's docs.
        method = "".join(
            "_" + c.lower() if c.isupper() else c for c in operation
        ).lstrip("_")
        return getattr(self._boto, method)(**kwargs)


def collect_remote_paths(params: dict[str, Any], output_uri: str | None) -> list[str]:
    """Every URI in the submission that would move data off this machine."""
    remote: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str) and "://" in value:
            remote.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk({k: v for k, v in params.items() if not str(k).startswith("_")})
    if output_uri:
        remote.append(output_uri)
    return sorted(set(remote))


def check_remote_inputs(
    params: dict[str, Any], output_uri: str | None, acknowledged: bool
) -> None:
    """Fail closed unless the caller has acknowledged data egress.

    Mirrors the nf-core wrappers' --allow-remote-inputs contract: the gate
    records an acknowledgement, not a data location.
    """
    remote = collect_remote_paths(params, output_uri)
    if not remote:
        return
    if not acknowledged:
        raise EgressRefused(
            "This submission reads or writes paths outside this machine:\n  "
            + "\n  ".join(remote)
            + "\nRe-run with --allow-remote-inputs to acknowledge that genomic "
            "data will be handled by AWS HealthOmics."
        )
    print(
        f"WARNING: --allow-remote-inputs is set; {len(remote)} path(s) will be read "
        "or written by AWS HealthOmics, so genomic data leaves the local machine: "
        + ", ".join(remote),
        file=sys.stderr,
    )


def derive_request_id(
    *,
    workflow_id: str,
    workflow_type: str,
    params: dict[str, Any],
    output_uri: str,
    role_arn: str,
    run_name: str,
) -> str:
    """A stable idempotency token derived from the submission itself.

    ``StartRun`` requires ``requestId``; AWS deduplicates submissions that
    reuse one. Deriving the token from the request's own content means
    re-running the identical command is a no-op at AWS rather than a second
    billable run — and changing any part of the submission correctly yields a
    new token, so a genuine resubmission is never suppressed.
    """
    payload = json.dumps(
        {
            "workflow_id": workflow_id,
            "workflow_type": workflow_type,
            "params": params,
            "output_uri": output_uri,
            "role_arn": role_arn,
            "run_name": run_name,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def prepare_start_request(args: argparse.Namespace) -> dict[str, Any]:
    params = json.loads(Path(args.params).read_text(encoding="utf-8"))
    if not isinstance(params, dict):
        raise ValueError("Params JSON must be an object.")
    check_remote_inputs(params, args.output_uri, args.allow_remote_inputs)
    request = build_start_run_request(
        workflow_id=args.start_run, workflow_type=args.workflow_type,
        params=params, output_uri=args.output_uri, role_arn=args.role_arn,
        run_name=args.run_name, request_id="", storage_type=args.storage_type,
        storage_capacity=(normalise_storage_capacity(args.storage_capacity, announce=True)
                          if args.storage_capacity else None),
        cache_id=args.cache_id, cache_behavior=args.cache_behavior,
        run_group_id=args.run_group_id, workflow_version_name=args.workflow_version_name,
        tags=json.loads(args.run_tags) if args.run_tags else None,
    )
    canonical = {k: v for k, v in request.items() if k != "requestId"}
    request["requestId"] = hashlib.sha256(json.dumps(canonical, sort_keys=True,
                                                     separators=(",", ":")).encode()).hexdigest()[:32]
    return request


def build_start_run_request(
    *,
    workflow_id: str,
    workflow_type: str,
    params: dict[str, Any],
    output_uri: str,
    role_arn: str,
    run_name: str,
    request_id: str,
    storage_type: str | None = None,
    storage_capacity: int | None = None,
    cache_id: str | None = None,
    cache_behavior: str | None = None,
    run_group_id: str | None = None,
    workflow_version_name: str | None = None,
    tags: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble the exact StartRun payload, so it can be shown before it is sent.

    Keys are AWS's own camelCase field names, used directly — there is no
    translation layer, so a casing mismatch between this skill and the API
    cannot arise. Optional fields are omitted when unset so the
    API's own defaults apply (notably ``storageType``, which defaults to the
    preferred DYNAMIC).
    """
    request: dict[str, Any] = {
        "workflowId": workflow_id,
        "workflowType": workflow_type,
        "roleArn": role_arn,
        "name": run_name,
        "outputUri": output_uri,
        "parameters": {k: v for k, v in params.items() if not str(k).startswith("_")},
        "requestId": request_id,
    }
    optional = {
        "storageType": storage_type,
        "storageCapacity": storage_capacity,
        "cacheId": cache_id,
        "cacheBehavior": cache_behavior,
        "runGroupId": run_group_id,
        "workflowVersionName": workflow_version_name,
        "tags": tags,
    }
    request.update({k: v for k, v in optional.items() if v is not None})
    return request


_API_PAGE_MAX = 100  # AWS caps maxResults at 100 on ListRuns and ListWorkflows.


def list_all(
    *, client: OmicsClient, operation: str, limit: int, **filters: Any
) -> list[dict[str, Any]]:
    """Page through a listing until ``limit`` items or the results run out.

    AWS caps ``maxResults`` at 100 and returns a ``nextToken``. Sending
    ``maxResults=150`` does not fail — it silently returns 100, which is a
    wrong answer delivered confidently. Requesting more than the cap is also
    just rude to the API, so each page asks for at most what AWS allows.
    """
    items: list[dict[str, Any]] = []
    token: str | None = None
    while len(items) < limit:
        kwargs = dict(filters)
        kwargs["maxResults"] = min(_API_PAGE_MAX, limit - len(items))
        if token:
            kwargs["startingToken"] = token
        response = client.call(operation, **kwargs)
        page = list(response.get("items", []))
        items.extend(page)
        token = response.get("nextToken")
        if not token or not page:
            break
    return items[:limit]


def normalise_storage_capacity(requested: int, *, announce: bool = False) -> int:
    """Round a STATIC capacity up to what AWS will actually allocate and bill.

    The rule is 1,200 GiB **or a multiple of 2,400 GiB** — not 1,200 chunks,
    which is the intuitive-but-wrong reading. AWS's own worked examples:
    5,000 rounds to 7,200 and 42,000 rounds to 43,200.

    Rounding rather than refusing, because AWS rounds anyway; the value this
    adds is telling the user before the invoice does.
    """
    if requested <= 1_200:
        allocated = 1_200
    else:
        allocated = 2_400 * math.ceil(requested / 2_400)
    if announce and allocated != requested:
        print(
            f"NOTE: --storage-capacity {requested} GiB is not an allocatable size. "
            f"AWS allocates and bills STATIC storage as 1,200 GiB or a multiple of "
            f"2,400 GiB, so this run will use {allocated} GiB.",
            file=sys.stderr,
        )
    return allocated


def run_output_prefix(output_uri: str, run_id: str) -> str:
    """The S3 prefix holding exactly this run's outputs.

    HealthOmics writes each run under ``<outputUri>/<runId>/``. Pointing at
    ``<outputUri>`` alone reaches every run the account ever wrote — the same
    rule the report's fetch command already encodes.
    """
    return f"{output_uri.rstrip('/')}/{str(run_id).strip()}/"


def upload_run_inputs(
    *, client: Any, sources: list[Path], destination: str,
    acknowledged: bool, confirmed: bool,
) -> dict[str, Any]:
    """Put a run's inputs in S3, behind the same two gates as a submission.

    ``acknowledged`` records that the user knows data leaves the machine;
    ``confirmed`` is the separate decision to actually transfer. Uploading puts
    a genome somewhere it was not before, which is exactly the consequence
    ``--allow-remote-inputs`` exists to make visible.
    """
    resolved = [Path(s).expanduser() for s in sources]
    if not acknowledged:
        raise EgressRefused(
            "This upload would copy local files to S3:\n  "
            + "\n  ".join(str(p) for p in resolved)
            + f"\n  -> {destination}"
            + "\nRe-run with --allow-remote-inputs to acknowledge that genomic "
            "data will leave this machine."
        )
    print(
        f"WARNING: --allow-remote-inputs is set; {len(resolved)} local file(s) "
        f"will be copied to {destination}, so genomic data leaves the local machine.",
        file=sys.stderr,
    )
    if not confirmed:
        print(
            "ESTIMATE ONLY: nothing was uploaded. Re-run with --confirm-upload "
            "to transfer.",
            file=sys.stderr,
        )
        return {
            "mode": "upload", "uploaded": False, "destination": destination,
            "sources": [str(p) for p in resolved], "uris": [], "n_uploaded": 0,
        }

    result = _s3.upload_files(client=client, sources=resolved, destination=destination)
    result.update({"mode": "upload", "uploaded": True,
                   "sources": [str(p) for p in resolved]})
    return result


def download_run_outputs(
    *, client: Any, output_uri: str, run_id: str, destination: Path,
    confirmed: bool, run_output_uri: str | None = None,
) -> dict[str, Any]:
    """Bring one run's outputs back to this machine.

    One gate, not two. There is no egress acknowledgement because this moves
    data *toward* the user; gating both directions would make the flag
    reflexive, and a flag passed on every command stops carrying meaning on the
    command where it matters.
    """
    prefix_uri = run_output_uri.rstrip("/") + "/" if run_output_uri else run_output_prefix(output_uri, run_id)
    objects = _s3.list_objects(client=client, uri=prefix_uri)
    bucket, key_prefix = _s3.parse_s3_uri(prefix_uri)

    if not confirmed:
        total = sum(o["size"] for o in objects)
        print(
            f"ESTIMATE ONLY: {len(objects)} object(s), {total:,} bytes under "
            f"{prefix_uri}. Nothing was downloaded. Re-run with "
            f"--confirm-download to transfer (S3 egress is billable).",
            file=sys.stderr,
        )
        return {
            "mode": "download", "downloaded": False, "source": prefix_uri,
            "n_objects": len(objects), "n_bytes": total, "n_downloaded": 0,
            "objects": objects,
        }

    transferred = _s3.download_objects(
        client=client, bucket=bucket, objects=objects,
        key_prefix=key_prefix, destination=Path(destination))
    transferred.update({"mode": "download", "downloaded": True,
                        "source": prefix_uri, "n_objects": len(objects)})
    return transferred


def verify_run_outputs(
    *, client: Any, output_uri: str, run_id: str, depth: str,
    destination: Path | None, confirmed: bool, run_output_uri: str | None = None,
) -> dict[str, Any]:
    """Say what a run actually produced, at one of two depths.

    ``manifest`` lists the run's output prefix and records each object's size
    and ETag. It moves no bytes and costs nothing.

    ``deep`` additionally downloads each object and computes a real SHA-256.
    That is a genuine checksum the repro bundle can stand behind, and it is the
    only mode that can prove an output actually exists — which matters because
    ``write_checksums`` silently skips paths that are missing and so cannot
    detect an output that never landed.
    """
    prefix_uri = run_output_uri.rstrip("/") + "/" if run_output_uri else run_output_prefix(output_uri, run_id)
    listed = _s3.list_objects(client=client, uri=prefix_uri)

    objects: list[dict[str, Any]] = []
    for item in listed:
        etag = _s3.describe_etag(item.get("etag", ""))
        objects.append({**item, **etag, "sha256": None})

    result: dict[str, Any] = {
        "depth": "manifest", "source": prefix_uri, "objects": objects,
        "n_objects": len(objects), "n_bytes": sum(o["size"] for o in objects),
        "n_missing": 0, "complete": True, "downloaded_to": None,
    }
    if depth != "deep":
        return result
    if not confirmed:
        raise EgressRefused("Deep verification requires --confirm-download")

    bucket, key_prefix = _s3.parse_s3_uri(prefix_uri)
    target = Path(destination) if destination else Path.cwd() / f"run-{run_id}"
    transferred = _s3.download_objects(
        client=client, bucket=bucket, objects=listed,
        key_prefix=key_prefix, destination=target)

    by_key = {d["key"]: d["path"] for d in transferred["downloaded_files"]}
    for entry in objects:
        path = by_key.get(entry["key"])
        if path and Path(path).is_file():
            entry["sha256"] = sha256_file(path)
            entry["local_path"] = path

    missing = [o["key"] for o in objects if not o["sha256"]]
    result.update({
        "depth": "deep",
        "downloaded_to": str(target),
        "n_missing": len(missing),
        "missing": missing,
        "complete": not missing,
        "failures": transferred["failures"],
    })
    return result


def register_run_workflow(
    *, client: OmicsClient, definition: Path, additional_files: list[Path],
    name: str, engine: str | None, description: str | None,
    parameter_template: dict[str, Any] | None, allow_duplicate: bool,
    confirmed: bool, output_dir: Path,
) -> dict[str, Any]:
    """Register a definition as a private workflow, gated by --confirm-register.

    The archive is built and checksummed even on a dry run: those bytes are the
    one piece of this operation the skill can pin honestly, having produced
    them itself, and seeing the digest before creating anything is the point of
    a dry run.
    """
    definition = Path(definition).expanduser().resolve()
    resolved_engine = _registration.resolve_engine(definition, engine)
    members = _registration.resolve_zip_members(definition, list(additional_files))
    manifest = _registration.build_definition_zip(
        members=members, destination=Path(output_dir) / "tables" / "workflow.zip")

    collisions = _registration.assert_workflow_name_is_free(
        client=client, name=name, allow_duplicate=allow_duplicate)

    request_id = hashlib.sha256(
        f"{name}:{resolved_engine}:{manifest['sha256']}".encode("utf-8")
    ).hexdigest()[:32]
    request = _registration.build_create_workflow_request(
        name=name, engine=resolved_engine, zip_path=Path(manifest["path"]),
        request_id=request_id, description=description,
        parameter_template=parameter_template)

    base: dict[str, Any] = {
        "mode": "register", "workflow_name": name, "engine": resolved_engine,
        "definition_path": str(definition), "zip": manifest,
        "name_collisions": collisions, "registered": False,
        "workflow_id": None, "workflow_status": None,
        "workflow_status_message": None,
    }

    if not confirmed:
        print(
            "DRY RUN: no workflow was created. Re-run with --confirm-register "
            "to create it.",
            file=sys.stderr,
        )
        return base

    created = _registration.register_workflow(client=client, request=request)
    workflow_id = str(created.get("id") or created.get("workflowId") or "")
    workflow = _registration.poll_workflow_until_settled(
        client=client, workflow_id=workflow_id)
    base.update({
        "registered": True, "workflow_id": workflow_id,
        "workflow_status": str(workflow.get("status", "")).upper(),
        # AWS's own explanation, not a guess. The same field this skill
        # already reads for a failed run and a failed task -- registration
        # discarded it after polling until a live FAILED registration exposed
        # that the report was printing a generic hint instead.
        "workflow_status_message": workflow.get("statusMessage"),
    })
    return base


def register_workflow_version(
    *, client: OmicsClient, workflow_id: str, definition: Path,
    additional_files: list[Path], version_name: str, description: str | None,
    parameter_template: dict[str, Any] | None, confirmed: bool, output_dir: Path,
) -> dict[str, Any]:
    """Add a version to an EXISTING workflow, rather than creating a new one.

    --start-run has always accepted --workflow-version-name; there was no path
    that could ever create the version it names. Reuses the same reproducible
    archive machinery as --register -- the same honesty about what the digest
    proves, and the same inability to lint before AWS validates server-side.
    """
    definition = Path(definition).expanduser().resolve()
    resolved_engine = _registration.resolve_engine(definition, None)
    members = _registration.resolve_zip_members(definition, list(additional_files))
    manifest = _registration.build_definition_zip(
        members=members,
        destination=Path(output_dir) / "tables" / f"workflow-version-{version_name}.zip")

    request_id = hashlib.sha256(
        f"{workflow_id}:{version_name}:{manifest['sha256']}".encode("utf-8")
    ).hexdigest()[:32]
    request: dict[str, Any] = {
        "workflowId": workflow_id, "versionName": version_name,
        "definitionZip": Path(manifest["path"]).read_bytes(), "requestId": request_id,
    }
    if description:
        request["description"] = description
    if parameter_template:
        request["parameterTemplate"] = parameter_template

    base: dict[str, Any] = {
        "mode": "register-version", "workflow_id": workflow_id,
        "version_name": version_name, "engine": resolved_engine,
        "definition_path": str(definition), "zip": manifest,
        "registered": False, "version_status": None, "version_status_message": None,
    }
    if not confirmed:
        print(
            "DRY RUN: no workflow version was created. Re-run with "
            "--confirm-register to create it.",
            file=sys.stderr,
        )
        return base

    client.call("CreateWorkflowVersion", **request)
    version = client.call(
        "GetWorkflowVersion", workflowId=workflow_id, versionName=version_name)
    base.update({
        "registered": True,
        "version_status": str(version.get("status", "")).upper(),
        "version_status_message": version.get("statusMessage"),
    })
    return base


def describe_run_group(*, client: OmicsClient, group_id: str) -> dict[str, Any]:
    """One run group's own detail -- concurrency and cost limits --
    so --start-run --run-group-id is not a blind reference to an id you
    listed but never actually looked at."""
    return client.call("GetRunGroup", id=str(group_id))


def describe_run_cache(*, client: OmicsClient, cache_id: str) -> dict[str, Any]:
    """One run cache's own detail, for the same reason as describe_run_group."""
    return client.call("GetRunCache", id=str(cache_id))


def tag_run(*, client: OmicsClient, run_id: str, tags: dict[str, str]) -> dict[str, Any]:
    """Set tags on a run after submission.

    ListTagsForResource had no write-side pair: tags were settable only at
    --start-run time, with no way back to correct or add them afterward.
    """
    run = client.call("GetRun", id=str(run_id))
    arn = run.get("arn")
    if not arn:
        raise OmicsCallError(f"Run {run_id} has no arn to tag.")
    client.call("TagResource", resourceArn=str(arn), tags=tags)
    return {"mode": "tag", "run_id": run_id, "tagged": tags, "arn": arn}


def list_run_tags(*, client: OmicsClient, run_id: str) -> dict[str, Any]:
    """Read one run's tags through the same resource ARN AWS mutates."""
    run = client.call("GetRun", id=str(run_id))
    arn = run.get("arn")
    if not arn:
        raise OmicsCallError(f"Run {run_id} has no arn to list tags.")
    tags = dict(client.call("ListTagsForResource", resourceArn=str(arn)).get("tags", {}))
    return {"mode": "tags", "run_id": run_id, "tags": tags, "arn": arn}


def sync_run_tags(
    *, client: OmicsClient, run_id: str, desired_tags: dict[str, str]
) -> dict[str, Any]:
    """Converge a run's tags to a desired JSON object.

    This is intentionally narrow: it reads current run tags, sets changed keys,
    and removes keys absent from the desired set. It never touches account-level
    tagging configuration.
    """
    run = client.call("GetRun", id=str(run_id))
    arn = run.get("arn")
    if not arn:
        raise OmicsCallError(f"Run {run_id} has no arn to sync tags.")
    current = dict(client.call("ListTagsForResource", resourceArn=str(arn)).get("tags", {}))
    to_set = {k: v for k, v in desired_tags.items() if current.get(k) != v}
    to_remove = sorted(set(current) - set(desired_tags))
    if to_set:
        client.call("TagResource", resourceArn=str(arn), tags=to_set)
    if to_remove:
        client.call("UntagResource", resourceArn=str(arn), tagKeys=to_remove)
    return {
        "mode": "sync-tags",
        "run_id": run_id,
        "arn": arn,
        "previous_tags": current,
        "desired_tags": desired_tags,
        "set": to_set,
        "removed": to_remove,
        "tags": desired_tags,
    }


def untag_run(*, client: OmicsClient, run_id: str, keys: list[str]) -> dict[str, Any]:
    """Remove tags from a run by key."""
    run = client.call("GetRun", id=str(run_id))
    arn = run.get("arn")
    if not arn:
        raise OmicsCallError(f"Run {run_id} has no arn to untag.")
    client.call("UntagResource", resourceArn=str(arn), tagKeys=keys)
    return {"mode": "untag", "run_id": run_id, "untagged": keys, "arn": arn}


def submit_run(
    *, client: OmicsClient, request: dict[str, Any], confirmed: bool
) -> dict[str, Any]:
    """Submit a run, but only past the cost gate.

    Without ``confirmed`` this returns the request untouched and calls nothing
    — the estimate-first contract, and the reason an
    unconfirmed --start-run bills nothing.
    """
    if not confirmed:
        return {"submitted": False, "request": request, "response": {}}
    response = client.call("StartRun", **request)
    return {"submitted": True, "request": request, "response": response}


def map_run_report(
    bundle: dict[str, Any],
    *,
    region: str,
    start_run_request: dict[str, Any] | None = None,
    submitted: bool = False,
    demo: bool = False,
    verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Turn API payloads into this skill's own reported shape."""
    run = bundle.get("run") or {}
    workflow = bundle.get("workflow") or {}
    tasks = list(bundle.get("tasks") or [])

    failed = [t for t in tasks if str(t.get("status", "")).upper() in {"FAILED", "CANCELLED"}]
    completed = [t for t in tasks if str(t.get("status", "")).upper() == "COMPLETED"]

    return {
        "mode": "run",
        "region": region,
        "transport": "boto3",
        "run": run,
        "workflow": workflow,
        "tasks": tasks,
        "tags": dict(bundle.get("tags") or {}),
        "verification": verification,
        "n_tasks": len(tasks),
        "n_completed": len(completed),
        "n_failed": len(failed),
        "run_status": run.get("status", "UNKNOWN"),
        "submitted": submitted,
        "start_run_request": start_run_request,
        "demo": demo,
    }


def map_list_report(
    items: list[dict[str, Any]], *, kind: str, region: str, demo: bool = False
) -> dict[str, Any]:
    """Turn a --list-runs / --list-workflows result into the reported shape."""
    return {
        "mode": kind,
        "region": region,
        "transport": "boto3",
        "items": items,
        "n_items": len(items),
        "run": {}, "workflow": {}, "tasks": [], "tags": {}, "verification": None,
        "run_status": "N/A",
        "n_tasks": 0, "n_completed": 0, "n_failed": 0,
        "submitted": False, "start_run_request": None, "demo": demo,
    }


def map_check_report(preflight_result: dict[str, Any], *, region: str) -> dict[str, Any]:
    """Turn preflight checks into the shared reported shape."""
    data = _pad_transfer_report(
        {
            **preflight_result,
            "region": region,
            "items": list(preflight_result.get("checks") or []),
            "n_items": int(preflight_result.get("n_checks", 0)),
        },
        region,
    )
    data["mode"] = "check"
    return data


def map_workflow_search_report(
    *, query: str, items: list[dict[str, Any]], kind: str, region: str
) -> dict[str, Any]:
    """Search/recommendation reports are workflow listings with scores."""
    data = map_list_report(items, kind=kind, region=region)
    data["query"] = query
    return data


def map_params_template_report(payload: dict[str, Any], *, region: str) -> dict[str, Any]:
    """Parameter-template report shape."""
    params = payload.get("params") or {}
    template = payload.get("parameter_template") or {}
    rows = [
        {
            "name": name,
            "default": json.dumps(default, sort_keys=True),
            "template": json.dumps(template.get(name, {}), sort_keys=True),
        }
        for name, default in sorted(params.items())
    ]
    return _pad_transfer_report(
        {
            "mode": "params-template",
            "workflow_id": payload.get("workflow_id"),
            "workflow_name": payload.get("workflow_name"),
            "workflow_type": payload.get("workflow_type"),
            "parameter_template": template,
            "params_template": params,
            "items": rows,
            "n_items": len(rows),
        },
        region,
    )


# Per-entity table columns. A listing and a run report describe different
# things, so they get different tables rather than one shape pretending to fit
# both -- see _write_table.
_RUN_FIELDS = ("id", "name", "status", "workflowId", "creationTime", "stopTime")
_WORKFLOW_FIELDS = ("id", "name", "status", "type", "creationTime")
_WORKFLOW_SEARCH_FIELDS = ("id", "name", "status", "type", "matchScore", "creationTime")
_RUN_GROUP_FIELDS = ("id", "name", "maxCpus", "maxRuns", "maxDuration")
_RUN_CACHE_FIELDS = ("id", "name", "status", "cacheS3Uri")
_WORKFLOW_VERSION_FIELDS = ("workflowId", "versionName", "status", "creationTime")
_CHECK_FIELDS = ("name", "status", "ok", "severity", "detail", "code")
_PARAMETER_FIELDS = ("name", "default", "template")
_TAG_FIELDS = ("key", "value")


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({f: row.get(f, "") for f in fields})
    return path


def _write_tasks_csv(output_dir: Path, tasks: list[dict[str, Any]]) -> Path:
    return _write_csv(output_dir / "tables" / "tasks.csv", _TASK_FIELDS, tasks)


def _pad_transfer_report(data: dict[str, Any], region: str) -> dict[str, Any]:
    """Fit an upload / download / register result into the shared report shape.

    One ``write_bundle`` handles every mode, so each mode fills the same keys
    rather than growing a parallel writer per operation.
    """
    padded = {
        "region": region, "transport": "boto3", "demo": False,
        "run": {}, "workflow": {}, "tasks": [], "tags": {}, "items": [],
        "n_items": 0, "run_status": "N/A", "n_tasks": 0, "n_completed": 0,
        "n_failed": 0, "submitted": False, "start_run_request": None,
        "verification": None,
    }
    padded.update(data)
    return padded


_OUTPUT_FIELDS = ("key", "size", "etag", "is_md5", "sha256", "last_modified")
_UPLOAD_FIELDS = ("source", "key", "uri", "n_bytes")
_ZIP_MEMBER_FIELDS = ("archive_name", "source_path", "n_bytes", "sha256")


def _write_table(output_dir: Path, data: dict[str, Any]) -> Path:
    """Write the table for whatever this report is actually about.

    A --list-workflows bundle used to carry an empty tasks.csv: run-task headers
    over zero rows, while the workflows it had just fetched appeared only in
    report.md. That is worse than omitting the file, because a downstream reader
    sees a well-formed header and concludes the query returned nothing.
    """
    mode = data.get("mode")
    if mode == "runs":
        return _write_csv(output_dir / "tables" / "runs.csv", _RUN_FIELDS, data["items"])
    if mode == "workflows":
        return _write_csv(
            output_dir / "tables" / "workflows.csv", _WORKFLOW_FIELDS, data["items"]
        )
    if mode in {"workflow-search", "workflow-recommendations"}:
        return _write_csv(
            output_dir / "tables" / "workflows.csv", _WORKFLOW_SEARCH_FIELDS,
            data["items"],
        )
    if mode == "run-groups":
        return _write_csv(output_dir / "tables" / "run-groups.csv",
                          _RUN_GROUP_FIELDS, data["items"])
    if mode == "run-caches":
        return _write_csv(output_dir / "tables" / "run-caches.csv",
                          _RUN_CACHE_FIELDS, data["items"])
    if mode == "workflow-versions":
        return _write_csv(output_dir / "tables" / "workflow-versions.csv",
                          _WORKFLOW_VERSION_FIELDS, data["items"])
    if mode == "upload":
        return _write_csv(output_dir / "tables" / "uploads.csv", _UPLOAD_FIELDS,
                          data.get("uploaded_files", []))
    if mode == "download":
        return _write_csv(output_dir / "tables" / "downloads.csv",
                          ("key", "path", "etag"), data.get("downloaded_files", []))
    if mode in {"register", "register-version"}:
        return _write_csv(
            output_dir / "tables" / "definition.csv", _ZIP_MEMBER_FIELDS,
            (data.get("zip") or {}).get("members", []),
        )
    if mode == "check":
        return _write_csv(output_dir / "tables" / "checks.csv", _CHECK_FIELDS,
                          data.get("checks", []))
    if mode == "params-template":
        return _write_csv(output_dir / "tables" / "params-template.csv",
                          _PARAMETER_FIELDS, data.get("items", []))
    if mode in {"tag", "untag", "tags", "sync-tags"}:
        tags = data.get("tags") or data.get("desired_tags") or data.get("tagged") or {}
        rows = [{"key": key, "value": value} for key, value in sorted(tags.items())]
        if mode == "untag":
            rows = [{"key": key, "value": ""} for key in data.get("untagged", [])]
        return _write_csv(output_dir / "tables" / "tags.csv", _TAG_FIELDS, rows)
    if data.get("verification"):
        return _write_csv(
            output_dir / "tables" / "outputs.csv", _OUTPUT_FIELDS,
            data["verification"]["objects"],
        )
    return _write_tasks_csv(output_dir, data["tasks"])


def _warn_before_overwrite(output_dir: Path) -> None:
    if output_dir.exists() and any(output_dir.iterdir()):
        print(
            f"WARNING: {output_dir} already exists and is not empty; files may be overwritten.",
            file=sys.stderr,
        )


def _replay_args(mode: str | None, data: dict[str, Any]) -> list[Any]:
    """The flags that reproduce this bundle's own mode.

    Every non-run mode used to fall through to ``["--run-status", ""]`` --
    upload, download, register, and the newer tag/untag/register-version modes
    all replay a run-status query for a run that was never named. Real
    bundles from this session (an upload, a registration) both shipped that
    broken command. One switch per mode, so a new mode that forgets to extend
    this fails loudly (KeyError) rather than silently inheriting the wrong
    replay.
    """
    if data.get("invocation"):
        return list(data["invocation"])
    if mode in _LIST_MODES:
        flags = {
            "runs": ["--list-runs"],
            "workflows": ["--list-workflows"],
            "run-groups": ["--list-run-groups"],
            "run-caches": ["--list-run-caches"],
            "workflow-versions": [
                "--list-workflow-versions",
                data.get("workflow_id")
                or (data.get("items") or [{}])[0].get("workflowId")
                or "",
            ],
            "workflow-search": ["--search-workflows", str(data.get("query", ""))],
            "workflow-recommendations": [
                "--recommend-workflow",
                str(data.get("query", "")),
            ],
        }[mode]
        return flags
    if mode == "check":
        return ["--check"]
    if mode == "params-template":
        return [
            "--params-template",
            str(data.get("workflow_id", "")),
            *_workflow_type_arg(data),
        ]
    if mode == "upload":
        return ["--upload-inputs", *data.get("sources", []), "--to", data["destination"]]
    if mode == "download":
        return ["--download-outputs", str(data.get("run_id", "")), "--to", data["destination"]]
    if mode == "register":
        return ["--register", data["definition_path"], "--workflow-name", data["workflow_name"]]
    if mode == "register-version":
        return ["--register", data["definition_path"], "--workflow-id", data["workflow_id"],
                "--new-version-name", data["version_name"]]
    if mode == "tag":
        return ["--tag-run", str(data["run_id"]), "--tags",
                json.dumps(data.get("tagged", {}), sort_keys=True)]
    if mode == "untag":
        return ["--untag-run", str(data["run_id"]), "--tag-keys",
                *list(data.get("untagged", []))]
    if mode == "tags":
        return ["--list-tags", str(data["run_id"])]
    if mode == "sync-tags":
        return ["--sync-tags", str(data["run_id"]), "--tags",
                json.dumps(data.get("desired_tags", {}), sort_keys=True)]
    return ["--run-status", str(data["run"].get("id", ""))]


def _workflow_type_arg(data: dict[str, Any]) -> list[str]:
    workflow_type = data.get("workflow_type") or data.get("workflow", {}).get("type")
    return ["--workflow-type", str(workflow_type)] if workflow_type else []


def _replay_preflight_lines(data: dict[str, Any]) -> list[str]:
    """Minimal replay guard shipped in commands.sh."""
    mode = data.get("mode") or "run"
    return [
        'MANIFEST="$OUTPUT_DIR/reproducibility/replay_manifest.json"',
        'if [ ! -f "$MANIFEST" ]; then',
        '  echo "Missing replay manifest: $MANIFEST" >&2',
        "  exit 1",
        "fi",
        f"if ! grep -q '\"mode\": \"{mode}\"' \"$MANIFEST\"; then",
        f'  echo "Replay manifest does not describe mode {mode}" >&2',
        "  exit 1",
        "fi",
    ]


def _handoff_for_path(path: str) -> list[str]:
    lower = path.lower()
    if lower.endswith((".vcf", ".vcf.gz", ".bcf")):
        return ["variant-annotation", "vcf-annotator", "pharmgx-reporter"]
    if lower.endswith((".h5ad", ".loom", ".mtx", ".mtx.gz")):
        return ["scrna-orchestrator", "scrna-embedding"]
    if lower.endswith((".counts.tsv", ".counts.csv", ".tsv", ".csv")):
        return ["rnaseq-de", "proteomics-de"]
    if lower.endswith((".bam", ".cram")):
        return ["seq-wrangler", "multiqc-reporter"]
    if lower.endswith((".html", ".zip")):
        return ["multiqc-reporter"]
    return []


def _write_extra_artifacts(output_dir: Path, data: dict[str, Any]) -> list[Path]:
    """Write richer machine-readable artifacts beside the standard contract."""
    written: list[Path] = []
    repro_dir = output_dir / "reproducibility"
    repro_dir.mkdir(parents=True, exist_ok=True)

    readiness = data.get("readiness") or (data if data.get("mode") == "check" else {})
    if readiness.get("environment"):
        path = output_dir / "environment.json"
        write_text_lf(path, json.dumps(readiness["environment"], indent=2, default=str) + "\n")
        written.append(path)
    for name in ("logs", "analysis", "smoke_validation"):
        if data.get(name) is not None:
            path = output_dir / f"{name}.json"
            _atomic_json(path, data[name])
            written.append(path)
    for filename in ("submission.json", "readiness.json"):
        path = output_dir / filename
        if path.exists():
            written.append(path)

    manifest = {
        "mode": data.get("mode") or "run",
        "region": data.get("region"),
        "run_id": data.get("run", {}).get("id") or data.get("run_id"),
        "workflow_id": data.get("workflow_id") or data.get("run", {}).get("workflowId"),
        "workflow_version_name": data.get("version_name") or data.get("workflow_version_name") or data.get("run", {}).get("workflowVersionName"),
        "workflow_type": data.get("workflow_type") or data.get("run", {}).get("workflowType"),
        "definition_digest": data.get("run", {}).get("digest") or data.get("workflow", {}).get("digest"),
        "resource_digests": data.get("run", {}).get("resourceDigests", {}),
        "run_output_uri": data.get("run", {}).get("runOutputUri"),
        "invocation": data.get("invocation", []),
        "request_id": data.get("start_run_request", {}).get("requestId")
        if isinstance(data.get("start_run_request"), dict) else None,
        "params_sha256": hashlib.sha256(
            json.dumps(
                data.get("start_run_request", {}).get("parameters", {}),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if isinstance(data.get("start_run_request"), dict) else None,
    }
    manifest_path = repro_dir / "replay_manifest.json"
    write_text_lf(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    written.append(manifest_path)

    verification = data.get("verification") or {}
    outputs_payload: dict[str, Any] | None = None
    if verification.get("objects") is not None:
        outputs_payload = {
            "source": verification.get("source"),
            "depth": verification.get("depth"),
            "objects": verification.get("objects", []),
            "complete": verification.get("complete"),
        }
    elif data.get("mode") == "download":
        outputs_payload = {
            "source": data.get("source"),
            "destination": data.get("destination"),
            "objects": data.get("downloaded_files", []),
            "complete": not data.get("failures"),
        }
    if outputs_payload is not None:
        outputs_payload["schema_version"] = 2
        outputs_payload["objects"] = [_outputs.describe_output(entry, provenance=manifest)
                                      for entry in outputs_payload.get("objects", [])]
        path = output_dir / "outputs.json"
        write_text_lf(path, json.dumps(outputs_payload, indent=2, default=str) + "\n")
        written.append(path)

        handoff_items = []
        for entry in outputs_payload.get("objects", []):
            local_path = entry.get("local_path") or entry.get("path")
            if not local_path:
                continue
            partners = entry.get("suggested_skills", [])
            if partners:
                handoff_items.append({"path": local_path, "suggested_skills": partners,
                                      "sample_ids": entry["sample_ids"], "reference": entry["reference"],
                                      "qc": entry["qc"], "provenance": entry["provenance"]})
        handoff = {
            "source": outputs_payload.get("source"),
            "items": handoff_items,
            "n_items": len(handoff_items),
        }
        handoff_path = output_dir / "handoff.json"
        write_text_lf(handoff_path, json.dumps(handoff, indent=2, default=str) + "\n")
        written.append(handoff_path)

    if data.get("mode") == "params-template":
        path = output_dir / "params.template.json"
        write_text_lf(
            path,
            json.dumps(data.get("params_template", {}), indent=2, sort_keys=True) + "\n",
        )
        written.append(path)

    return written


def write_bundle(
    output_dir: Path, data: dict[str, Any], *, warn_before_overwrite: bool = True
) -> dict[str, Any]:
    """Write the full ClawBio output contract."""
    from clawbio.common.reproducibility import (
        ReproCommand,
        ReproPath,
        write_checksums,
        write_environment_yml,
        write_portable_commands_sh,
    )

    if warn_before_overwrite:
        _warn_before_overwrite(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    report_path = output_dir / "report.md"
    write_text_lf(report_path, _report_markdown(data))

    mode = data.get("mode")
    table_path = _write_table(output_dir, data)
    extra_paths = _write_extra_artifacts(output_dir, data)
    args: list[Any] = _replay_args(mode, data)
    if data["demo"]:
        args = ["--demo"]
    else:
        args += ["--region", data["region"]]
        if data.get("profile"):
            args += ["--profile", data["profile"]]
    args += ["--output", ReproPath(output_dir, anchor="output_dir")]

    commands_path = write_portable_commands_sh(
        output_dir,
        ReproCommand(
            script_path=_REL_SCRIPT,
            args=args,
            comment=(
                "Replays the local reporting step. A live replay additionally requires "
                "the same AWS account and execution role."
            ),
            preflight=_replay_preflight_lines(data),
        ),
        repo_root=_PROJECT_ROOT,
    )
    env_path = write_environment_yml(
        output_dir,
        env_name="clawbio-healthomics-bridge",
        pip_deps=["boto3>=1.34", "miniwdl>=1.12"],
        conda_deps=[],
        python_version="3.11",
    )

    if mode in _LIST_MODES:
        summary = {
            "kind": mode, "n_items": data["n_items"],
            "region": data["region"], "demo": data["demo"],
        }
        status = "LISTED"
    elif mode == "check":
        summary = {
            "kind": "check",
            "ok": bool(data.get("ok")),
            "n_checks": data.get("n_checks", 0),
            "n_failed": data.get("n_failed", 0),
            "n_warnings": data.get("n_warnings", 0),
            "n_unknown": data.get("n_unknown", 0),
            "scope": data.get("scope", "local"),
            "region": data["region"],
            "demo": data["demo"],
        }
        status = "OK" if data.get("ok") else "FAILED"
    elif mode == "params-template":
        summary = {
            "kind": "params-template",
            "workflow_id": data.get("workflow_id"),
            "n_parameters": data.get("n_items", 0),
            "region": data["region"],
            "demo": data["demo"],
        }
        status = "TEMPLATE"
    elif mode in {"tag", "untag", "tags", "sync-tags"}:
        summary = {
            "kind": mode,
            "run_id": data.get("run_id"),
            "n_tags": len(
                data.get("tags")
                or data.get("desired_tags")
                or data.get("tagged")
                or data.get("untagged")
                or {}
            ),
            "region": data["region"],
            "demo": data["demo"],
        }
        status = "TAGGED" if mode in {"tag", "sync-tags"} else "TAGS"
    else:
        summary = {
            "run_id": data["run"].get("id"),
            "run_status": data["run_status"],
            "n_tasks": data["n_tasks"],
            "n_failed_tasks": data["n_failed"],
            "submitted": data["submitted"],
            "region": data["region"],
            "demo": data["demo"],
        }
        status = str(data["run_status"])
        if mode == "run":
            summary["execution_ok"] = (True if status == "COMPLETED" else
                                       False if status in {"FAILED", "CANCELLED"} else None)
            summary["failure_reason"] = data["run"].get("failureReason")
            summary["report_ok"] = True

    result_path = write_result_json(
        output_dir=output_dir,
        skill=SKILL_NAME,
        version=SKILL_VERSION,
        summary=summary,
        data=data,
        datasets={"AWS HealthOmics": "synthetic offline fixture" if data["demo"] else
                  "local validation only" if mode == "check" and data.get("scope") == "local" else "live account"},
        status=status,
        # Exit 0 means the skill produced a truthful report, not that the run
        # succeeded. The run's outcome is in `status`.
        ok=bool(data.get("ok")) if mode == "check" else True,
    )
    write_checksums(
        [report_path, result_path, table_path, commands_path, env_path, *extra_paths],
        output_dir,
        anchor=output_dir,
    )
    return json.loads(Path(result_path).read_text(encoding="utf-8"))


def run_demo(output_dir: Path) -> dict[str, Any]:
    """Deterministic offline demo: no AWS account, no credentials, no boto3."""
    bundle = json.loads(_DEMO_BUNDLE.read_text(encoding="utf-8"))
    data = map_run_report(bundle, region="us-east-1", demo=True)
    return write_bundle(output_dir, data)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".healthomics-", delete=False) as stream:
        temp = Path(stream.name)
        try:
            json.dump(payload, stream, indent=2, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _live_readiness(args: argparse.Namespace, client: OmicsClient) -> dict[str, Any]:
    return _readiness.run_live_preflight(
        args, omics=client,
        ecr=_ecr.ECROperations(_boto=_ecr.build_ecr_client(args.region, args.profile)),
        s3=_s3.S3Operations(_boto=_s3.build_s3_client(args.region, args.profile)),
    )


def _run_live(args: argparse.Namespace, output_dir: Path) -> dict[str, Any]:
    if args.start_run and args.confirm_submit and (output_dir / "submission.json").exists():
        raise FileExistsError("An existing submission receipt must be preserved; use --resume-from or a fresh output directory")
    _warn_before_overwrite(output_dir)

    def _publish(directory, data, **kwargs):
        data["profile"] = args.profile
        data["invocation"] = list(getattr(args, "_invocation", []))
        if args.params and args.params.is_file():
            snapshot = directory / "reproducibility" / "params.snapshot.json"
            _atomic_json(snapshot, json.loads(args.params.read_text(encoding="utf-8")))
            invocation = data["invocation"]
            if "--params" in invocation:
                invocation[invocation.index("--params") + 1] = str(snapshot)
        return write_bundle(directory, data, **kwargs)

    if args.resume_from:
        receipt = _submission.load_receipt(args.resume_from, profile=args.profile, region=args.region)
        args._submission_receipt = receipt
        client = OmicsOperations(_boto=build_boto_client(args.region, args.profile))
        if not receipt.get("run_id"):
            if not args.confirm_submit or not args.allow_remote_inputs:
                raise ValueError("Submission outcome is uncertain; inspect runs, then use --confirm-submit and --allow-remote-inputs to retry the saved token")
            saved = receipt["request"]
            args.start_run = saved["workflowId"]
            args.workflow_type = saved["workflowType"]
            args.role_arn = saved["roleArn"]
            args.output_uri = saved["outputUri"]
            args.run_name = saved["name"]
            args.workflow_version_name = saved.get("workflowVersionName")
            args.params = output_dir / "recovery.params.json"
            _atomic_json(args.params, saved["parameters"])
            readiness = _live_readiness(args, client)
            _atomic_json(output_dir / "readiness.json", readiness)
            if not readiness["ok"]:
                return _publish(output_dir, map_check_report(readiness, region=args.region), warn_before_overwrite=False)
            args.start_run = None
        _submission.save_receipt(output_dir / "submission.json", receipt)
        args.run_status = _submission.recover_submission(client=client, receipt=receipt, confirmed=args.confirm_submit)
        _submission.save_receipt(output_dir / "submission.json", receipt)
        args._invocation = ["--run-status", args.run_status]

    if args.check:
        if args.live:
            client = OmicsOperations(_boto=build_boto_client(args.region, args.profile))
            readiness = _live_readiness(args, client)
        else:
            readiness = _preflight.run_preflight(args)
        data = map_check_report(
            readiness,
            region=args.region,
        )
        data["profile"] = args.profile
        return _publish(output_dir, data, warn_before_overwrite=False)

    start_run_request: dict[str, Any] | None = None
    if args.start_run:
        start_run_request = prepare_start_request(args)
        if not args.confirm_submit:
            print(
                "ESTIMATE ONLY: no run was submitted and nothing was billed. "
                "Re-run with --confirm-submit to start this run.",
                file=sys.stderr,
            )
            data = map_run_report(
                {"run": {}, "workflow": {}, "tasks": []}, region=args.region,
                start_run_request=start_run_request, submitted=False,
            )
            return _publish(output_dir, data, warn_before_overwrite=False)

    # S3-only modes never construct an omics client: a transfer has nothing to
    # ask HealthOmics about.
    if args.upload_inputs:
        s3 = _s3.S3Operations(_boto=_s3.build_s3_client(args.region, args.profile))
        data = upload_run_inputs(
            client=s3, sources=list(args.upload_inputs), destination=args.to,
            acknowledged=args.allow_remote_inputs, confirmed=args.confirm_upload)
        return _publish(output_dir, _pad_transfer_report(data, args.region),
                            warn_before_overwrite=False)

    client = OmicsOperations(_boto=build_boto_client(args.region, args.profile))

    if args.search_workflows:
        filters: dict[str, Any] = {"type": args.workflow_type} if args.workflow_type else {}
        items = list_all(
            client=client,
            operation="ListWorkflows",
            limit=max(args.limit, 100),
            **filters,
        )
        matches = _recommendations.search_workflows(
            items, args.search_workflows, limit=args.limit
        )
        data = map_workflow_search_report(
            query=args.search_workflows,
            items=matches,
            kind="workflow-search",
            region=args.region,
        )
        return _publish(output_dir, data, warn_before_overwrite=False)

    if args.recommend_workflow:
        filters = {"type": args.workflow_type} if args.workflow_type else {}
        items = list_all(
            client=client,
            operation="ListWorkflows",
            limit=max(args.limit, 100),
            **filters,
        )
        candidates = _recommendations.search_workflows(items, args.recommend_workflow, limit=min(args.limit, 25))
        detailed = []
        for candidate in candidates:
            try:
                metadata = client.call("GetWorkflow", id=candidate["id"],
                                       type=candidate.get("type", args.workflow_type or "PRIVATE"))
                detailed.append({**candidate, **metadata})
            except Exception:
                detailed.append(candidate)
        recommendation = _recommendations.recommend_workflows(
            detailed, args.recommend_workflow, limit=args.limit,
            input_format=args.input_format, engine=args.recommend_engine, region=args.region)
        data = map_workflow_search_report(
            query=args.recommend_workflow,
            items=recommendation["recommendations"],
            kind="workflow-recommendations",
            region=args.region,
        )
        data["inferred_domains"] = recommendation["inferred_domains"]
        return _publish(output_dir, data, warn_before_overwrite=False)

    if args.params_template:
        lookup: dict[str, Any] = {"id": str(args.params_template)}
        if args.workflow_type:
            lookup["type"] = args.workflow_type
        if args.workflow_version_name:
            workflow = client.call(
                "GetWorkflowVersion",
                workflowId=str(args.params_template),
                versionName=args.workflow_version_name,
            )
        else:
            workflow = client.call("GetWorkflow", **lookup)
        data = map_params_template_report(
            _params_template.workflow_params_payload(workflow),
            region=args.region,
        )
        if not data.get("workflow_id"):
            data["workflow_id"] = args.params_template
        return _publish(output_dir, data, warn_before_overwrite=False)

    if args.download_outputs:
        run = client.call("GetRun", id=str(args.download_outputs))
        output_uri = run.get("outputUri")
        if not output_uri:
            raise OmicsCallError(
                f"Run {args.download_outputs} reports no outputUri, so there is "
                f"nothing to download."
            )
        s3 = _s3.S3Operations(_boto=_s3.build_s3_client(args.region, args.profile))
        data = download_run_outputs(
            client=s3, output_uri=output_uri, run_id=str(args.download_outputs),
            destination=Path(args.to), confirmed=args.confirm_download, run_output_uri=run.get("runOutputUri"))
        return _publish(output_dir, _pad_transfer_report(data, args.region),
                            warn_before_overwrite=False)

    if args.register:
        template = (
            json.loads(args.parameter_template.read_text(encoding="utf-8"))
            if args.parameter_template else None
        )
        if args.workflow_id:
            data = register_workflow_version(
                client=client, workflow_id=args.workflow_id,
                definition=Path(args.register), additional_files=list(args.additional_files),
                version_name=args.new_version_name, description=args.description,
                parameter_template=template, confirmed=args.confirm_register,
                output_dir=output_dir)
        else:
            data = register_run_workflow(
                client=client, definition=Path(args.register),
                additional_files=list(args.additional_files), name=args.workflow_name,
                engine=args.engine, description=args.description,
                parameter_template=template, allow_duplicate=args.allow_duplicate_name,
                confirmed=args.confirm_register, output_dir=output_dir)
        return _publish(output_dir, _pad_transfer_report(data, args.region),
                            warn_before_overwrite=False)

    if args.list_run_groups:
        items = list_all(client=client, operation="ListRunGroups", limit=args.limit)
        data = map_list_report(items, kind="run-groups", region=args.region)
        return _publish(output_dir, data, warn_before_overwrite=False)

    if args.list_run_caches:
        items = list_all(client=client, operation="ListRunCaches", limit=args.limit)
        data = map_list_report(items, kind="run-caches", region=args.region)
        return _publish(output_dir, data, warn_before_overwrite=False)

    if args.list_workflow_versions:
        items = list_all(client=client, operation="ListWorkflowVersions",
                         limit=args.limit, workflowId=args.list_workflow_versions)
        data = map_list_report(items, kind="workflow-versions", region=args.region)
        data["workflow_id"] = args.list_workflow_versions
        return _publish(output_dir, data, warn_before_overwrite=False)

    if args.describe_run_group:
        group = describe_run_group(client=client, group_id=args.describe_run_group)
        data = map_list_report([group], kind="run-groups", region=args.region)
        return _publish(output_dir, data, warn_before_overwrite=False)

    if args.describe_run_cache:
        cache = describe_run_cache(client=client, cache_id=args.describe_run_cache)
        data = map_list_report([cache], kind="run-caches", region=args.region)
        return _publish(output_dir, data, warn_before_overwrite=False)

    if args.tag_run:
        tags = json.loads(args.tags)
        data = tag_run(client=client, run_id=args.tag_run, tags=tags)
        return _publish(output_dir, _pad_transfer_report(data, args.region),
                            warn_before_overwrite=False)

    if args.untag_run:
        data = untag_run(client=client, run_id=args.untag_run, keys=list(args.tag_keys))
        return _publish(output_dir, _pad_transfer_report(data, args.region),
                            warn_before_overwrite=False)

    if args.list_tags:
        data = list_run_tags(client=client, run_id=args.list_tags)
        return _publish(output_dir, _pad_transfer_report(data, args.region),
                            warn_before_overwrite=False)

    if args.sync_tags:
        tags = json.loads(args.tags)
        data = sync_run_tags(client=client, run_id=args.sync_tags, desired_tags=tags)
        return _publish(output_dir, _pad_transfer_report(data, args.region),
                            warn_before_overwrite=False)

    if args.list_runs:
        items = list_all(client=client, operation="ListRuns", limit=args.limit)
        data = map_list_report(items, kind="runs", region=args.region)
        return _publish(output_dir, data, warn_before_overwrite=False)

    if args.list_workflows:
        filters: dict[str, Any] = {"type": args.workflow_type} if args.workflow_type else {}
        items = list_all(
            client=client, operation="ListWorkflows", limit=args.limit, **filters
        )
        data = map_list_report(items, kind="workflows", region=args.region)
        return _publish(output_dir, data, warn_before_overwrite=False)

    readiness = None
    if args.start_run:
        readiness = _live_readiness(args, client)
        _atomic_json(output_dir / "readiness.json", readiness)
        if not readiness["ok"]:
            print("READINESS BLOCKED: no run submitted. See the checks in report.md.", file=sys.stderr)
            data = map_check_report(readiness, region=args.region)
            data["profile"] = args.profile
            return _publish(output_dir, data, warn_before_overwrite=False)
        if prepare_start_request(args) != start_run_request:
            raise ValueError("Run parameters changed during readiness inspection; build a fresh plan.")
        receipt = _submission.make_receipt(start_run_request, profile=args.profile, region=args.region)
        _submission.save_receipt(output_dir / "submission.json", receipt)
        args._submission_receipt = receipt
        result = submit_run(client=client, request=start_run_request, confirmed=True)
        run_id = str(result["response"].get("id", ""))
        if not run_id:
            raise RuntimeError("StartRun returned no run id; preserve submission.json before retrying.")
        receipt.update(state="SUBMITTED", run_id=run_id)
        _submission.save_receipt(output_dir / "submission.json", receipt)
        initial = map_run_report({"run": {**result["response"], "id": run_id,
                                  "workflowId": args.start_run, "workflowType": args.workflow_type}},
                                 region=args.region, submitted=True, start_run_request=start_run_request)
        initial["readiness"] = readiness
        initial["profile"] = args.profile
        _publish(output_dir, initial, warn_before_overwrite=False)
        print(f"Submitted run {run_id}; receipt: {output_dir / 'submission.json'}", file=sys.stderr, flush=True)
    else:
        run_id = args.run_status

    if args.wait:
        print(
            f"Waiting for run {run_id} to reach a terminal state "
            f"(polling every {args.poll_interval:.0f}s). The run keeps billing "
            f"while this waits; Ctrl-C stops watching, not the run.",
            file=sys.stderr,
        )
        wait_for_run(
            client=client, run_id=run_id,
            poll_seconds=args.poll_interval,
            timeout_seconds=args.wait_timeout_seconds,
            on_poll=lambda run: _atomic_json(output_dir / "run_state.json", {
                "run_id": run_id, "status": run.get("status"),
                "failure_reason": run.get("failureReason"), "status_message": run.get("statusMessage"),
                "observed_at": time.time(), "profile": args.profile, "region": args.region,
            }),
        )

    bundle = fetch_run_bundle(client=client, run_id=run_id)

    verification = None
    if args.verify_outputs:
        output_uri = (bundle.get("run") or {}).get("outputUri")
        if output_uri:
            s3 = _s3.S3Operations(_boto=_s3.build_s3_client(args.region, args.profile))
            verification = verify_run_outputs(
                client=s3, output_uri=output_uri, run_id=run_id,
                depth=args.verify_outputs,
                destination=Path(args.to) if args.to else None,
                confirmed=args.confirm_download, run_output_uri=bundle["run"].get("runOutputUri"))
        else:
            print(
                f"WARNING: run {run_id} reports no outputUri; nothing to verify.",
                file=sys.stderr,
            )

    data = map_run_report(
        bundle, region=args.region, start_run_request=start_run_request,
        submitted=bool(args.start_run), verification=verification,
    )
    data["readiness"] = readiness
    data["profile"] = args.profile
    if args.logs:
        try:
            data["logs"] = _observability.fetch_run_logs(
                client=_observability.build_logs_client(args.region, args.profile),
                run=bundle["run"], limit=args.log_limit)
        except Exception as exc:
            data["logs"] = {"status": "UNAVAILABLE", "reason": _ecr.exception_code(exc)}
    if args.analyze_run:
        data["analysis"] = _observability.run_analyzer(run_id=run_id,
            output=output_dir / "analysis.csv", region=args.region, profile=args.profile)
    if args.validate_smoke:
        paths = [Path(item["local_path"]) for item in (verification or {}).get("objects", []) if item.get("local_path")]
        data["smoke_validation"] = _outputs.validate_smoke_outputs(args.validate_smoke, paths,
            expected_residues=args.expected_residues, expected_greeting=args.expected_greeting)
    return _publish(output_dir, data, warn_before_overwrite=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="healthomics_bridge.py",
        description=(
            "Submit, monitor and import AWS HealthOmics runs via boto3, behind an "
            "allow-listed client, an egress gate and a cost gate."
        ),
    )
    parser.add_argument("--demo", action="store_true", help="Offline demo; no AWS account needed")
    parser.add_argument("--logs", action="store_true", help="Fetch bounded run/engine CloudWatch logs")
    parser.add_argument("--log-limit", type=int, default=100)
    parser.add_argument("--analyze-run", action="store_true", help="Invoke optional AWS Run Analyzer")
    parser.add_argument("--input-format", help="Declared input format to match in workflow recommendations")
    parser.add_argument("--recommend-engine", choices=["WDL", "CWL", "NEXTFLOW", "WDL_LENIENT"])
    parser.add_argument("--validate-smoke", choices=["wdl", "esmfold"], help="Validate downloaded synthetic/public smoke outputs")
    parser.add_argument("--expected-residues", type=int)
    parser.add_argument("--expected-greeting")
    parser.add_argument("--check", action="store_true",
                        help="Run read-only preflight checks and exit before any live action")
    parser.add_argument("--live", action="store_true",
                        help="With --check: inspect the workflow and run prerequisites in AWS")
    parser.add_argument("--allow-unverified-readiness", action="store_true",
                        help="Acknowledge required UNKNOWN readiness checks; never overrides FAIL")
    parser.add_argument(
        "--output", type=Path, default=Path("output/healthomics"), help="Output directory"
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume-from", type=Path, help="Recover the original submission receipt without regenerating its token")
    mode.add_argument("--list-runs", action="store_true", help="List recent runs (read-only)")
    mode.add_argument("--list-workflows", action="store_true", help="List workflows (read-only)")
    mode.add_argument("--run-status", metavar="RUN_ID", help="Report one run (read-only)")
    mode.add_argument("--start-run", metavar="WORKFLOW_ID", help="Submit a run (gated)")
    mode.add_argument("--upload-inputs", nargs="+", type=Path, metavar="PATH",
                      help="Upload a run's input files to S3 (gated)")
    mode.add_argument("--download-outputs", metavar="RUN_ID",
                      help="Download one run's outputs from S3 (gated)")
    mode.add_argument("--register", metavar="DEFINITION",
                      help="Register a WDL/CWL/Nextflow definition as a private "
                           "workflow (gated). Dry-run without --confirm-register. "
                           "With --workflow-id instead of --workflow-name, adds a "
                           "version to an existing workflow.")
    mode.add_argument("--list-run-groups", action="store_true",
                      help="List run groups referenced by --run-group-id (read-only)")
    mode.add_argument("--list-run-caches", action="store_true",
                      help="List run caches referenced by --cache-id (read-only)")
    mode.add_argument("--describe-run-group", metavar="GROUP_ID",
                      help="One run group's own detail (read-only)")
    mode.add_argument("--describe-run-cache", metavar="CACHE_ID",
                      help="One run cache's own detail (read-only)")
    mode.add_argument("--list-workflow-versions", metavar="WORKFLOW_ID",
                      help="List a workflow's versions (read-only)")
    mode.add_argument("--search-workflows", metavar="QUERY",
                      help="Search workflows by name, description, id and type")
    mode.add_argument("--recommend-workflow", metavar="TASK",
                      help="Recommend workflows for a plain-English task")
    mode.add_argument("--params-template", metavar="WORKFLOW_ID",
                      help="Write a starter params.template.json for a workflow")
    mode.add_argument("--tag-run", metavar="RUN_ID",
                      help="Set tags on an existing run (gated). Needs --tags.")
    mode.add_argument("--untag-run", metavar="RUN_ID",
                      help="Remove tags from a run by key (gated). Needs --tag-keys.")
    mode.add_argument("--list-tags", metavar="RUN_ID",
                      help="List tags on a run (read-only)")
    mode.add_argument("--sync-tags", metavar="RUN_ID",
                      help="Converge a run's tags to --tags JSON")

    parser.add_argument(
        "--workflow-type", choices=["PRIVATE", "READY2RUN"],
        help=(
            "Workflow type. REQUIRED with --start-run: AWS needs it to resolve a "
            "Ready2Run workflow, and being explicit avoids the not-found error a "
            "missing type produces. Also filters --list-workflows."
        ),
    )
    parser.add_argument("--params", type=Path, help="JSON parameters file (with --start-run)")
    parser.add_argument("--output-uri", help="S3 URI for run outputs (with --start-run)")
    parser.add_argument("--role-arn", help="HealthOmics execution role ARN (with --start-run)")
    parser.add_argument("--run-name", help="Run name (with --start-run)")
    parser.add_argument("--to", help="Destination: an s3:// URI for --upload-inputs, "
                                     "a local directory for --download-outputs")
    parser.add_argument("--confirm-upload", action="store_true",
                        help="Actually upload. Without it --upload-inputs is a dry run.")
    parser.add_argument("--confirm-download", action="store_true",
                        help="Actually download. S3 egress is billable.")
    parser.add_argument(
        "--verify-outputs", nargs="?", const="manifest", default=None,
        choices=["manifest", "deep"],
        help="With --run-status: record what the run produced. 'manifest' lists "
             "sizes and ETags and moves no bytes; 'deep' downloads and computes "
             "real SHA-256 checksums (needs --confirm-download).",
    )
    parser.add_argument("--workflow-name", help="Name for the new workflow (with --register)")
    parser.add_argument("--engine", choices=sorted(_registration.SUPPORTED_ENGINES),
                        help="Workflow engine. Inferred from the definition's "
                             "extension (.wdl/.cwl/.nf) when omitted.")
    parser.add_argument("--additional-files", nargs="+", type=Path, default=[],
                        help="Extra files for a multi-file WDL/CWL bundle")
    parser.add_argument("--description", help="Workflow description (with --register)")
    parser.add_argument("--parameter-template", type=Path,
                        help="JSON parameter template (with --register)")
    parser.add_argument("--allow-duplicate-name", action="store_true",
                        help="Register even though a workflow of this name exists")
    parser.add_argument("--confirm-register", action="store_true",
                        help="Actually create the workflow (or version). Without "
                             "it --register is a dry run.")
    parser.add_argument("--workflow-id", help="Existing workflow id to add a "
                                              "version to (with --register)")
    parser.add_argument("--new-version-name",
                        help="Version name to create (with --register --workflow-id)")
    parser.add_argument("--tags", help="JSON tags to set (with --tag-run or --sync-tags)")
    parser.add_argument("--tag-keys", nargs="+", help="Tag keys to remove (with --untag-run)")
    parser.add_argument("--storage-type", choices=["STATIC", "DYNAMIC"],
                        help="Omit to take AWS's preferred DYNAMIC default")
    parser.add_argument("--storage-capacity", type=int,
                        help="Run storage in GiB; only meaningful with --storage-type STATIC")
    parser.add_argument("--cache-id", help="Run cache to reuse task results from")
    parser.add_argument("--cache-behavior", choices=["CACHE_ALWAYS", "CACHE_ON_FAILURE"])
    parser.add_argument("--run-group-id", help="Run group, for concurrency and cost caps")
    parser.add_argument("--workflow-version-name", help="Pin the workflow version to run")
    parser.add_argument(
        "--run-tags", metavar="JSON",
        help=(
            "Cost-allocation tags for the RUN as JSON, e.g. '{\"team\":\"genomics\"}'. "
            "Per-run cost allocation, applied at submission time."
        ),
    )

    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    parser.add_argument("--limit", type=int, default=25, help="Max results for list modes")
    parser.add_argument(
        "--wait", action="store_true",
        help="Poll until the run reaches a terminal state before reporting. "
             "Watching does not stop billing, and Ctrl-C stops watching, not the run.",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=30.0,
        help="Seconds between --wait polls (default: 30)",
    )
    parser.add_argument(
        "--wait-timeout-seconds", type=float, default=86_400.0,
        help="Give up watching after this long (default: 24h). The run continues.",
    )
    parser.add_argument(
        "--allow-remote-inputs", action="store_true",
        help="Acknowledge that submitting a run sends genomic data to AWS",
    )
    parser.add_argument(
        "--confirm-submit", action="store_true",
        help="Actually submit. Without it, --start-run only estimates and bills nothing.",
    )
    return parser


def _selected_mode(args: argparse.Namespace) -> str:
    for attr, label in (
        ("check", "check"),
        ("resume_from", "resume"),
        ("list_runs", "runs"),
        ("list_workflows", "workflows"),
        ("run_status", "run"),
        ("start_run", "start-run"),
        ("upload_inputs", "upload"),
        ("download_outputs", "download"),
        ("register", "register"),
        ("list_run_groups", "run-groups"),
        ("list_run_caches", "run-caches"),
        ("describe_run_group", "run-group"),
        ("describe_run_cache", "run-cache"),
        ("list_workflow_versions", "workflow-versions"),
        ("search_workflows", "workflow-search"),
        ("recommend_workflow", "workflow-recommendations"),
        ("params_template", "params-template"),
        ("tag_run", "tag"),
        ("untag_run", "untag"),
        ("list_tags", "tags"),
        ("sync_tags", "sync-tags"),
    ):
        if getattr(args, attr, None):
            return label
    return "unknown"


def _write_error_bundle(output_dir: Path, args: argparse.Namespace, exc: BaseException) -> None:
    """Best-effort error report with a stable machine-readable code."""
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = _error_codes.error_payload(
        exc,
        mode=_selected_mode(args),
        region=getattr(args, "region", None),
    )
    receipt = getattr(args, "_submission_receipt", None)
    if receipt:
        payload["submission"] = receipt
    report = generate_report_header(
        title="AWS HealthOmics — Error",
        skill_name=SKILL_NAME,
        skill_version=SKILL_VERSION,
        extra_metadata={
            "Mode": payload["mode"],
            "Region": payload["region"],
            "Error code": payload["error_code"],
        },
    )
    report += (
        f"## Error\n\n`{payload['error_code']}`\n\n{payload['message']}\n\n"
        + generate_report_footer()
    )
    if receipt:
        report += f"\nSubmission state: {receipt['state']}; run ID: {receipt.get('run_id')}. See submission.json.\n"
    write_text_lf(output_dir / "report.md", report)
    write_result_json(
        output_dir=output_dir,
        skill=SKILL_NAME,
        version=SKILL_VERSION,
        summary={
            "kind": "error",
            "error_code": payload["error_code"],
            "mode": payload["mode"],
            "region": payload["region"],
            "run_id": receipt.get("run_id") if receipt else None,
        },
        data=payload,
        datasets={"AWS HealthOmics": "not reached or failed"},
        status=payload["error_code"],
        ok=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args._invocation = _submission.replay_invocation(list(argv if argv is not None else sys.argv[1:]))
    if (args.logs or args.analyze_run) and not (args.run_status or args.resume_from):
        parser.error("--logs and --analyze-run require --run-status or --resume-from")
    if not 1 <= args.log_limit <= 1000:
        parser.error("--log-limit must be between 1 and 1000")
    if (args.input_format or args.recommend_engine) and not args.recommend_workflow:
        parser.error("Recommendation filters require --recommend-workflow")
    if args.validate_smoke and args.verify_outputs != "deep":
        parser.error("--validate-smoke requires --verify-outputs deep and --confirm-download")
    if args.validate_smoke == "esmfold" and (not args.expected_residues or args.expected_residues <= 0):
        parser.error("ESMFold smoke validation requires --expected-residues > 0")
    if args.validate_smoke == "wdl" and args.expected_greeting is None:
        parser.error("WDL smoke validation requires --expected-greeting")
    if args.live and not args.check:
        parser.error("--live only applies to --check")
    if args.live and not args.start_run:
        parser.error("--check --live requires --start-run and its run parameters")
    if args.allow_unverified_readiness and not args.start_run:
        parser.error("--allow-unverified-readiness only applies to --start-run")
    output_dir = args.output.expanduser().resolve()

    if args.demo:
        result = run_demo(output_dir)
        print(json.dumps(result["summary"], indent=2))
        return 0

    if not any([args.resume_from, args.check, args.list_runs, args.list_workflows, args.run_status, args.start_run,
                args.upload_inputs, args.download_outputs, args.register,
                args.list_run_groups, args.list_run_caches, args.describe_run_group,
                args.describe_run_cache, args.list_workflow_versions,
                args.search_workflows, args.recommend_workflow, args.params_template,
                args.tag_run, args.untag_run, args.list_tags, args.sync_tags]):
        parser.error(
            "choose one of --demo, --check, --list-runs, --list-workflows, --run-status, "
            "--start-run, --upload-inputs, --download-outputs, --register, "
            "--list-run-groups, --list-run-caches, --describe-run-group, "
            "--describe-run-cache, --list-workflow-versions, --search-workflows, "
            "--recommend-workflow, --params-template, --tag-run, --untag-run, "
            "--list-tags or --sync-tags"
        )

    # Mode-scoped flags rejected outside their mode, so a misplaced flag is a
    # loud error rather than a silently ignored one.
    for flag, value, owner in (
        ("--workflow-name", args.workflow_name, "--register"),
        ("--engine", args.engine, "--register"),
        ("--additional-files", args.additional_files, "--register"),
        ("--description", args.description, "--register"),
        ("--parameter-template", args.parameter_template, "--register"),
        ("--allow-duplicate-name", args.allow_duplicate_name, "--register"),
        ("--confirm-register", args.confirm_register, "--register"),
        ("--workflow-id", args.workflow_id, "--register"),
        ("--new-version-name", args.new_version_name, "--register"),
        ("--confirm-upload", args.confirm_upload, "--upload-inputs"),
        ("--tag-keys", args.tag_keys, "--untag-run"),
    ):
        owned = {"--register": args.register, "--upload-inputs": args.upload_inputs,
                 "--tag-run": args.tag_run, "--untag-run": args.untag_run}[owner]
        if value and not owned:
            parser.error(f"{flag} only applies to {owner}")

    if args.tags and not (args.tag_run or args.sync_tags):
        parser.error("--tags only applies to --tag-run or --sync-tags")
    if args.tag_run and not args.tags:
        parser.error("--tag-run requires --tags '{\"key\":\"value\"}'")
    if args.sync_tags and not args.tags:
        parser.error("--sync-tags requires --tags '{\"key\":\"value\"}'")
    if args.untag_run and not args.tag_keys:
        parser.error("--untag-run requires --tag-keys KEY [KEY ...]")

    if args.upload_inputs:
        if not args.to:
            parser.error("--upload-inputs requires --to s3://bucket/prefix/")
        missing_sources = [str(p) for p in args.upload_inputs if not p.expanduser().is_file()]
        if missing_sources:
            parser.error(f"input file not found: {', '.join(missing_sources)}")

    if args.download_outputs and not args.to:
        parser.error("--download-outputs requires --to <local-directory>")

    if args.verify_outputs and not args.run_status:
        parser.error("--verify-outputs only applies to --run-status")

    if args.verify_outputs == "deep" and not args.confirm_download:
        parser.error(
            "--verify-outputs deep downloads every output to hash it, and S3 "
            "egress is billable. Add --confirm-download, or use "
            "--verify-outputs manifest which moves no bytes."
        )

    if args.register:
        if args.workflow_id:
            if args.workflow_name:
                parser.error("--register takes --workflow-name (new workflow) or "
                             "--workflow-id (new version of an existing one), not both")
            if not args.new_version_name:
                parser.error("--register --workflow-id requires --new-version-name")
        elif not args.workflow_name:
            parser.error("--register requires --workflow-name, or --workflow-id "
                         "plus --new-version-name to version an existing workflow")
        if not Path(args.register).expanduser().is_file():
            parser.error(f"definition not found: {args.register}")
        for extra in args.additional_files:
            if not extra.expanduser().is_file():
                parser.error(f"additional file not found: {extra}")
        if args.parameter_template and not args.parameter_template.expanduser().is_file():
            parser.error(f"parameter template not found: {args.parameter_template}")

    if args.start_run:
        missing = [
            flag for flag, value in (
                ("--params", args.params), ("--output-uri", args.output_uri),
                ("--role-arn", args.role_arn), ("--run-name", args.run_name),
                # Required rather than defaulted: guessing PRIVATE would make a
                # Ready2Run submission fail with a bare not-found error, which
                # is exactly the confusion this skill exists to avoid.
                ("--workflow-type", args.workflow_type),
            ) if not value
        ]
        if missing:
            parser.error(f"--start-run requires {', '.join(missing)}")
        if not args.params.exists():
            parser.error(f"params file not found: {args.params}")
        if args.storage_capacity is not None and args.storage_type != "STATIC":
            parser.error(
                "--storage-capacity only applies to STATIC run storage; add "
                "--storage-type STATIC, or drop it and take the DYNAMIC default"
            )
        if args.cache_behavior and not args.cache_id:
            parser.error("--cache-behavior requires --cache-id")

    try:
        result = _run_live(args, output_dir)
    except Exception as exc:
        try:
            _write_error_bundle(output_dir, args, exc)
        except OSError:
            pass
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result["summary"], indent=2))
    if result["summary"].get("kind") == "check" and not result["summary"].get("ok"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
