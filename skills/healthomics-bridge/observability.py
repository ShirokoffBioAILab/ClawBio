"""Bounded read-only log retrieval and an optional AWS Run Analyzer adapter."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import Any
import re
import csv


def build_logs_client(region: str, profile: str | None):
    import boto3
    from botocore.config import Config
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client("logs", region_name=region,
                          config=Config(retries={"mode": "adaptive", "max_attempts": 5},
                                        connect_timeout=10, read_timeout=30))


def fetch_run_logs(*, client: Any, run: dict[str, Any], limit: int = 100,
                   tasks: list[dict[str, Any]] | None = None,
                   cursors: dict[str, str] | None = None,
                   start_time: int | None = None, end_time: int | None = None) -> dict[str, Any]:
    if not 1 <= limit <= 1000:
        raise ValueError("Log limit must be between 1 and 1000")
    if start_time is not None and (start_time < 0 or (end_time is not None and end_time <= start_time)):
        raise ValueError("Invalid log time window")
    streams = set()
    for location in (run.get("logLocation") or {}).values():
        stream = location.split(":log-stream:", 1)[-1]
        if stream in {f"run/{run['id']}", f"run/{run['id']}/engine"}:
            streams.add(stream)
    for task in (tasks or [])[:25]:
        task_id = str(task.get("taskId", ""))
        if re.fullmatch(r"[A-Za-z0-9-]+", task_id):
            streams.add(f"run/{run['id']}/task/{task_id}")
    events = []
    errors = []
    next_cursors = {key: value for key, value in (cursors or {}).items() if key in streams}
    per_stream = max(1, limit // max(1, len(streams)))
    for stream in sorted(streams):
        token = next_cursors.get(stream)
        stream_events = 0
        for _ in range(10):
            if len(events) >= limit or stream_events >= per_stream:
                break
            try:
                response = client.get_log_events(logGroupName="/aws/omics/WorkflowLog",
                    logStreamName=stream, limit=min(limit-len(events), per_stream-stream_events), startFromHead=True,
                    **({"nextToken": token} if token else {}),
                    **({"startTime": start_time} if start_time is not None else {}),
                    **({"endTime": end_time} if end_time is not None else {}))
            except Exception as exc:
                code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
                errors.append({"stream": stream, "code": code})
                break
            next_token = response.get("nextForwardToken")
            if token and next_token == token:
                break
            page = response.get("events", [])[:min(limit-len(events), per_stream-stream_events)]
            events.extend({**event, "stream": stream} for event in page)
            stream_events += len(page)
            token = next_token
            if token:
                next_cursors[stream] = token
            if not token:
                break
    evidence = [{"stream": e["stream"], "timestamp": e.get("timestamp"), "code": code}
                for e in events for code in re.findall(r"\b(?:ECR_PERMISSION_ERROR|OUT_OF_MEMORY|ACCESS_DENIED|TASK_FAILED)\b", e.get("message", ""))]
    return {"status": ("PARTIAL" if errors else "AVAILABLE") if events else "UNAVAILABLE",
            "run_id": str(run["id"]), "events": events, "errors": errors,
            "cursors": next_cursors, "failure_evidence": evidence,
            "start_time": start_time, "end_time": end_time,
            "limit": limit, "bounded": True, "complete": False}


def run_analyzer(*, run_id: str, output: Path, region: str, profile: str | None) -> dict[str, Any]:
    executable = shutil.which("aws-healthomics-tools")
    if not executable:
        return {"status": "UNAVAILABLE", "reason": "Install optional aws-healthomics-tools to enable Run Analyzer"}
    output = Path(output)
    if output.exists():
        raise FileExistsError("Analyzer output exists; choose a fresh report directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "AWS_DEFAULT_REGION": region, "AWS_REGION": region}
    if profile:
        env["AWS_PROFILE"] = profile
    command = [executable, "run_analyzer", str(run_id), "-o", str(output), f"--region={region}"]
    if profile:
        command.append(f"--profile={profile}")
    result = subprocess.run(command,
                            env=env, capture_output=True, text=True, timeout=300)
    rows = 0
    columns = []
    if result.returncode == 0 and output.is_file():
        with output.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            columns = reader.fieldnames or []
            rows = sum(1 for _ in reader)
    return {"status": "COMPLETED" if result.returncode == 0 and output.is_file() else "FAILED",
            "returncode": result.returncode, "output": str(output),
            "rows": rows, "columns": columns, "region": region, "profile": profile,
            "tested_tools_version": "0.13.2",
            "scope": "AWS estimates, not a billing limit"}
