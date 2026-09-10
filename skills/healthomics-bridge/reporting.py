"""Pure Markdown rendering for HealthOmics report data."""
from __future__ import annotations

import json
from typing import Any
from clawbio.common.report import generate_report_header, generate_report_footer
from healthomics_pricing import estimated_cost_line
from omics_client import ALLOWED_OPERATIONS
from contracts import SKILL_NAME, SKILL_VERSION


def _transfer_markdown(data: dict[str, Any]) -> str:
    """Report for the modes that move bytes or create a workflow."""
    mode = data["mode"]
    titles = {
        "upload": "AWS HealthOmics — Input Upload",
        "download": "AWS HealthOmics — Output Download",
        "register": "AWS HealthOmics — Workflow Registration",
    }
    acted = {"upload": data.get("uploaded"), "download": data.get("downloaded"),
             "register": data.get("registered")}[mode]
    header = generate_report_header(
        title=titles[mode],
        skill_name=SKILL_NAME,
        skill_version=SKILL_VERSION,
        extra_metadata={
            "Mode": f"{mode.title()}" + ("" if acted else " — DRY RUN"),
            "Region": data["region"],
        },
    )
    lines = [header, ""]

    if mode == "upload":
        lines += ["## Upload", ""]
        lines.append(f"**Destination**: `{data['destination']}`")
        if acted:
            lines.append(
                f"\n{data['n_uploaded']} file(s), {data['n_bytes']:,} bytes uploaded."
            )
            lines += ["", "| Source | S3 URI | Bytes |", "|---|---|---|"]
            for entry in data.get("uploaded_files", []):
                lines.append(
                    f"| `{entry['source']}` | `{entry['uri']}` | {entry['n_bytes']:,} |"
                )
            lines.append(
                "\nPass these URIs to `--start-run --params`; this skill does not "
                "write them into a params file for you."
            )
        else:
            lines.append("\n**Nothing was uploaded.** Files that would be sent:")
            lines += [""] + [f"- `{p}`" for p in data.get("sources", [])]
            lines.append("\nRe-run with `--confirm-upload` to transfer.")

    elif mode == "download":
        lines += ["## Download", ""]
        lines.append(f"**Source**: `{data['source']}`")
        if acted:
            lines.append(
                f"\n{data['n_downloaded']} of {data['n_objects']} object(s) written "
                f"to `{data['destination']}`."
            )
            if data.get("failures"):
                lines += ["", "### Failed", ""]
                for failure in data["failures"]:
                    lines.append(f"- `{failure['key']}` — {failure['error']}")
        else:
            lines.append(
                f"\n**Nothing was downloaded.** {data['n_objects']} object(s), "
                f"{data['n_bytes']:,} bytes are available. Re-run with "
                f"`--confirm-download` to transfer — S3 egress is billable."
            )

    else:  # register
        zip_manifest = data.get("zip") or {}
        lines += ["## Workflow definition", ""]
        lines.append(f"- **Name**: `{data['workflow_name']}`")
        lines.append(f"- **Engine**: {data['engine']}")
        lines.append(f"- **Definition**: `{data['definition_path']}`")
        lines.append(
            f"- **Archive**: {zip_manifest.get('n_bytes', 0):,} bytes, "
            f"`{str(zip_manifest.get('sha256', ''))[:16]}…` "
            f"({zip_manifest.get('compression', 'stored')})"
        )
        lines += ["", "| Archive member | Bytes | sha256 |", "|---|---|---|"]
        for member in zip_manifest.get("members", []):
            lines.append(
                f"| `{member['archive_name']}` | {member['n_bytes']:,} | "
                f"`{member['sha256'][:12]}…` |"
            )
        lines.append(
            "\nThe archive digest is reproducible: the same inputs always produce "
            "the same bytes, so it pins exactly what was uploaded."
        )
        if acted:
            status = data.get("workflow_status")
            lines += ["", f"## Workflow created — status `{status}`", ""]
            lines.append(f"- **Workflow id**: `{data['workflow_id']}`")
            if status == "FAILED":
                reason = data.get("workflow_status_message")
                lines.append("\n**This workflow failed to register and cannot be run.**")
                if reason:
                    lines.append(f"\nAWS's own reason: {reason}")
                else:
                    lines.append(
                        "\nAWS validates the definition server-side — there is no "
                        "lint API to catch this earlier — so check that the "
                        "entrypoint filename matches what the engine expects."
                    )
            else:
                lines.append(
                    f"\nRun it with:\n\n```bash\n--start-run {data['workflow_id']} "
                    f"--workflow-type PRIVATE --params params.json \\\n"
                    f"  --output-uri s3://<bucket>/output/ --role-arn <role> \\\n"
                    f"  --run-name <name> --allow-remote-inputs --confirm-submit\n```"
                )
            lines.append(
                f"\nThis skill cannot delete a workflow — that is barred by "
                f"consequence, not by omission. Remove it with:\n\n```bash\n"
                f"aws omics delete-workflow --id {data['workflow_id']} "
                f"--region {data['region']}\n```"
            )
        else:
            lines += ["", "## Nothing was created.", ""]
            lines.append(
                "Re-run with `--confirm-register` to create this workflow. "
                "Registration bills nothing; running the workflow does."
            )

    lines += ["", "## Provenance", ""] + _provenance_lines(data)
    lines += ["", generate_report_footer().strip(), ""]
    return "\n".join(lines)


def _tag_markdown(data: dict[str, Any]) -> str:
    """Report for tag read/write modes."""
    labels = {
        "tag": "Tagged",
        "untag": "Untagged",
        "tags": "Tags",
        "sync-tags": "Synced Tags",
    }
    verb = labels[data["mode"]]
    header = generate_report_header(
        title=f"AWS HealthOmics — Run {verb}",
        skill_name=SKILL_NAME, skill_version=SKILL_VERSION,
        extra_metadata={"Mode": verb, "Region": data["region"]},
    )
    lines = [header, "", f"## {verb}", ""]
    lines.append(f"**Run**: `{data['run_id']}` (`{data['arn']}`)")
    if data["mode"] == "tag":
        rendered = ", ".join(f"`{k}={v}`" for k, v in sorted(data["tagged"].items()))
        lines.append(f"\nSet: {rendered}")
    elif data["mode"] == "untag":
        lines.append(f"\nRemoved: {', '.join(f'`{k}`' for k in data['untagged'])}")
    elif data["mode"] == "sync-tags":
        if data.get("set"):
            rendered = ", ".join(f"`{k}={v}`" for k, v in sorted(data["set"].items()))
            lines.append(f"\nSet/updated: {rendered}")
        if data.get("removed"):
            lines.append(f"\nRemoved: {', '.join(f'`{k}`' for k in data['removed'])}")
        if not data.get("set") and not data.get("removed"):
            lines.append("\nNo changes were needed.")
    tags = data.get("tags") or data.get("desired_tags") or data.get("tagged") or {}
    if tags:
        lines += ["", "| Key | Value |", "|---|---|"]
        for key, value in sorted(tags.items()):
            lines.append(f"| `{key}` | `{value}` |")
    lines += ["", generate_report_footer().strip(), ""]
    return "\n".join(lines)


def _register_version_markdown(data: dict[str, Any]) -> str:
    """Report for adding a version to an existing workflow."""
    acted = data.get("registered")
    header = generate_report_header(
        title="AWS HealthOmics — Workflow Version",
        skill_name=SKILL_NAME, skill_version=SKILL_VERSION,
        extra_metadata={
            "Mode": "Register version" + ("" if acted else " — DRY RUN"),
            "Region": data["region"],
        },
    )
    zip_manifest = data.get("zip") or {}
    lines = [header, "", "## Version definition", ""]
    lines.append(f"- **Workflow id**: `{data['workflow_id']}`")
    lines.append(f"- **Version name**: `{data['version_name']}`")
    lines.append(f"- **Engine**: {data['engine']}")
    lines.append(
        f"- **Archive**: {zip_manifest.get('n_bytes', 0):,} bytes, "
        f"`{str(zip_manifest.get('sha256', ''))[:16]}…`"
    )
    if acted:
        status = data.get("version_status")
        lines += ["", f"## Version created — status `{status}`", ""]
        if status == "FAILED":
            reason = data.get("version_status_message")
            lines.append("\n**This version failed to register.**")
            if reason:
                lines.append(f"\nAWS's own reason: {reason}")
        else:
            lines.append(
                f"\nRun it with `--start-run {data['workflow_id']} "
                f"--workflow-version-name {data['version_name']} ...`"
            )
    else:
        lines += ["", "## Nothing was created.", ""]
        lines.append("Re-run with `--confirm-register` to create this version.")
    lines += ["", generate_report_footer().strip(), ""]
    return "\n".join(lines)


def _check_markdown(data: dict[str, Any]) -> str:
    """Report for --check."""
    header = generate_report_header(
        title="AWS HealthOmics — Preflight",
        skill_name=SKILL_NAME,
        skill_version=SKILL_VERSION,
        extra_metadata={"Mode": "Preflight", "Region": data["region"]},
    )
    lines = [header, "", "## Checks", ""]
    lines.append(f"Scope: {data.get('scope', 'local')}. "
                 "PASS means the stated check passed; it is not a guarantee of runtime access.")
    lines.append(
        f"{data.get('n_checks', 0)} check(s): "
        f"{data.get('n_failed', 0)} failed, {data.get('n_warnings', 0)} warning(s)."
    )
    lines += ["", "| Check | Result | Severity | Detail |", "|---|---|---|---|"]
    for check in data.get("checks", []):
        result = check.get("status") or ("PASS" if check.get("ok") else "FAIL")
        lines.append(
            f"| `{check.get('name', '')}` | {result} | "
            f"{check.get('severity', '')} | {check.get('detail', '')} |"
        )
    lines += ["", "## Provenance", ""] + _provenance_lines(data)
    lines += ["", generate_report_footer().strip(), ""]
    return "\n".join(lines)


def _workflow_search_markdown(data: dict[str, Any]) -> str:
    """Report for workflow search and recommendation."""
    title = (
        "AWS HealthOmics — Workflow Recommendations"
        if data["mode"] == "workflow-recommendations"
        else "AWS HealthOmics — Workflow Search"
    )
    header = generate_report_header(
        title=title,
        skill_name=SKILL_NAME,
        skill_version=SKILL_VERSION,
        extra_metadata={"Mode": data["mode"], "Region": data["region"]},
    )
    lines = [header, "", f"## Query", "", f"`{data.get('query', '')}`", ""]
    lines.append(f"{data.get('n_items', 0)} workflow(s) matched.")
    lines += ["", "| Score | Id | Name | Status | Type |", "|---|---|---|---|---|"]
    for item in data.get("items", []):
        lines.append(
            f"| {item.get('matchScore', '')} | `{item.get('id', 'n/a')}` | "
            f"{item.get('name', 'n/a')} | {item.get('status', 'n/a')} | "
            f"{item.get('type', item.get('workflowType', 'n/a'))} |"
        )
        if item.get("matchReasons"):
            lines.append("\nReasons: " + "; ".join(item["matchReasons"]) +
                         f". Input compatibility: {item.get('inputCompatibility', 'UNKNOWN')}.\n")
    if data.get("items"):
        first = data["items"][0]
        lines += [
            "",
            "## Next step",
            "",
            "Generate a params skeleton before submitting:",
            "",
            "```bash",
            f"--params-template {first.get('id', '<workflow-id>')} "
            f"--workflow-type {first.get('type', first.get('workflowType', 'PRIVATE'))}",
            "```",
        ]
    lines += ["", "## Provenance", ""] + _provenance_lines(data)
    lines += ["", generate_report_footer().strip(), ""]
    return "\n".join(lines)


def _params_template_markdown(data: dict[str, Any]) -> str:
    """Report for --params-template."""
    header = generate_report_header(
        title="AWS HealthOmics — Params Template",
        skill_name=SKILL_NAME,
        skill_version=SKILL_VERSION,
        extra_metadata={"Mode": "Params template", "Region": data["region"]},
    )
    lines = [header, "", "## Workflow", ""]
    lines += [
        f"- **Workflow id**: `{data.get('workflow_id', 'n/a')}`",
        f"- **Workflow name**: {data.get('workflow_name', 'n/a')}",
        f"- **Workflow type**: {data.get('workflow_type', 'n/a')}",
    ]
    lines += ["", "## Parameters", ""]
    if data.get("items"):
        lines += ["| Name | Default |", "|---|---|"]
        for item in data["items"]:
            lines.append(f"| `{item['name']}` | `{item['default']}` |")
    else:
        lines.append("No parameter template was exposed for this workflow.")
    lines += [
        "",
        "A writable skeleton is in `params.template.json`; use it as the starting params file and fill in real S3/local values before `--start-run`.",
        "",
        "## Provenance",
        "",
    ] + _provenance_lines(data)
    lines += ["", generate_report_footer().strip(), ""]
    return "\n".join(lines)


def _verification_lines(data: dict[str, Any]) -> list[str]:
    """Render what the run actually produced, without overstating it.

    An ETag alone does not establish MD5: encryption and multipart uploads
    change its meaning. Local SHA-256 fingerprints identify downloaded bytes.
    Genomic outputs are routinely multipart, so calling either one "the
    checksum" would put a guarantee in the bundle that does not hold.
    """
    verification = data.get("verification")
    if not verification:
        return []

    deep = verification.get("depth") == "deep"
    lines = ["", "## Outputs", ""]
    lines.append(
        f"{verification['n_objects']} object(s), "
        f"{verification['n_bytes']:,} bytes under `{verification['source']}`."
    )

    if deep:
        if verification.get("complete"):
            downloaded_to = verification.get("downloaded_to", "the requested destination")
            lines.append(
                f"\nEvery listed object was downloaded to "
                f"`{downloaded_to}` and hashed. The SHA-256 values "
                f"below are real checksums of the bytes on disk."
            )
        else:
            lines.append(
                f"\n**{verification['n_missing']} listed object(s) could not be "
                f"retrieved**, so this verification is incomplete: "
                + ", ".join(f"`{k}`" for k in verification.get("missing", [])[:5])
            )
    else:
        lines.append(
            "\nListing only — no bytes were transferred. **The ETag column is "
            "not a checksum guarantee**: encryption/upload evidence is unavailable, "
            "and multipart ETags cannot be compared with a local whole-file MD5. Use "
            "`--verify-outputs deep` for real SHA-256 checksums."
        )

    header = "| Key | Bytes | ETag | MD5? |" + (" SHA-256 |" if deep else "")
    divider = "|---|---|---|---|" + ("---|" if deep else "")
    lines += ["", header, divider]
    for entry in verification["objects"][:50]:
        row = (
            f"| `{entry['key']}` | {entry['size']:,} | `{entry['etag']}` | "
            f"{'yes' if entry.get('is_md5') else 'unverified'} |"
        )
        if deep:
            digest = entry.get("sha256")
            row += f" `{digest[:16]}…`|" if digest else " **missing** |"
        lines.append(row)
    if len(verification["objects"]) > 50:
        lines.append(f"\n…and {len(verification['objects']) - 50} more.")

    return lines


def _provenance_lines(data: dict[str, Any]) -> list[str]:
    """The honest ceiling for work that happened in someone else's account."""
    if data.get("mode") == "check" and data.get("scope", "local") == "local":
        return ["Local validation only. No AWS client was constructed and no AWS API was called."]
    if data["demo"]:
        ceiling = (
            "This report replays a synthetic fixture. No AWS call was made, and "
            "nothing here describes an actual account, run or workflow."
        )
    elif data.get("mode") != "run" or not data["run"].get("id"):
        ceiling = (
            "No AWS HealthOmics run executed as part of this report. Any "
            "identifiers above describe an unsent request or a query result, "
            "not a run that took place."
        )
    else:
        verification = data.get("verification") or {}
        if verification.get("depth") == "deep" and verification.get("complete"):
            outputs = (
                f"Every one of the {verification['n_objects']} output object(s) was "
                f"downloaded and hashed; the sha256 values in tables/outputs.csv are "
                f"real checksums of those bytes, computed here rather than reported "
                f"by AWS."
            )
        elif verification.get("depth") == "deep":
            outputs = (
                f"Output verification is INCOMPLETE: {verification['n_missing']} of "
                f"{verification['n_objects']} listed object(s) could not be "
                f"retrieved, so the checksums below cover only part of the run."
            )
        elif verification.get("depth") == "manifest":
            outputs = (
                f"The {verification['n_objects']} output object(s) were listed but "
                f"not fetched, so no checksum in this bundle covers their bytes — an "
                f"ETag is not one. Use `--verify-outputs deep` for real sha256s."
            )
        else:
            outputs = (
                "No checksum in this bundle covers the run's outputs, which remain "
                "in S3 and were not read. Add `--verify-outputs` to record what the "
                "run produced."
            )
        ceiling = (
            "This run executed in AWS HealthOmics. Replaying it requires the same "
            "account, execution role and container images. The identifiers here "
            f"pin what the run WAS. {outputs}"
        )
    return [
        "- Transport: **boto3** (AWS HealthOmics API directly).",
        f"- Allow-listed operations: {len(ALLOWED_OPERATIONS)} of 107 available.",
        "",
        ceiling,
    ]


_LIST_MODES = {
    "runs",
    "workflows",
    "run-groups",
    "run-caches",
    "workflow-versions",
    "workflow-search",
    "workflow-recommendations",
}


def _report_markdown(data: dict[str, Any]) -> str:
    if data.get("mode") == "check":
        return _check_markdown(data)
    if data.get("mode") in {"workflow-search", "workflow-recommendations"}:
        return _workflow_search_markdown(data)
    if data.get("mode") == "params-template":
        return _params_template_markdown(data)
    if data.get("mode") in _LIST_MODES:
        return _list_markdown(data)
    if data.get("mode") in {"upload", "download", "register"}:
        return _transfer_markdown(data)
    if data.get("mode") in {"tag", "untag", "tags", "sync-tags"}:
        return _tag_markdown(data)
    if data.get("mode") == "register-version":
        return _register_version_markdown(data)

    run = data["run"]
    workflow = data["workflow"]
    header = generate_report_header(
        title="AWS HealthOmics Run Report",
        skill_name=SKILL_NAME,
        skill_version=SKILL_VERSION,
        extra_metadata={
            "Mode": "Synthetic offline demo" if data["demo"] else "Live AWS HealthOmics (boto3)",
            "Region": data["region"],
            "Run status": str(data["run_status"]),
        },
    )
    lines = [header, "", "## Run", ""]
    lines += [
        f"- **Run id**: `{run.get('id', 'n/a')}`",
        f"- **Run name**: {run.get('name', 'n/a')}",
        f"- **Status**: **{data['run_status']}**",
        f"- **Workflow**: `{workflow.get('name', 'n/a')}` "
        f"(`{workflow.get('id', run.get('workflowId', 'n/a'))}`, "
        f"{workflow.get('type', run.get('workflowType', 'n/a'))})",
        f"- **Output URI**: `{run.get('outputUri', 'n/a')}`",
    ]
    if data.get("tags"):
        rendered = ", ".join(f"`{k}={v}`" for k, v in sorted(data["tags"].items()))
        lines.append(f"- **Tags**: {rendered}")
    # Read back rather than echoed: this is what AWS holds, which is the only
    # way to confirm the tags this skill set actually landed.
    if run.get("statusMessage"):
        lines.append(f"- **Status message**: {run['statusMessage']}")

    lines += ["", "## Tasks", ""]
    lines.append(
        f"{data['n_tasks']} task(s): {data['n_completed']} completed, {data['n_failed']} failed."
    )
    if data["n_failed"]:
        lines += ["", "### Failed tasks", ""]
        for task in data["tasks"]:
            if str(task.get("status", "")).upper() not in {"FAILED", "CANCELLED"}:
                continue
            lines.append(
                f"- **{task.get('name', task.get('taskId', 'unknown'))}** "
                f"(`{task.get('taskId', 'n/a')}`) — {task.get('status')}"
            )
            # The reason is the whole point of reading this section. AWS puts
            # it on the task, and omitting it sent users to the console for the
            # one fact they came for.
            reason = task.get("statusMessage") or task.get("failureReason")
            if reason:
                lines.append(f"  - {reason}")
            # Fetched by _enrich_failed_tasks and previously discarded: the log
            # location is the next thing a user reading this section needs.
            log_stream = task.get("logStream")
            if log_stream:
                group, _, stream = log_stream.partition(":log-stream:")
                group_name = group.split(":log-group:")[-1] if ":log-group:" in group else group
                lines.append(f"  - Logs: `{log_stream}`")
                lines.append(
                    f"    ```bash\n    aws logs get-log-events "
                    f"--log-group-name {group_name} --log-stream-name {stream}\n    ```"
                )

    if data.get("start_run_request") is not None:
        lines += ["", "## Submission", ""]
        verb = "Submitted" if data["submitted"] else "NOT submitted (estimate only)"
        lines.append(f"**{verb}.** The exact request:")
        lines += ["", "```json", json.dumps(data["start_run_request"], indent=2), "```"]
        cost = estimated_cost_line(str(data["start_run_request"].get("workflowId", "")))
        if cost and data["start_run_request"].get("workflowType") == "READY2RUN":
            lines += ["", f"**Estimated cost**: {cost}"]
        if not data["submitted"]:
            lines.append(
                "\nNo run was started and nothing was billed. Re-run with "
                "`--confirm-submit` to submit this request."
            )

    output_uri = run.get("outputUri")
    if output_uri:
        # HealthOmics writes each run under <outputUri>/<runId>/. Pointing the
        # command at <outputUri> alone pulls every run this account ever wrote
        # into one directory -- and this is the line users copy verbatim.
        run_id = str(run.get("id", "")).strip()
        prefix = run.get("runOutputUri") or (f"{output_uri.rstrip('/')}/{run_id}/" if run_id else output_uri)
        lines += [
            "", "## Fetching the outputs", "",
            "Outputs stay in S3. Bring this run's outputs down with:",
            "", "```bash",
            f"--download-outputs {run_id or '<run-id>'} --to ./run-{run_id or 'outputs'}/ "
            f"--confirm-download",
            "```",
            "", "or with the AWS CLI directly:", "", "```bash",
            f"aws s3 cp --recursive {prefix} ./run-{run_id or 'outputs'}/",
            "```",
        ]

    lines += _verification_lines(data)
    lines += ["", "## Provenance", ""] + _provenance_lines(data)
    lines += ["", generate_report_footer().strip(), ""]
    return "\n".join(lines)


_LIST_TITLES = {
    "runs": "AWS HealthOmics Runs",
    "workflows": "AWS HealthOmics Workflows",
    "run-groups": "AWS HealthOmics Run Groups",
    "run-caches": "AWS HealthOmics Run Caches",
    "workflow-versions": "AWS HealthOmics Workflow Versions",
    "workflow-search": "AWS HealthOmics Workflow Search",
    "workflow-recommendations": "AWS HealthOmics Workflow Recommendations",
}


def _list_markdown(data: dict[str, Any]) -> str:
    title = _LIST_TITLES[data["mode"]]
    header = generate_report_header(
        title=title,
        skill_name=SKILL_NAME,
        skill_version=SKILL_VERSION,
        extra_metadata={
            "Mode": "Synthetic offline demo" if data["demo"] else "Live AWS HealthOmics (boto3)",
            "Region": data["region"],
        },
    )
    lines = [header, "", f"## {title}", "", f"{data['n_items']} item(s).", ""]
    lines += ["| Id | Name | Status | Type |", "|---|---|---|---|"]
    for item in data["items"]:
        lines.append(
            f"| `{item.get('id', 'n/a')}` | {item.get('name', 'n/a')} | "
            f"{item.get('status', 'n/a')} | "
            f"{item.get('type', item.get('workflowType', 'n/a'))} |"
        )
    lines += ["", "## Provenance", ""] + _provenance_lines(data)
    lines += ["", generate_report_footer().strip(), ""]
    return "\n".join(lines)
