"""Bounded read-only log retrieval and an optional AWS Run Analyzer adapter."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any


def build_logs_client(region: str, profile: str | None):
    import boto3
    from botocore.config import Config
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client("logs", region_name=region,
                          config=Config(retries={"mode": "adaptive", "max_attempts": 5},
                                        connect_timeout=10, read_timeout=30))


def fetch_run_logs(*, client: Any, run: dict[str, Any], limit: int = 100) -> dict[str, Any]:
    if not 1 <= limit <= 1000:
        raise ValueError("Log limit must be between 1 and 1000")
    streams = (run.get("logLocation") or {}).values()
    events = []
    for location in sorted(set(streams)):
        # GetRun may return ARNs or stream names. Never enumerate account streams.
        stream = location.split(":log-stream:", 1)[-1]
        if stream not in {f"run/{run['id']}", f"run/{run['id']}/engine"}:
            continue
        token = None
        for _ in range(10):
            if len(events) >= limit:
                break
            response = client.get_log_events(logGroupName="/aws/omics/WorkflowLog",
                logStreamName=stream, limit=limit-len(events), startFromHead=True,
                **({"nextToken": token} if token else {}))
            next_token = response.get("nextForwardToken")
            if token and next_token == token:
                break
            events.extend({**event, "stream": stream} for event in response.get("events", [])[:limit-len(events)])
            token = next_token
            if not token:
                break
    return {"status": "AVAILABLE" if events else "UNAVAILABLE", "events": events,
            "limit": limit, "bounded": True, "complete": False}


def run_analyzer(*, run_id: str, output: Path, region: str, profile: str | None) -> dict[str, Any]:
    executable = shutil.which("aws-healthomics-tools")
    if not executable:
        return {"status": "UNAVAILABLE", "reason": "Install optional aws-healthomics-tools to enable Run Analyzer"}
    output = Path(output)
    if output.exists():
        raise FileExistsError("Analyzer output exists; choose a fresh report directory")
    env = {**os.environ, "AWS_DEFAULT_REGION": region, "AWS_REGION": region}
    if profile:
        env["AWS_PROFILE"] = profile
    result = subprocess.run([executable, "run_analyzer", str(run_id), "-o", str(output)],
                            env=env, capture_output=True, text=True, timeout=300)
    return {"status": "COMPLETED" if result.returncode == 0 and output.is_file() else "FAILED",
            "returncode": result.returncode, "output": str(output),
            "scope": "AWS estimates, not a billing limit"}
