"""Regression coverage for transfer safety and complete run provenance."""
from pathlib import Path

import pytest

import healthomics_bridge as bridge
import s3_client as s3


class DownloadClient:
    def __init__(self):
        self.calls = []

    def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        Path(kwargs["Filename"]).write_text("complete")


@pytest.mark.parametrize("key", ["out/../escape", "out//absolute", "elsewhere/file", "out/a/../../escape"])
def test_download_rejects_unsafe_keys(tmp_path, key):
    client = DownloadClient()
    result = s3.download_objects(client=client, bucket="b", objects=[{"key": key}],
                                 key_prefix="out/", destination=tmp_path / "download")
    assert result["n_failed"] == 1
    assert client.calls == []


def test_download_refuses_symlink_and_existing_file(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "link").symlink_to(outside, target_is_directory=True)
    (dest / "existing").write_text("original")
    client = DownloadClient()
    result = s3.download_objects(client=client, bucket="b",
        objects=[{"key": "out/link/file"}, {"key": "out/existing"}],
        key_prefix="out/", destination=dest)
    assert result["n_failed"] == 2
    assert client.calls == []
    assert (dest / "existing").read_text() == "original"


def test_etag_without_encryption_evidence_is_not_md5():
    assert s3.describe_etag("a" * 32)["is_md5"] is False


def test_tasks_paginate_and_resolve_exact_version():
    calls = []
    class Client:
        def call(self, operation, **kwargs):
            calls.append((operation, kwargs))
            if operation == "GetRun":
                return {"workflowId": "w", "workflowType": "PRIVATE", "workflowVersionName": "v1"}
            if operation == "ListRunTasks":
                return {"items": [{"taskId": "2"}]} if kwargs.get("nextToken") else {"items": [{"taskId": "1"}], "nextToken": "page2"}
            if operation == "GetWorkflowVersion":
                return {"name": "version-one"}
            raise AssertionError(operation)
    bundle = bridge.fetch_run_bundle(client=Client(), run_id="7")
    assert len(bundle["tasks"]) == 2
    assert bundle["workflow"]["name"] == "version-one"
    assert ("GetWorkflowVersion", {"workflowId": "w", "versionName": "v1"}) in calls


def test_live_replay_preserves_invocation():
    invocation = ["--check", "--live", "--start-run", "7", "--workflow-type", "PRIVATE"]
    assert bridge._replay_args("check", {"invocation": invocation}) == invocation


def test_upload_duplicate_basenames_refused_before_transfer(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    for directory in (a, b):
        (directory / "same.txt").write_text("x")
    client = DownloadClient()
    with pytest.raises(ValueError, match="basename"):
        s3.upload_files(client=client, sources=[a / "same.txt", b / "same.txt"], destination="s3://b/in/")
    assert client.calls == []
