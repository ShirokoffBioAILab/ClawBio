"""Readiness and durable submission regressions; no AWS calls."""

import base64
import io
import json
import zipfile

import pytest

import healthomics_bridge as bridge


IMAGE = "123456789012.dkr.ecr.us-east-1.amazonaws.com/hello:latest"
WDL = ('version 1.0\ntask Echo { command { echo hi } runtime { docker: "'
       + IMAGE + '" } }\nworkflow Hello { call Echo }')


class AwsError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code, "Message": "test"}}


class Calls:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def call(self, operation, **kwargs):
        self.calls.append((operation, kwargs))
        value = self.responses[operation]
        if isinstance(value, Exception):
            raise value
        return value


def args_for(tmp_path):
    params = tmp_path / "params.json"
    params.write_text('{}')
    return bridge.build_parser().parse_args([
        "--start-run", "1", "--workflow-type", "PRIVATE", "--params", str(params),
        "--role-arn", "arn:aws:iam::123456789012:role/omics",
        "--output-uri", "s3://bucket/output/", "--run-name", "test",
        "--profile", "default", "--region", "us-east-1", "--allow-remote-inputs",
        "--confirm-submit", "--output", str(tmp_path / "out"),
    ])


def test_new_submission_cannot_overwrite_an_existing_receipt(tmp_path, monkeypatch):
    args = args_for(tmp_path)
    args.output.mkdir()
    path = args.output / "submission.json"
    path.write_text('{"run_id":"original"}')
    monkeypatch.setattr(bridge, "build_boto_client", lambda *a: pytest.fail("AWS must not be reached"))
    with pytest.raises(FileExistsError, match="receipt"):
        bridge._run_live(args, args.output)
    assert json.loads(path.read_text())["run_id"] == "original"


def test_wdl_runtime_image_is_discovered_even_with_empty_params():
    pytest.importorskip("WDL")
    import readiness
    images, complete = readiness.workflow_images({"definition": WDL, "engine": "WDL"}, {})
    assert images == [IMAGE]
    assert complete


def test_unresolved_runtime_expression_is_unknown():
    pytest.importorskip("WDL")
    import readiness
    dynamic = WDL.replace('docker: "' + IMAGE + '"', 'docker: select_first(["a", "b"])')
    _, complete = readiness.workflow_images({"definition": dynamic, "engine": "WDL"}, {})
    assert not complete


@pytest.mark.parametrize("method", ["set_repository_policy", "create_repository", "put_image", "delete_repository"])
def test_ecr_mutations_never_dispatch(method):
    import ecr_client
    with pytest.raises(ecr_client.ECRMethodNotAllowed):
        ecr_client.ECROperations(_boto=object()).call(method)


def test_missing_image_is_a_failure_and_access_denied_is_unknown():
    import ecr_client
    for code, expected in [("ImageNotFoundException", "FAIL"), ("AccessDeniedException", "UNKNOWN")]:
        client = Calls({"describe_images": AwsError(code)})
        checks = ecr_client.inspect_image(client=client, uri=IMAGE, region="us-east-1")
        assert checks[0]["status"] == expected
        assert checks[0]["code"] == code


def test_ecr_policy_readability_is_not_service_access():
    import ecr_client
    policy = {"Statement": [{"Effect": "Allow", "Principal": {"AWS": "*"}, "Action": "ecr:*"}]}
    assert ecr_client.policy_evidence(policy)["status"] == "UNKNOWN"
    policy["Statement"][0]["Principal"] = {"Service": "omics.amazonaws.com"}
    assert ecr_client.policy_evidence(policy)["status"] == "PASS"
    policy["Statement"][0]["Condition"] = {"StringEquals": {"aws:SourceAccount": "other"}}
    assert ecr_client.policy_evidence(policy)["status"] == "UNKNOWN"


def test_private_lookup_never_falls_back_to_ready2run(tmp_path):
    import readiness
    args = args_for(tmp_path)
    client = Calls({"GetWorkflow": AwsError("ResourceNotFoundException")})
    result = readiness.run_live_preflight(args, omics=client, ecr=Calls({}), s3=Calls({}))
    assert result["ok"] is False
    assert [kw["type"] for op, kw in client.calls if op == "GetWorkflow"] == ["PRIVATE"]


def test_live_preflight_finds_the_embedded_missing_image(tmp_path):
    pytest.importorskip("WDL")
    import readiness
    args = args_for(tmp_path)
    omics = Calls({"GetWorkflow": {"id": "1", "type": "PRIVATE", "status": "ACTIVE",
                                  "engine": "WDL", "definition": WDL, "parameterTemplate": {}}})
    ecr = Calls({"describe_images": AwsError("RepositoryNotFoundException")})
    s3 = Calls({"list_objects_v2": {}})
    result = readiness.run_live_preflight(args, omics=omics, ecr=ecr, s3=s3)
    assert not result["ok"]
    assert any(c.get("code") == "RepositoryNotFoundException" for c in result["checks"])
    assert all(op != "StartRun" for op, _ in omics.calls)


@pytest.mark.parametrize("state", ["FAIL", "UNKNOWN"])
def test_failed_or_unknown_required_preflight_blocks_start_run(tmp_path, monkeypatch, state):
    import readiness
    args = args_for(tmp_path)
    checks = [readiness.check("image", state, "unavailable")]
    monkeypatch.setattr(bridge, "_live_readiness", lambda *a, **k: readiness.summarize(checks))
    monkeypatch.setattr(bridge, "build_boto_client", lambda *a: object())
    monkeypatch.setattr(bridge, "submit_run", lambda **k: pytest.fail("StartRun reached"))
    result = bridge._run_live(args, args.output)
    assert result["summary"]["kind"] == "check"
    assert result["summary"]["ok"] is False


def test_submission_receipt_survives_interrupted_wait(tmp_path, monkeypatch):
    import readiness
    import submission
    monkeypatch.setattr(submission, "resolve_identity", lambda **kwargs: {"account": "123456789012", "partition": "aws"})
    args = args_for(tmp_path)
    args.wait = True
    monkeypatch.setattr(bridge, "_live_readiness", lambda *a, **k: readiness.summarize([]))
    monkeypatch.setattr(bridge, "build_boto_client", lambda *a: object())
    monkeypatch.setattr(bridge, "submit_run", lambda **k: {"response": {"id": "42", "status": "PENDING"}})
    def interrupted(**kwargs):
        receipt = json.loads((args.output / "submission.json").read_text())
        assert receipt["run_id"] == "42"
        assert receipt["profile"] == "default"
        raise KeyboardInterrupt()
    monkeypatch.setattr(bridge, "wait_for_run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        bridge._run_live(args, args.output)
    assert json.loads((args.output / "result.json").read_text())["summary"]["run_id"] == "42"


def test_full_request_changes_get_distinct_idempotency_tokens(tmp_path):
    args = args_for(tmp_path)
    first = bridge.prepare_start_request(args)
    args.workflow_version_name = "v2"
    second = bridge.prepare_start_request(args)
    assert first["requestId"] != second["requestId"]
    assert second == bridge.prepare_start_request(args)


def test_failed_run_exposes_aws_reason_separately_from_report_success(tmp_path):
    data = bridge.map_run_report({"run": {"id": "42", "status": "FAILED",
                                 "failureReason": "ECR_PERMISSION_ERROR"}}, region="us-east-1")
    result = bridge.write_bundle(tmp_path, data)
    assert result["summary"]["execution_ok"] is False
    assert result["summary"]["failure_reason"] == "ECR_PERMISSION_ERROR"


def test_archive_discovery_does_not_extract_files(tmp_path):
    pytest.importorskip("WDL")
    import readiness
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("main.wdl", WDL)
        archive.writestr("../../escape", "never extracted")
    value = base64.b64encode(stream.getvalue()).decode()
    images, complete = readiness.workflow_images({"engine": "WDL", "definition": value}, {})
    assert complete and images == [IMAGE]
    assert not (tmp_path / "escape").exists()


def test_imports_and_prefix_mappings_remain_incomplete():
    pytest.importorskip("WDL")
    import readiness
    source = WDL.replace("version 1.0", 'version 1.0\nimport "remote.wdl" as lib')
    assert readiness.workflow_images({"engine": "WDL", "definition": source}, {})[1] is False
    workflow = {"engine": "WDL", "definition": WDL,
                "containerRegistryMap": {"registryMappings": [{"upstreamRegistryUrl": "quay.io"}]}}
    assert readiness.workflow_images(workflow, {})[1] is False


def test_unknown_acknowledgement_never_overrides_failure():
    import readiness
    assert readiness.summarize([readiness.check("x", "UNKNOWN", "x")], allow_unknown=True)["ok"]
    assert not readiness.summarize([readiness.check("x", "FAIL", "x")], allow_unknown=True)["ok"]


@pytest.mark.parametrize("platform,expected", [("amd64", "PASS"), ("arm64", "FAIL")])
def test_image_architecture_and_policy_are_checked(platform, expected):
    import ecr_client
    client = Calls({
        "describe_images": {"imageDetails": [{"imageDigest": "sha256:" + "a" * 64}]},
        "get_repository_policy": {"policyText": json.dumps({"Statement": [{
            "Effect": "Allow", "Principal": {"Service": "omics.amazonaws.com"}, "Action": "ecr:*"}]})},
        "batch_get_image": {"images": [{"imageManifest": json.dumps({"manifests": [
            {"platform": {"os": "linux", "architecture": platform}}]})}]},
    })
    checks = ecr_client.inspect_image(client=client, uri=IMAGE, region="us-east-1")
    assert next(c for c in checks if c["name"] == "image_architecture")["status"] == expected
    assert next(c for c in checks if c["name"] == "repository_policy")["status"] == "PASS"


def test_single_image_config_is_verified_by_digest(monkeypatch):
    import hashlib
    import ecr_client
    config = b'{"os":"linux","architecture":"amd64"}'
    digest = "sha256:" + hashlib.sha256(config).hexdigest()
    client = Calls({
        "describe_images": {"imageDetails": [{"imageDigest": "sha256:" + "a" * 64}]},
        "get_repository_policy": AwsError("RepositoryPolicyNotFoundException"),
        "batch_get_image": {"images": [{"imageManifest": json.dumps({"config": {"digest": digest}})}]},
        "get_download_url_for_layer": {"downloadUrl": "https://example.s3.amazonaws.com/config"},
    })
    monkeypatch.setattr(ecr_client, "read_aws_metadata", lambda url: config)
    checks = ecr_client.inspect_image(client=client, uri=IMAGE, region="us-east-1")
    assert next(c for c in checks if c["name"] == "image_architecture")["status"] == "PASS"
    assert next(c for c in checks if c["name"] == "repository_policy")["status"] == "FAIL"


@pytest.mark.parametrize("url", ["http://localhost/a", "https://evil.test/a", "file:///etc/passwd",
                                "https://amazonaws.com.evil.test/a", "https://x.amazonaws.com:123/a"])
def test_metadata_download_rejects_untrusted_urls(url):
    import ecr_client
    with pytest.raises(ValueError):
        ecr_client.read_aws_metadata(url)


def test_caller_s3_denial_is_not_execution_role_denial():
    import s3_client
    client = Calls({"list_objects_v2": AwsError("AccessDenied"), "head_object": AwsError("AccessDenied")})
    checks = s3_client.inspect_run_paths(client=client, params={"input": "s3://bucket/input.fasta"},
                                       output_uri="s3://bucket/output/")
    assert all(c["status"] == "UNKNOWN" and not c["required"] for c in checks)


def test_blocked_cli_exits_nonzero(tmp_path, monkeypatch):
    import readiness
    args = args_for(tmp_path)
    monkeypatch.setattr(bridge, "_live_readiness", lambda *a: readiness.summarize([readiness.check("x", "FAIL", "missing")]))
    monkeypatch.setattr(bridge, "build_boto_client", lambda *a: object())
    assert bridge.main(["--check", "--live", "--start-run", "1", "--workflow-type", "PRIVATE",
                        "--params", str(args.params), "--role-arn", args.role_arn,
                        "--output-uri", args.output_uri, "--run-name", "trial", "--output", str(args.output)]) == 2


def test_service_error_code_not_misclassified_as_s3():
    import error_codes
    assert error_codes.error_code_for_exception(AwsError("AccessDeniedException")) == "AccessDeniedException"


def test_private_esmfold_search_does_not_recommend_an_unrelated_active_workflow():
    import recommendations
    assert recommendations.search_workflows([
        {"id": "1", "name": "hello-world", "type": "PRIVATE", "status": "ACTIVE"},
    ], "ESMFold", limit=10) == []
