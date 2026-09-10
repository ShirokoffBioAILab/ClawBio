"""Opt-in private workflow smoke runner; all AWS run calls go through the bridge."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import healthomics_bridge as bridge
from submission import atomic_json


def build_commands(case: dict, output: Path) -> dict[str, list[str]]:
    if case.get("workflow_type") != "PRIVATE":
        raise ValueError("Smoke cases must explicitly use PRIVATE workflows; no Ready2Run substitution")
    if case.get("kind") not in ("wdl", "esmfold"):
        raise ValueError("Unknown smoke kind")
    if case["kind"] == "wdl" and not isinstance(case.get("expected_greeting"), str):
        raise ValueError("WDL requires expected_greeting before submission")
    if case["kind"] == "esmfold" and (type(case.get("expected_residues")) is not int or case["expected_residues"] <= 0):
        raise ValueError("ESMFold requires positive expected_residues before submission")
    required = ("workflow_id", "params", "output_uri", "role_arn", "run_name")
    if any(key not in case for key in required) or not case["workflow_id"]:
        raise ValueError("Smoke case is missing explicit workflow/run inputs")
    common = ["--start-run", str(case["workflow_id"]), "--workflow-type", "PRIVATE",
              "--params", str(output / "params.json"), "--output-uri", case["output_uri"],
              "--role-arn", case["role_arn"], "--run-name", case["run_name"],
              "--profile", case.get("profile", "default"), "--region", case.get("region", "us-east-1"),
              "--allow-remote-inputs"]
    if case.get("run_group_id"):
        common += ["--run-group-id", str(case["run_group_id"])]
    if case.get("workflow_version_name"):
        common += ["--workflow-version-name", case["workflow_version_name"]]
    return {"check": [*common, "--check", "--live", "--output", str(output / "check")],
            "submit": [*common, "--confirm-submit", "--wait", "--wait-timeout-seconds", "3600",
                       "--output", str(output / "run")]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="One explicit public/synthetic smoke case JSON")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm-submit", action="store_true", help="Billable: execute exactly one private case")
    args = parser.parse_args(argv)
    case = json.loads(args.input.read_text(encoding="utf-8"))
    commands = build_commands(case, args.output)
    if args.output.exists() and any(args.output.iterdir()):
        raise FileExistsError("Smoke output exists; preserve its receipt and choose a fresh directory")
    if case.get("data_classification") not in ("public", "synthetic"):
        raise ValueError("Only declared public/synthetic fixtures are allowed; never patient data")
    if args.confirm_submit and not case.get("run_group_id"):
        raise ValueError("Billable smoke tests require an explicitly bounded run group")
    atomic_json(args.output / "params.json", case["params"])
    atomic_json(args.output / "plan.json", commands)
    check_code = bridge.main(commands["check"])
    if check_code or not args.confirm_submit:
        return check_code
    code = bridge.main(commands["submit"])
    if code:
        return code
    result = json.loads((args.output / "run" / "result.json").read_text())
    if result["summary"].get("run_status") != "COMPLETED":
        return 2
    run_id = result["data"]["run"]["id"]
    verification = ["--run-status", run_id, "--verify-outputs", "deep", "--confirm-download",
        "--to", str(args.output / "downloads"), "--output", str(args.output / "verification"),
        "--profile", case.get("profile", "default"), "--region", case.get("region", "us-east-1"),
        "--validate-smoke", case["kind"]]
    if case["kind"] == "wdl":
        verification += ["--expected-greeting", case["expected_greeting"]]
    else:
        verification += ["--expected-residues", str(case["expected_residues"])]
    code = bridge.main(verification)
    if code:
        return code
    checks = json.loads((args.output / "verification" / "smoke_validation.json").read_text())
    return 0 if checks["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
