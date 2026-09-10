"""Independent record-preservation checks for the fixed public GIAB smoke example.

bcftools performs VCF processing. This bounded-purpose reader fingerprints
selected CHROM/POS/REF/ALT/FORMAT/sample fields; it is not a general VCF validator.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
from pathlib import Path
import re


def parse_interval(value: str) -> tuple[str, int, int]:
    match = re.fullmatch(r"([A-Za-z0-9_.]+):(\d+)-(\d+)", value)
    if not match:
        raise ValueError("Expected contig:start-end interval")
    chrom, start, end = match[1], int(match[2]), int(match[3])
    if start < 1 or end < start:
        raise ValueError("Interval coordinates must be positive and ordered")
    return chrom, start, end


def summarize(path: Path, interval: str) -> dict:
    chrom, start, end = parse_interval(interval)
    digest = hashlib.sha256()
    samples = None
    reference = []
    records = total = 0
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", newline="") as stream:
        for row in csv.reader(stream, delimiter="\t"):
            if not row:
                continue
            if row[0].startswith("##reference="):
                reference.append(row[0].split("=", 1)[1])
            if row[0] == "#CHROM":
                samples = row[9:]
                continue
            if row[0].startswith("#"):
                continue
            if samples is None or len(row) != 9 + len(samples):
                raise ValueError("VCF header/record shape mismatch")
            pos = int(row[1])
            total += 1
            if row[0] == chrom and start <= pos <= end:
                canonical = [row[0], str(pos), row[3], row[4], *row[8:]]
                digest.update((json.dumps(canonical, separators=(",", ":")) + "\n").encode())
                records += 1
    if not samples:
        raise ValueError("VCF sample header is missing")
    return {"schema_version": 1, "interval": interval, "samples": samples,
            "reference_headers": reference, "records": records,
            "total_records": total, "records_sha256": digest.hexdigest()}


def validate(path: Path, expected: dict) -> dict:
    actual = summarize(path, expected["interval"])
    checks = {key: actual[key] == expected[key]
              for key in ("samples", "reference_headers", "records", "records_sha256")}
    checks["no_outside_records"] = actual["total_records"] == actual["records"]
    checks["nonempty"] = actual["records"] > 0
    return {"ok": all(checks.values()), "checks": checks, "actual": actual}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", default="chr22:20000000-20100000")
    parser.add_argument("--expected", type=Path)
    args = parser.parse_args(argv)
    result = (validate(args.input, json.loads(args.expected.read_text())) if args.expected
              else summarize(args.input, args.interval))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    return 0 if result.get("ok", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
