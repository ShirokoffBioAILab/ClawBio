import json
from pathlib import Path

import pytest


def test_typed_wdl_files_exclude_strings_and_include_defaults():
    from readiness import workflow_file_inputs
    source = '''version 1.0
    workflow w {
      input { File required File? optional String prefix File defaulted = "s3://b/default" }
      output { File out = required }
    }'''
    uris, complete = workflow_file_inputs({"engine": "WDL", "definition": source},
        {"required": "s3://b/file", "prefix": "s3://b/prefix/"})
    assert complete
    assert uris == {"s3://b/file", "s3://b/default"}


@pytest.mark.parametrize("code,status", [("404", "FAIL"), ("AccessDenied", "UNKNOWN")])
def test_missing_typed_file_is_required(code, status):
    from s3_client import inspect_run_paths
    class Error(Exception):
        response = {"Error": {"Code": code}}
    class Client:
        def call(self, name, **kwargs):
            if name == "head_object":
                raise Error()
            return {}
    checks = inspect_run_paths(client=Client(), params={"f": "s3://b/f", "prefix": "s3://b/p/"},
        output_uri="s3://b/out/", file_uris={"s3://b/f"})
    file_check = next(c for c in checks if "s3://b/f:" in c["detail"])
    assert file_check["required"] and file_check["status"] == status
    assert not next(c for c in checks if "prefix input" in c["detail"])["required"]


def test_receipt_binds_account_and_partition(tmp_path):
    from submission import make_receipt, load_receipt, verify_identity
    identity = {"account": "123456789012", "partition": "aws"}
    receipt = make_receipt({"requestId": "token"}, profile="default", region="us-east-1", identity=identity)
    path = tmp_path / "r.json"
    path.write_text(json.dumps(receipt))
    saved = load_receipt(path, profile="default", region="us-east-1")
    verify_identity(saved, identity)
    with pytest.raises(ValueError, match="account"):
        verify_identity(saved, {**identity, "account": "999999999999"})
    with pytest.raises(ValueError, match="account"):
        verify_identity(saved, {**identity, "partition": "aws-cn"})


def test_legacy_receipt_cannot_be_retried_unbound():
    from submission import verify_identity
    with pytest.raises(ValueError, match="legacy"):
        verify_identity({"schema_version": 2}, {"account": "123456789012", "partition": "aws"})


@pytest.mark.parametrize("state,strict,validation,expected", [
    ("FAILED", False, None, 0), ("FAILED", True, None, 3),
    ("RUNNING", True, None, 4), ("COMPLETED", True, None, 0),
    ("COMPLETED", False, {"ok": False}, 5)])
def test_execution_exit_contract(state, strict, validation, expected):
    from healthomics_bridge import result_exit_code
    result = {"summary": {"run_status": state}, "data": {"smoke_validation": validation}}
    assert result_exit_code(result, strict=strict) == expected


def test_task_logs_resume_and_preserve_other_stream_errors():
    from observability import fetch_run_logs
    calls = []
    class Client:
        def get_log_events(self, **kwargs):
            calls.append(kwargs)
            if kwargs["logStreamName"] == "run/7":
                raise RuntimeError("unavailable")
            return {"events": [{"timestamp": 2, "message": "task evidence"}], "nextForwardToken": "cursor"}
    result = fetch_run_logs(client=Client(), run={"id": "7", "logLocation": {"run": "run/7"}},
        tasks=[{"taskId": "8"}], cursors={"run/7/task/8": "previous"}, limit=2,
        start_time=1, end_time=3)
    assert result["events"]
    assert result["errors"]
    assert result["cursors"]["run/7/task/8"] == "cursor"
    task_call = next(c for c in calls if c["logStreamName"] == "run/7/task/8")
    assert task_call["nextToken"] == "previous" and task_call["startTime"] == 1


def test_giab_case_requires_frozen_expectations(tmp_path):
    from live_smoke import build_commands
    case = {"kind": "giab", "workflow_type": "PRIVATE", "workflow_id": "1", "params": {},
        "output_uri": "s3://b/out/", "role_arn": "r", "run_name": "n",
        "expected": {"interval": "chr22:1-10", "records": 2, "samples": ["HG002"],
                     "records_sha256": "a" * 64, "reference_headers": []}}
    commands = build_commands(case, tmp_path)
    assert "--strict-exit" in commands["submit"]
    assert "--confirm-submit" not in commands["preview"]
    assert "--storage-type" in commands["submit"]


def test_giab_duplicate_checksums_do_not_pass(tmp_path):
    from live_smoke import validate_giab_checksums
    import hashlib
    path = tmp_path / "subset.vcf.gz"
    path.write_bytes(b"data")
    line = hashlib.sha256(b"data").hexdigest() + "  subset.vcf.gz\n"
    (tmp_path / "checksums.sha256").write_text(line * 5)
    assert not validate_giab_checksums(tmp_path)


def test_protein_sequence_must_match_not_just_count(tmp_path):
    from output_manifest import validate_smoke_outputs
    path = tmp_path / "prediction.pdb"
    path.write_text("ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00 80.00           C\nEND\n")
    assert validate_smoke_outputs("esmfold", [path], expected_residues=1, expected_sequence="A")["ok"]
    assert not validate_smoke_outputs("esmfold", [path], expected_residues=1, expected_sequence="G")["ok"]


def test_analyzer_passes_explicit_identity_context(tmp_path, monkeypatch):
    import observability
    from types import SimpleNamespace
    monkeypatch.setattr(observability.shutil, "which", lambda _: "/tools/aws-healthomics-tools")
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        Path(command[command.index("-o") + 1]).write_text("taskId,status\n1,COMPLETED\n")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(observability.subprocess, "run", run)
    result = observability.run_analyzer(run_id="7", output=tmp_path / "fresh" / "a.csv", region="us-east-1", profile="default")
    assert "--profile=default" in calls[0]
    assert "--region=us-east-1" in calls[0]
    assert result["rows"] == 1


@pytest.mark.parametrize("group", [{"maxDuration": 21, "maxRuns": 1, "maxGpus": 1, "maxCpus": 16},
    {"maxDuration": 20, "maxRuns": 2, "maxGpus": 1, "maxCpus": 16}, {}])
def test_smoke_rejects_unbounded_or_larger_run_groups(group):
    from live_smoke import validate_run_group
    with pytest.raises(ValueError, match="run group"):
        validate_run_group(group)


def test_smoke_accepts_existing_small_run_group():
    from live_smoke import validate_run_group
    validate_run_group({"maxDuration": 20, "maxRuns": 1, "maxGpus": 1, "maxCpus": 16})


def test_giab_invalid_expectations_block_before_cloud(tmp_path):
    from live_smoke import build_commands
    case = {"kind": "giab", "workflow_type": "PRIVATE", "workflow_id": "1", "params": {},
        "output_uri": "s3://b/out/", "role_arn": "r", "run_name": "n",
        "expected": {"interval": "nonsense", "records": 2, "samples": ["HG002"],
                     "records_sha256": "z" * 64}}
    with pytest.raises(ValueError, match="expectations"):
        build_commands(case, tmp_path)
