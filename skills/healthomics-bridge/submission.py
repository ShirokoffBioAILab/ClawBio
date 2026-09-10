"""Versioned submission receipts and explicit, token-preserving recovery."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, TypedDict


class SubmissionReceipt(TypedDict):
    schema_version: int
    state: str
    request: dict[str, Any]
    request_sha256: str
    profile: str | None
    region: str
    run_id: str | None


def replay_invocation(argv: list[str]) -> list[str]:
    result = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg == "--output":
            skip_next = True
        elif arg.startswith("--output=") or arg.startswith("--confirm-"):
            continue
        else:
            result.append(arg)
    return result


def request_digest(request: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def resolve_identity(*, profile: str | None, region: str) -> dict[str, str]:
    import boto3
    from botocore.config import Config
    session = boto3.Session(profile_name=profile)
    identity = session.client("sts", region_name=region,
        config=Config(connect_timeout=10, read_timeout=30, retries={"mode": "standard", "max_attempts": 3})).get_caller_identity()
    return {"account": identity["Account"], "partition": identity["Arn"].split(":")[1]}


def verify_identity(receipt: dict[str, Any], identity: dict[str, str]) -> None:
    if receipt.get("schema_version") != 3 or not receipt.get("identity"):
        raise ValueError("Unbound legacy receipt: inspect its run with --run-status; do not retry it automatically")
    if receipt["identity"] != identity:
        raise ValueError("Receipt AWS account/partition differs from the resolved caller")


def make_receipt(request: dict[str, Any], *, profile: str | None, region: str,
                 identity: dict[str, str] | None = None) -> SubmissionReceipt:
    return {"schema_version": 3 if identity else 2, "state": "SUBMITTING", "request": request,
            "request_sha256": request_digest(request), "profile": profile,
            "region": region, "run_id": None, **({"identity": identity} if identity else {})}


def load_receipt(path: Path, *, profile: str | None, region: str) -> dict[str, Any]:
    receipt = json.loads(Path(path).read_text(encoding="utf-8"))
    if receipt.get("schema_version") not in (1, 2, 3):
        raise ValueError("Unsupported receipt schema")
    if (receipt.get("profile") or "default", receipt.get("region")) != (profile or "default", region):
        raise ValueError("Receipt AWS context differs; use its original profile and region")
    request = receipt.get("request")
    if not isinstance(request, dict) or not request.get("requestId"):
        raise ValueError("Receipt has no original request token")
    if receipt.get("schema_version") in (2, 3) and receipt.get("request_sha256") != request_digest(request):
        raise ValueError("Receipt request digest mismatch")
    if receipt.get("state") not in ("SUBMITTING", "SUBMITTED"):
        raise ValueError("Invalid receipt state")
    if receipt.get("state") == "SUBMITTED" and not receipt.get("run_id"):
        raise ValueError("Submitted receipt is missing its run ID")
    return receipt


def recover_submission(*, client: Any, receipt: dict[str, Any], confirmed: bool) -> str:
    if receipt.get("run_id"):
        return str(receipt["run_id"])
    if not confirmed:
        raise ValueError("Submission outcome is uncertain. Inspect AWS runs before explicitly confirming a retry; preserve the original token.")
    response = client.call("StartRun", **receipt["request"])
    if not response.get("id"):
        raise RuntimeError("StartRun returned no ID; submission remains uncertain")
    receipt.update(state="SUBMITTED", run_id=str(response["id"]))
    return receipt["run_id"]


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".receipt-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, indent=2, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_receipt(path: Path, receipt: dict[str, Any]) -> None:
    atomic_json(path, receipt)
    # Keep the request in one file; the journal records only state transitions.
    event = {"observed_at": time.time(), "state": receipt["state"],
             "run_id": receipt.get("run_id"), "request_sha256": request_digest(receipt["request"])}
    with path.with_suffix(".journal.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
