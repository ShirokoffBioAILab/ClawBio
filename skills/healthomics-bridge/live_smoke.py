"""Opt-in private workflow smoke runner; all AWS run calls go through the bridge."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import importlib.util
import hashlib
import re

import healthomics_bridge as bridge
from submission import atomic_json


def validate_run_group(group: dict) -> None:
    limits = {"maxDuration": 20, "maxRuns": 1, "maxCpus": 16, "maxGpus": 1}
    if any(type(group.get(key)) is not int or not 0 < group[key] <= maximum
           for key, maximum in limits.items()):
        raise ValueError("Smoke run group must limit duration to 20 minutes, concurrency to 1, CPUs to 16 and GPUs to 1")


def validate_giab_checksums(downloads: Path) -> bool:
    expected = {"subset.vcf.gz", "subset.vcf.gz.tbi", "stats.txt", "metrics.json", "tool-version.txt"}
    manifests = list(downloads.rglob("checksums.sha256"))
    if len(manifests) != 1 or manifests[0].stat().st_size > 4096:
        return False
    lines = [line.split(maxsplit=1) for line in manifests[0].read_text().splitlines()]
    if len(lines) != len(expected) or any(len(line) != 2 for line in lines):
        return False
    if {name for _, name in lines} != expected:
        return False
    for digest, name in lines:
        matches = list(downloads.rglob(name))
        if not re.fullmatch(r"[a-f0-9]{64}", digest) or len(matches) != 1 or not matches[0].is_file():
            return False
        measured = hashlib.sha256()
        with matches[0].open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                measured.update(chunk)
        if measured.hexdigest() != digest:
            return False
    return True


def build_commands(case: dict, output: Path) -> dict[str, list[str]]:
    if case.get("workflow_type") != "PRIVATE":
        raise ValueError("Smoke cases must explicitly use PRIVATE workflows; no Ready2Run substitution")
    if case.get("kind") not in ("wdl", "esmfold", "giab"):
        raise ValueError("Unknown smoke kind")
    if case["kind"] == "giab":
        expected = case.get("expected", {})
        interval = re.fullmatch(r"[A-Za-z0-9_.]+:(\d+)-(\d+)", str(expected.get("interval", "")))
        if (not interval or not 0 < int(interval[1]) <= int(interval[2])
                or type(expected.get("records")) is not int
                or expected["records"] <= 0 or not expected.get("samples")
                or not isinstance(expected.get("samples"), list)
                or not all(isinstance(s, str) and s for s in expected["samples"])
                or not isinstance(expected.get("reference_headers"), list)
                or not re.fullmatch(r"[a-f0-9]{64}", str(expected.get("records_sha256", "")))):
            raise ValueError("GIAB requires frozen original-file expectations before submission")
    if case["kind"] == "wdl" and not isinstance(case.get("expected_greeting"), str):
        raise ValueError("WDL requires expected_greeting before submission")
    if case["kind"] == "esmfold" and (type(case.get("expected_residues")) is not int or case["expected_residues"] <= 0):
        raise ValueError("ESMFold requires positive expected_residues before submission")
    if case.get("expected_sequence") is not None and (not re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWY]+", str(case["expected_sequence"]))
            or len(case["expected_sequence"]) != case.get("expected_residues")):
        raise ValueError("ESMFold expected_sequence must match the expected_residues count")
    required = ("workflow_id", "params", "output_uri", "role_arn", "run_name")
    if any(key not in case for key in required) or not case["workflow_id"]:
        raise ValueError("Smoke case is missing explicit workflow/run inputs")
    common = ["--start-run", str(case["workflow_id"]), "--workflow-type", "PRIVATE",
              "--params", str(output / "params.json"), "--output-uri", case["output_uri"],
              "--role-arn", case["role_arn"], "--run-name", case["run_name"],
              "--profile", case.get("profile", "default"), "--region", case.get("region", "us-east-1"),
              "--allow-remote-inputs", "--storage-type", "DYNAMIC"]
    if case.get("run_group_id"):
        common += ["--run-group-id", str(case["run_group_id"])]
    if case.get("workflow_version_name"):
        common += ["--workflow-version-name", case["workflow_version_name"]]
    return {"check": [*common, "--check", "--live", "--output", str(output / "check")],
            "preview": [*common, "--output", str(output / "preview")],
            "submit": [*common, "--confirm-submit", "--strict-exit", "--wait", "--wait-timeout-seconds", "1800",
                       "--output", str(output / "run")]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="One explicit public/synthetic smoke case JSON")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm-submit", action="store_true", help="Billable: execute exactly one private case")
    parser.add_argument("--resume", action="store_true", help="Observe an existing receipt; never silently start another run")
    args = parser.parse_args(argv)
    case = json.loads(args.input.read_text(encoding="utf-8"))
    commands = build_commands(case, args.output)
    if not args.resume and args.output.exists() and any(args.output.iterdir()):
        raise FileExistsError("Smoke output exists; preserve its receipt and choose a fresh directory")
    if case.get("data_classification") not in ("public", "synthetic"):
        raise ValueError("Only declared public/synthetic fixtures are allowed; never patient data")
    if args.confirm_submit and not case.get("run_group_id"):
        raise ValueError("Billable smoke tests require an explicitly bounded run group")
    if args.confirm_submit:
        group_output = args.output / ("group-resume" if args.resume else "group")
        code = bridge.main(["--describe-run-group", str(case["run_group_id"]),
            "--profile", case.get("profile", "default"), "--region", case.get("region", "us-east-1"),
            "--output", str(group_output)])
        if code:
            return code
        groups = json.loads((group_output / "result.json").read_text())["data"]["items"]
        if len(groups) != 1:
            raise ValueError("Cannot establish smoke run group limits")
        validate_run_group(groups[0])
    if args.resume:
        if json.loads((args.output / "case.json").read_text()) != case:
            raise ValueError("Smoke case changed; refuse recovery with different expectations")
        receipt = args.output / "run" / "submission.json"
        if not receipt.is_file():
            raise ValueError("No submission receipt; inspect the prior attempt before starting anything")
        observation = args.output / "observation"
        command = ["--resume-from", str(receipt), "--strict-exit", "--wait",
                   "--wait-timeout-seconds", "1800", "--profile", case.get("profile", "default"),
                   "--region", case.get("region", "us-east-1"), "--output", str(observation)]
        if args.confirm_submit:
            command += ["--confirm-submit", "--allow-remote-inputs"]
        code = bridge.main(command)
    else:
        atomic_json(args.output / "case.json", case)
        atomic_json(args.output / "params.json", case["params"])
        atomic_json(args.output / "plan.json", commands)
        for phase in ("check", "preview"):
            code = bridge.main(commands[phase])
            if code:
                return code
        if not args.confirm_submit:
            return 0
        code = bridge.main(commands["submit"])
        observation = args.output / "run"
    if code:
        return code
    result = json.loads((observation / "result.json").read_text())
    if result["summary"].get("run_status") != "COMPLETED":
        return 2
    run_id = result["data"]["run"]["id"]
    attempt = 1
    verification_dir = args.output / "verification"
    downloads = args.output / "downloads"
    while verification_dir.exists() or downloads.exists():
        attempt += 1
        verification_dir = args.output / f"verification-{attempt}"
        downloads = args.output / f"downloads-{attempt}"
    verification = ["--run-status", run_id, "--strict-exit", "--verify-outputs", "deep", "--confirm-download",
        "--to", str(downloads), "--output", str(verification_dir),
        "--profile", case.get("profile", "default"), "--region", case.get("region", "us-east-1")]
    if case["kind"] != "giab":
        verification += ["--validate-smoke", case["kind"]]
    if case["kind"] == "wdl":
        verification += ["--expected-greeting", case["expected_greeting"]]
    elif case["kind"] == "esmfold":
        verification += ["--expected-residues", str(case["expected_residues"])]
        if case.get("expected_sequence"):
            verification += ["--expected-sequence", case["expected_sequence"]]
    code = bridge.main(verification)
    if code:
        return code
    if case["kind"] == "giab":
        spec = importlib.util.spec_from_file_location("giab_qc", Path(__file__).resolve().parent / "examples/giab/giab_qc.py")
        giab = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(giab)
        candidates = list(downloads.rglob("subset.vcf.gz"))
        checks = giab.validate(candidates[0], case["expected"]) if len(candidates) == 1 else {"ok": False}
        hashes_ok = validate_giab_checksums(downloads)
        checks["checksums_ok"] = hashes_ok
        checks["ok"] = checks["ok"] and hashes_ok
        atomic_json(verification_dir / "smoke_validation.json", checks)
    else:
        checks = json.loads((verification_dir / "smoke_validation.json").read_text())
    atomic_json(args.output / "smoke_result.json", {"ok": checks["ok"], "run_id": run_id,
        "verification": str(verification_dir), "case_kind": case["kind"]})
    return 0 if checks["ok"] else 5


if __name__ == "__main__":
    sys.exit(main())
