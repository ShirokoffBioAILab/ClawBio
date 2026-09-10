"""Scoped policy and archive-only WDL resolution regressions."""

import base64
import io
import zipfile

import pytest

import ecr_client
import readiness

ARN = "arn:aws:omics:us-east-1:111122223333:workflow/123"
IMAGE = "111122223333.dkr.ecr.us-east-1.amazonaws.com/tools/hello:v1"


def policy(condition):
    return {"Statement": [{"Effect": "Allow", "Principal": {"Service": "omics.amazonaws.com"},
                           "Action": "ecr:*", "Resource": "*", "Condition": condition}]}


@pytest.mark.parametrize("operator,value", [("StringEquals", ARN), ("ArnEquals", [ARN]),
                                            ("ArnLike", "arn:aws:omics:us-east-1:111122223333:workflow/*")])
def test_scoped_policy_evidence(operator, value):
    result = ecr_client.policy_evidence(policy({operator: {"AWS:SourceArn": value},
        "StringEquals": {"aws:SourceAccount": "444455556666"}}) if operator != "StringEquals"
        else policy({operator: {"AWS:SourceArn": value, "aws:SourceAccount": "444455556666"}}),
        source_arn=ARN, source_account="444455556666")
    assert result["status"] == "PASS"
    assert "not effective authorization" in result["detail"]


@pytest.mark.parametrize("condition", [
    {"StringEquals": {"aws:SourceAccount": "111122223333"}},
    {"ArnLike": {"aws:SourceArn": "arn:aws:omics:other:*:workflow/*"}},
    {"StringEqualsIfExists": {"aws:SourceAccount": "444455556666"}},
    {"StringEquals": {"aws:PrincipalOrgID": "o-example"}},
    {"ArnLike": {"aws:SourceArn": "arn:aws:omics:us-east-1:[1]*:workflow/*"}},
])
def test_uncertain_or_mismatched_scope_stays_unknown(condition):
    assert ecr_client.policy_evidence(policy(condition), source_arn=ARN,
        source_account="444455556666")["status"] == "UNKNOWN"


def test_deny_and_notresource_never_certify_policy():
    for extra in ({"Effect": "Deny"}, {"NotResource": "anything"}):
        value = policy({})
        value["Statement"].append({**value["Statement"][0], **extra})
        assert ecr_client.policy_evidence(value)["status"] == "UNKNOWN"


def bundle(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, source in files.items():
            archive.writestr(name, source)
    return base64.b64encode(stream.getvalue()).decode()


TASK = '''version 1.0
task Echo { input { String image } command { echo hi } runtime { docker: image } }
'''
MAIN = '''version 1.0
import "lib/task.wdl" as lib
workflow Hello { input { String repository String tag = "v1" }
 String image = repository + ":" + tag
 call lib.Echo { input: image = image }
}
'''


def test_local_import_and_parameter_bindings():
    pytest.importorskip("WDL")
    workflow = {"engine": "WDL", "definition": bundle({"main.wdl": MAIN, "lib/task.wdl": TASK})}
    assert readiness.workflow_images(workflow, {"Hello.repository": IMAGE.rsplit(":", 1)[0]}) == ([IMAGE], True)


@pytest.mark.parametrize("uri", ["https://example.com/task.wdl", "/tmp/task.wdl", "../task.wdl",
                                 "file:///tmp/task.wdl", "lib/../../task.wdl", "lib\\task.wdl"])
def test_unsafe_imports_never_read_external_sources(uri, monkeypatch):
    pytest.importorskip("WDL")
    monkeypatch.setattr(ecr_client, "read_aws_metadata", lambda *a: pytest.fail("network read"))
    workflow = {"engine": "WDL", "definition": bundle({"main.wdl": MAIN.replace("lib/task.wdl", uri),
                                                           uri: TASK})}
    assert readiness.workflow_images(workflow, {})[1] is False


@pytest.mark.parametrize("engine", ["CWL", "NEXTFLOW", "WDL_LENIENT"])
def test_unsupported_engine_remains_unknown(engine):
    assert not readiness.workflow_images({"engine": engine, "definition": TASK}, {})[1]


def test_registry_mapping_and_exact_override():
    pytest.importorskip("WDL")
    source = TASK.replace('docker: image', 'docker: "quay.io/tools/hello:v1"')
    workflow = {"engine": "WDL", "arn": ARN, "definition": source,
        "containerRegistryMap": {"registryMappings": [{"upstreamRegistryUrl": "quay.io",
                                                        "ecrRepositoryPrefix": "cache"}]}}
    expected = IMAGE.replace("/tools/", "/cache/tools/")
    assert readiness.workflow_images(workflow, {}) == ([expected], True)
    workflow["containerRegistryMap"]["imageMappings"] = [{"sourceImage": "quay.io/tools/hello:v1",
                                                           "destinationImage": IMAGE}]
    assert readiness.workflow_images(workflow, {}) == ([IMAGE], True)


def test_runtime_file_read_is_not_evaluated(monkeypatch):
    pytest.importorskip("WDL")
    source = TASK.replace("docker: image", 'docker: read_string("/tmp/image")')
    assert readiness.workflow_images({"engine": "WDL", "definition": source}, {})[1] is False


def test_live_policy_context_uses_workflow_owner_and_subscriber_separately(tmp_path, monkeypatch):
    import healthomics_bridge as bridge
    class Calls:
        def __init__(self, responses):
            self.responses = responses

        def call(self, operation, **kwargs):
            return self.responses[operation]
    params = tmp_path / "params.json"
    params.write_text("{}")
    args = bridge.build_parser().parse_args([
        "--start-run", "123", "--workflow-type", "PRIVATE", "--params", str(params),
        "--role-arn", "arn:aws:iam::123456789012:role/omics", "--output-uri", "s3://bucket/out/",
        "--run-name", "test", "--profile", "default", "--region", "us-east-1",
        "--allow-remote-inputs", "--workflow-version-name", "v1"])
    seen = []
    def inspect(**kwargs):
        seen.append(kwargs)
        return []
    monkeypatch.setattr(ecr_client, "inspect_image", inspect)
    workflow = {"id": "1", "type": "PRIVATE", "status": "ACTIVE", "arn": ARN,
                "engine": "WDL", "definition": TASK.replace("docker: image", f'docker: "{IMAGE}"')}
    version = {**workflow, "arn": ARN + "/version/v1"}
    readiness.run_live_preflight(args, omics=Calls({"GetWorkflow": workflow, "GetWorkflowVersion": version}),
                                ecr=Calls({}), s3=Calls({"list_objects_v2": {}}))
    assert seen[0]["source_arn"] == ARN
    assert seen[0]["source_account"] == "123456789012"


def test_interpolation_and_multiple_call_overrides():
    pytest.importorskip("WDL")
    main = MAIN.replace('repository + ":" + tag', '"~{repository}:~{tag}"').replace(
        "call lib.Echo { input: image = image }", 'call lib.Echo { input: image = image }\n'
        'call lib.Echo as Second { input: image = "quay.io/other:v2" }')
    workflow = {"engine": "WDL", "definition": bundle({"main.wdl": main, "lib/task.wdl": TASK})}
    assert readiness.workflow_images(workflow, {"repository": IMAGE.rsplit(":", 1)[0]}) == (
        sorted([IMAGE, "quay.io/other:v2"]), True)


@pytest.mark.parametrize("source", [MAIN.replace('lib/task.wdl', 'missing.wdl'),
    MAIN.replace('call lib.Echo { input: image = image }',
                 'scatter (n in [1,2]) { call lib.Echo { input: image = image } }')])
def test_missing_import_and_scatter_remain_unknown(source):
    pytest.importorskip("WDL")
    assert not readiness.workflow_images({"engine": "WDL", "definition": bundle({"main.wdl": source,
        "lib/task.wdl": TASK})}, {"repository": IMAGE})[1]


def test_registry_mapping_without_context_is_unknown():
    source = TASK.replace("docker: image", 'docker: "quay.io/tools/hello:v1"')
    assert not readiness.workflow_images({"engine": "WDL", "definition": source,
        "containerRegistryMap": {"registryMappings": [{"upstreamRegistryUrl": "quay.io",
                                                         "ecrRepositoryPrefix": "cache"}]}}, {})[1]


def test_nested_local_import_and_subworkflow():
    pytest.importorskip("WDL")
    root = '''version 1.0
import "lib/sub.wdl" as sub
workflow Root { input { String image } call sub.Child { input: image = image } }
'''
    child = '''version 1.0
import "task.wdl" as tasks
workflow Child { input { String image } call tasks.Echo { input: image = image } }
'''
    workflow = {"engine": "WDL", "main": "root.wdl", "definition": bundle({
        "root.wdl": root, "lib/sub.wdl": child, "lib/task.wdl": TASK})}
    assert readiness.workflow_images(workflow, {"Root.image": IMAGE}) == ([IMAGE], True)
    assert not readiness.workflow_images(workflow, {})[1]


def test_import_cycle_remains_unknown():
    pytest.importorskip("WDL")
    source = 'version 1.0\nimport "main.wdl" as cycle\nworkflow Empty {}'
    assert not readiness.workflow_images({"engine": "WDL", "definition": source}, {})[1]


def test_conflicting_registry_mappings_remain_unknown():
    pytest.importorskip("WDL")
    source = TASK.replace("docker: image", 'docker: "quay.io/tools/hello:v1"')
    workflow = {"engine": "WDL", "arn": ARN, "definition": source,
        "containerRegistryMap": {"registryMappings": [
            {"upstreamRegistryUrl": "quay.io", "ecrRepositoryPrefix": "one"},
            {"upstreamRegistryUrl": "quay.io", "ecrRepositoryPrefix": "two"}]}}
    assert not readiness.workflow_images(workflow, {})[1]


def test_scoped_policy_requires_explicit_context():
    value = policy({"StringEquals": {"aws:SourceArn": ARN, "aws:SourceAccount": "111122223333"}})
    assert ecr_client.policy_evidence(value)["status"] == "UNKNOWN"
    assert ecr_client.policy_evidence(value, source_arn=ARN)["status"] == "UNKNOWN"


def test_inspect_image_forwards_scope_to_policy_evidence():
    import json
    class Client:
        def call(self, operation, **kwargs):
            return {
                "describe_images": {"imageDetails": [{"imageDigest": "sha256:" + "a" * 64}]},
                "get_repository_policy": {"policyText": json.dumps(policy({"StringEquals": {
                    "aws:SourceArn": ARN, "aws:SourceAccount": "444455556666"}}))},
                "batch_get_image": {"images": [{"imageManifest": json.dumps({"manifests": [
                    {"platform": {"os": "linux", "architecture": "amd64"}}]})}]},
            }[operation]
    result = ecr_client.inspect_image(client=Client(), uri=IMAGE, region="us-east-1",
                                      source_arn=ARN, source_account="444455556666")
    assert next(c for c in result if c["name"] == "repository_policy")["status"] == "PASS"
