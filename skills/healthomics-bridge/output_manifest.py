"""Conservative local output semantics and synthetic smoke assertions."""
from __future__ import annotations

import gzip
import math
from pathlib import Path
from typing import Any


def describe_output(entry: dict[str, Any], *, provenance: dict[str, Any]) -> dict[str, Any]:
    result = {**entry, "sample_ids": [], "reference": None, "role": "unknown",
              "qc": {"status": "not_assessed"}, "provenance": provenance,
              "suggested_skills": []}
    local = entry.get("local_path") or entry.get("path")
    if not local:
        return result
    path = Path(local)
    if path.name.lower().endswith((".vcf", ".vcf.gz")):
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8") as handle:
                # Headers only, bounded even for malformed or compressed inputs.
                for _ in range(10000):
                    line = handle.readline(65537)
                    if not line or len(line) > 65536:
                        break
                    if line.startswith("##reference="):
                        result["reference"] = line.strip().split("=", 1)[1]
                    if line.startswith("#CHROM\t"):
                        result.update(role="variants", sample_ids=line.rstrip().split("\t")[9:],
                                      suggested_skills=["variant-annotation"])
                        break
        except (OSError, UnicodeError):
            result["qc"] = {"status": "metadata_unreadable"}
    elif path.suffix.lower() in (".pdb", ".cif"):
        result["role"] = "protein_structure"
    return result


def validate_smoke_outputs(kind: str, paths: list[Path], *, expected_residues: int | None = None,
                           expected_greeting: str | None = None,
                           expected_sequence: str | None = None) -> dict[str, Any]:
    """Structural sanity only, never a claim of scientific prediction accuracy."""
    checks = []
    if kind == "wdl":
        if expected_greeting is None:
            raise ValueError("WDL validation requires the exact expected greeting")
        checks = [{"path": str(p), "ok": p.is_file() and p.stat().st_size < 65536
                   and p.read_text(encoding="utf-8").strip() == expected_greeting}
                  for p in paths if p.name == "greeting.txt"]
    elif kind == "esmfold":
        if expected_residues is None or expected_residues <= 0:
            raise ValueError("ESMFold validation requires the expected input residue count")
        from Bio.PDB import PDBParser, MMCIFParser
        from Bio.SeqUtils import seq1
        for path in paths:
            if path.suffix.lower() not in (".pdb", ".cif"):
                continue
            try:
                parser = PDBParser(QUIET=True) if path.suffix.lower() == ".pdb" else MMCIFParser(QUIET=True)
                structure = parser.get_structure("smoke", str(path))
                residues = [r for r in structure.get_residues() if r.id[0] == " "]
                sequence = "".join(seq1(r.resname) for r in residues)
                sequence_ok = expected_sequence is None or sequence == expected_sequence
                backbone_ok = all("CA" in residue for residue in residues)
                atoms = list(structure.get_atoms())
                finite = bool(atoms) and all(math.isfinite(float(v)) for atom in atoms
                                            for v in (*atom.coord, atom.bfactor))
                checks.append({"path": str(path), "residues": len(residues),
                               "sequence": sequence, "sequence_checked": expected_sequence is not None,
                               "sequence_ok": sequence_ok, "alpha_carbons_present": backbone_ok,
                               "finite_coordinates_and_b_factors": finite,
                               "ok": finite and backbone_ok and sequence_ok and len(residues) == expected_residues})
            except Exception as exc:
                checks.append({"path": str(path), "ok": False, "error": type(exc).__name__})
    else:
        raise ValueError("Unknown smoke workflow kind")
    return {"kind": kind, "checks": checks, "ok": bool(checks) and all(c["ok"] for c in checks),
            "scope": "output sanity, not scientific accuracy"}
