import json

import pytest


def test_resume_is_a_cli_mode():
    import healthomics_bridge as bridge
    args = bridge.build_parser().parse_args(["--resume-from", "submission.json"])
    assert str(args.resume_from) == "submission.json"


def test_snapshot_strips_confirmation_and_output_only():
    from submission import replay_invocation
    assert replay_invocation(["--check", "--live", "--start-run", "7", "--output", "old", "--confirm-submit"]) == ["--check", "--live", "--start-run", "7"]


def test_receipt_digest_and_context_are_validated(tmp_path):
    from submission import make_receipt, load_receipt
    receipt = make_receipt({"requestId": "original", "workflowId": "w"}, profile="default", region="us-east-1")
    path = tmp_path / "submission.json"
    path.write_text(json.dumps(receipt))
    assert load_receipt(path, profile="default", region="us-east-1")["request"]["requestId"] == "original"
    with pytest.raises(ValueError, match="context"):
        load_receipt(path, profile="default", region="us-west-2")
    receipt["request"]["workflowId"] = "changed"
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="digest"):
        load_receipt(path, profile="default", region="us-east-1")


def test_recovery_never_submits_known_run():
    from submission import recover_submission
    class Client:
        def call(self, *args, **kwargs):
            pytest.fail("Known run must not be resubmitted")
    assert recover_submission(client=Client(), receipt={"state": "SUBMITTED", "run_id": "7"}, confirmed=False) == "7"


def test_uncertain_recovery_requires_confirmation_and_retains_token():
    from submission import recover_submission
    calls = []
    class Client:
        def call(self, operation, **kwargs):
            calls.append((operation, kwargs))
            return {"id": "8"}
    receipt = {"state": "SUBMITTING", "request": {"requestId": "original"}, "run_id": None}
    with pytest.raises(ValueError, match="uncertain"):
        recover_submission(client=Client(), receipt=receipt, confirmed=False)
    assert calls == []
    assert recover_submission(client=Client(), receipt=receipt, confirmed=True) == "8"
    assert calls == [("StartRun", {"requestId": "original"})]
