import pytest


def test_logs_are_bounded_and_use_only_returned_run_streams():
    from observability import fetch_run_logs
    calls = []
    class Client:
        def get_log_events(self, **kwargs):
            calls.append(kwargs)
            return {"events": [{"message": "failure", "timestamp": 1}], "nextForwardToken": "same"}
    result = fetch_run_logs(client=Client(), run={"id": "7", "logLocation": {"runLogStream": "run/7"}}, limit=10)
    assert result["events"][0]["message"] == "failure"
    assert len(calls) == 2
    assert all(c["logStreamName"] == "run/7" and c["limit"] <= 10 for c in calls)


def test_missing_log_location_does_not_guess_streams():
    from observability import fetch_run_logs
    assert fetch_run_logs(client=object(), run={"id": "7"}, limit=10)["status"] == "UNAVAILABLE"
