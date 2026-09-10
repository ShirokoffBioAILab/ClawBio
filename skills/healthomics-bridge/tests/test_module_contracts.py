def test_rendering_has_no_aws_dependency():
    from reporting import _report_markdown
    import healthomics_bridge as bridge
    data = bridge.map_run_report({"run": {"id": "7", "status": "COMPLETED"}}, region="us-east-1")
    assert "COMPLETED" in _report_markdown(data)


def test_monitoring_returns_all_pages():
    from monitoring import fetch_run_bundle
    class Client:
        def call(self, operation, **kwargs):
            if operation == "GetRun":
                return {"id": "7"}
            return {"items": [{"taskId": "1"}]}
    assert fetch_run_bundle(client=Client(), run_id="7")["tasks"] == [{"taskId": "1"}]
