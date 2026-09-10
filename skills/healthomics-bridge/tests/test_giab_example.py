import gzip
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def giab():
    spec = importlib.util.spec_from_file_location("giab_qc", ROOT / "examples" / "giab" / "giab_qc.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def vcf(tmp_path):
    path = tmp_path / "fixture.vcf.gz"
    with gzip.open(path, "wt") as stream:
        stream.write('##fileformat=VCFv4.2\n##reference=GRCh38\n'
                     '#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tHG002\n'
                     'chr22\t99\t.\tA\tG\t.\tPASS\t.\tGT\t0/1\n'
                     'chr22\t100\t.\tA\tG\t.\tPASS\t.\tGT\t0/1\n'
                     'chr22\t200\t.\tAT\tA\t.\tPASS\t.\tGT\t1/1\n'
                     'chr22\t201\t.\tC\tT\t.\tPASS\t.\tGT\t0/1\n')
    return path


def test_summary_uses_inclusive_pos_interval_and_sample(giab, vcf):
    result = giab.summarize(vcf, "chr22:100-200")
    assert result["records"] == 2
    assert result["total_records"] == 4
    assert result["samples"] == ["HG002"]
    assert len(result["records_sha256"]) == 64


@pytest.mark.parametrize("interval", ["chr22:200-100", "chr22:0-100", "chr22:1-2;echo bad", "22"])
def test_invalid_intervals_refused(giab, interval):
    with pytest.raises(ValueError):
        giab.parse_interval(interval)


def test_validation_detects_outside_records_and_changed_genotype(giab, vcf, tmp_path):
    expected = giab.summarize(vcf, "chr22:100-200")
    assert not giab.validate(vcf, expected)["ok"]
    subset = tmp_path / "subset.vcf.gz"
    with gzip.open(vcf, "rt") as source, gzip.open(subset, "wt") as target:
        for line in source:
            if line.startswith("#") or line.split("\t")[1] in ("100", "200"):
                target.write(line)
    assert giab.validate(subset, expected)["ok"]
    with gzip.open(subset, "rt") as stream:
        text = stream.read()
    with gzip.open(subset, "wt") as stream:
        stream.write(text.replace("1/1", "0/0"))
    assert not giab.validate(subset, expected)["ok"]


def test_missing_header_is_not_a_valid_empty_vcf(giab, tmp_path):
    path = tmp_path / "empty.vcf.gz"
    with gzip.open(path, "wt") as stream:
        stream.write("##fileformat=VCFv4.2\n")
    with pytest.raises(ValueError, match="header"):
        giab.summarize(path, "chr22:100-200")
