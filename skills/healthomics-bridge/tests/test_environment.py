from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_template_grants_the_complete_documented_ecr_pull_actions():
    template = yaml.safe_load((ROOT / "environment" / "template.yaml").read_text())
    statement = template["Resources"]["Images"]["Properties"]["RepositoryPolicyText"]["Statement"][0]
    assert set(statement["Action"]) == {
        "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"
    }


def test_fixture_environment_is_private_bounded_and_retained():
    template = yaml.safe_load((ROOT / "environment" / "template.yaml").read_text())
    resources = template["Resources"]
    bucket = resources["Artifacts"]
    assert bucket["DeletionPolicy"] == "Retain"
    assert all(bucket["Properties"]["PublicAccessBlockConfiguration"].values())
    assert resources["Images"]["Properties"]["ImageTagMutability"] == "IMMUTABLE"
    assert resources["Images"]["DeletionPolicy"] == "Retain"
    assert resources["RunGroup"]["Properties"]["MaxRuns"] == 1
    assert "Condition" in resources["Images"]["Properties"]["RepositoryPolicyText"]["Statement"][0]


def test_live_smoke_refuses_ready2run_and_requires_explicit_execution(tmp_path):
    from live_smoke import build_commands
    import pytest
    case = {"kind": "wdl", "workflow_id": "1", "workflow_type": "READY2RUN", "params": {},
            "output_uri": "s3://b/out/", "role_arn": "arn:aws:iam::123456789012:role/test", "run_name": "smoke", "expected_greeting": "hello"}
    with pytest.raises(ValueError, match="PRIVATE"):
        build_commands(case, tmp_path)
    case["workflow_type"] = "PRIVATE"
    commands = build_commands(case, tmp_path)
    assert "--confirm-submit" not in commands["check"]
    assert "--live" in commands["check"]
    assert commands["submit"].count("--confirm-submit") == 1


def test_smoke_expectations_are_checked_before_any_aws_call(tmp_path):
    from live_smoke import build_commands
    import pytest
    case = {"kind": "esmfold", "workflow_id": "1", "workflow_type": "PRIVATE", "params": {},
            "output_uri": "s3://b/out/", "role_arn": "arn:aws:iam::123456789012:role/test", "run_name": "smoke"}
    with pytest.raises(ValueError, match="expected_residues"):
        build_commands(case, tmp_path)
