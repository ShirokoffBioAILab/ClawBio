def test_vcf_samples_and_reference_are_read_not_guessed(tmp_path):
    from output_manifest import describe_output
    path = tmp_path / "arbitrary.vcf"
    path.write_text("##fileformat=VCFv4.2\n##reference=GRCh38\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE1\n")
    result = describe_output({"path": str(path)}, provenance={"run_id": "7"})
    assert result["sample_ids"] == ["SAMPLE1"]
    assert result["reference"] == "GRCh38"
    assert result["provenance"]["run_id"] == "7"


def test_generic_csv_is_not_inferred_as_expression(tmp_path):
    from output_manifest import describe_output
    path = tmp_path / "something.csv"
    path.write_text("a,b\n1,2\n")
    result = describe_output({"path": str(path)}, provenance={})
    assert result["role"] == "unknown"
    assert result["suggested_skills"] == []


def test_protein_structure_smoke_checks_residues(tmp_path):
    from output_manifest import validate_smoke_outputs
    path = tmp_path / "fold.pdb"
    path.write_text("ATOM      1  CA  ALA A   1      11.000  12.000  13.000  1.00 90.00           C\n")
    assert validate_smoke_outputs("esmfold", [path], expected_residues=1)["ok"]
    assert not validate_smoke_outputs("esmfold", [path], expected_residues=2)["ok"]
