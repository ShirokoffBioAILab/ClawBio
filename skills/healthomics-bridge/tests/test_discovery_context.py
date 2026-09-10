import recommendations


def test_recommendations_explain_compatibility_and_required_parameters():
    items = [{"id": "1", "name": "RNA analysis", "status": "ACTIVE", "engine": "WDL",
              "inputFormats": ["fastq"], "parameterTemplate": {"reads": {"optional": False}}},
             {"id": "2", "name": "RNA analysis", "status": "ACTIVE", "engine": "CWL",
              "inputFormats": ["bam"]}]
    result = recommendations.recommend_workflows(items, "RNA", limit=10, input_format="fastq", engine="WDL")
    assert [item["id"] for item in result["recommendations"]] == ["1"]
    assert result["recommendations"][0]["requiredParameters"] == ["reads"]
    assert result["recommendations"][0]["matchReasons"]


def test_unknown_format_is_not_claimed_compatible():
    result = recommendations.recommend_workflows([{"name": "RNA", "id": "1"}], "RNA", limit=10, input_format="fastq")
    assert result["recommendations"][0]["inputCompatibility"] == "UNKNOWN"
