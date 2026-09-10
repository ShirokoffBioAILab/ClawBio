import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "examples/esmfold"


def module():
    spec = importlib.util.spec_from_file_location("esmfold_inference", ROOT / "inference.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.mark.parametrize("sequence", ["", "ACD*", "ACDX", "A" * 81])
def test_smoke_refuses_noncanonical_or_unbounded_sequence(sequence):
    with pytest.raises(ValueError):
        module().validate_sequence(sequence)


def test_smoke_accepts_short_synthetic_sequence():
    assert module().validate_sequence("ACDEFGHIKLMNPQRSTVWY") == "ACDEFGHIKLMNPQRSTVWY"


def test_builder_is_scoped_and_time_bounded():
    import yaml
    template = yaml.safe_load((ROOT / "builder.yaml").read_text())
    properties = template["Resources"]["Builder"]["Properties"]
    assert properties["TimeoutInMinutes"] == 20
    assert properties["ConcurrentBuildLimit"] == 1
    assert properties["Source"]["Type"] == "S3"
