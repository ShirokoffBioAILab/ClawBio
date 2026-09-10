"""Read-only run observation, complete pagination, and bounded task enrichment."""
from __future__ import annotations

import time
from typing import Any
from omics_client import OmicsClient, OmicsCallError

_MAX_TASKS_TO_ENRICH = 25


_TERMINAL_RUN_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "DELETED"})


def wait_for_run(
    *, client: OmicsClient, run_id: str, poll_seconds: float = 30.0,
    timeout_seconds: float = 86_400.0,
    on_poll: Any = None,
) -> dict[str, Any]:
    """Poll ``GetRun`` until the run reaches a terminal state.

    Watching a run you started is not analysis, so it belongs here rather than
    a separate tool. Uses only ``GetRun``, already allow-listed, so this adds
    no reach — a caller who can start a run can already read its status.

    Raises ``TimeoutError`` rather than polling forever: an unbounded loop
    against a billing API is how a stuck run becomes a stuck terminal.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        run = client.call("GetRun", id=str(run_id))
        if on_poll is not None:
            on_poll(run)
        status = str(run.get("status", "")).upper()
        if status in _TERMINAL_RUN_STATES:
            return run
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Run {run_id} was {status or 'UNKNOWN'} after {timeout_seconds:.0f}s. "
                f"It is still running and still billing — this gave up watching, "
                f"it did not stop the run."
            )
        time.sleep(poll_seconds)


def _enrich_failed_tasks(*, client: OmicsClient, run_id: str, tasks: list[dict[str, Any]]) -> None:
    """Fetch the one field ListRunTasks omits: why a failed task failed.

    Mutates ``tasks`` in place. Scoped to FAILED/CANCELLED tasks, capped at
    ``_MAX_TASKS_TO_ENRICH``, and best-effort per task -- a permissions gap or
    a single GetRunTask failure must not sink an otherwise good report, the
    same posture as the tag lookup above it.
    """
    failed = [t for t in tasks if str(t.get("status", "")).upper() in {"FAILED", "CANCELLED"}]
    for task in failed[:_MAX_TASKS_TO_ENRICH]:
        task_id = task.get("taskId")
        if not task_id:
            continue
        try:
            detail = client.call("GetRunTask", id=str(run_id), taskId=str(task_id))
        except Exception:
            continue
        for field in ("statusMessage", "failureReason", "logStream"):
            if detail.get(field):
                task[field] = detail[field]


def fetch_run_bundle(*, client: OmicsClient, run_id: str) -> dict[str, Any]:
    """Everything one run report needs, in as few calls as possible.

    Still one workflow lookup, with no try-PRIVATE-then-retry-READY2RUN dance —
    but not because the id self-resolves. ``GetWorkflow`` raises
    ``ResourceNotFoundException`` for a Ready2Run id unless told
    ``type=READY2RUN``; the run record carries ``workflowType``, so the type is
    already known before the call, and no try-one-then-the-other retry is
    needed.
    """
    run = client.call("GetRun", id=str(run_id))
    tasks = []
    task_token = None
    seen_tokens = set()
    while True:
        page = client.call("ListRunTasks", id=str(run_id), **({"nextToken": task_token} if task_token else {}))
        tasks.extend(page.get("items", []) or [])
        task_token = page.get("nextToken")
        if not task_token:
            break
        if task_token in seen_tokens:
            raise OmicsCallError("ListRunTasks repeated a pagination token; task list is incomplete")
        seen_tokens.add(task_token)
    _enrich_failed_tasks(client=client, run_id=run_id, tasks=tasks)

    workflow: dict[str, Any] = {}
    workflow_id = run.get("workflowId")
    if workflow_id:
        lookup: dict[str, Any] = {"id": str(workflow_id)}
        # Omitted rather than guessed when the run does not say: AWS's own
        # default is the right answer, and a wrong guess is a not-found error
        # that reads like a bad id.
        workflow_type = run.get("workflowType")
        if workflow_type:
            lookup["type"] = workflow_type
        try:
            if run.get("workflowVersionName"):
                workflow = client.call("GetWorkflowVersion", workflowId=str(workflow_id),
                                       versionName=run["workflowVersionName"])
            else:
                workflow = client.call("GetWorkflow", **lookup)
        except Exception:
            # A workflow this account can no longer see does not invalidate the
            # run report; it just means the workflow block stays empty. This
            # once also hid a real bug — the lookup failing for every Ready2Run
            # run — so the report names the workflow as unavailable rather than
            # quietly omitting it.
            workflow = {}

    # Tags are read back, not echoed from the submission. Setting run tags is
    # this skill's headline capability and it could not confirm its own work --
    # verifying required the AWS CLI. Read-only and best-effort: a missing
    # ListTagsForResource permission must not sink an otherwise good report.
    tags: dict[str, str] = {}
    arn = run.get("arn")
    if arn:
        try:
            tags = dict(client.call("ListTagsForResource", resourceArn=str(arn)).get("tags", {}))
        except Exception:
            tags = {}

    return {"run": run, "workflow": workflow, "tasks": tasks, "tags": tags}
