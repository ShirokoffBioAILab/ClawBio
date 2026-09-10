# Recovery, Diagnostics and Validation

## Recover a Receipt

```bash
python skills/healthomics-bridge/healthomics_bridge.py \
  --resume-from output/previous/submission.json \
  --profile default --region us-east-1 --output output/recovered
```

A known run ID is observed without StartRun. An uncertain SUBMITTING receipt is
refused by default. Inspect recent runs in the original account first; only then
explicitly add `--confirm-submit --allow-remote-inputs` to retry the unchanged
saved request/token after fresh readiness. This is one attempt, not a retry loop.
Schema-1 receipts remain readable; schema-2 receipts additionally check a request
digest. Never edit receipt requests, and preserve the original directory.

Receipts contain region/profile, not credentials. Profiles can be reconfigured;
verify the account before recovery. Do not assume AWS remembers tokens forever.
Intentional new runs need a new run name and fresh output directory.

## Logs and Costs

```bash
python skills/healthomics-bridge/healthomics_bridge.py \
  --run-status RUN_ID --logs --log-limit 100 --analyze-run \
  --profile default --region us-east-1 --output output/diagnostics
```

`--logs` reads at most 1,000 events, with bounded pages, from the run/engine
streams returned by GetRun. It never enumerates unrelated account streams.
Logs can contain sensitive application output; review before sharing artifacts.
Unavailable logs do not invalidate the run report.

`--analyze-run` invokes the optional `aws-healthomics-tools run_analyzer` CLI.
Install that package explicitly to enable it. Its result is recorded in
`analysis.json`; estimates do not impose spending limits. Run-group duration and
resource limits belong to environment setup. A wait timeout never cancels a run.

Sources: [AWS log locations](https://docs.aws.amazon.com/omics/latest/dev/monitoring-cloudwatch-logs.html),
[AWS tools](https://github.com/awslabs/aws-healthomics-tools).

## Output Integrity and Handoff

Use a fresh download directory. Existing paths, traversal keys and symlink paths
are refused. Complete temporary downloads are atomically published without
overwriting destination files. A failed object remains explicit in the report.

ETags are opaque without encryption/upload evidence. Deep verification computes
local SHA-256 fingerprints, not automatic comparison with an authoritative AWS
checksum. VCF headers supply sample IDs/reference when present; absent metadata
stays null/empty. QC remains `not_assessed` unless an actual validator ran.

## Private End-to-End Smoke Tests

`live_smoke.py` accepts one JSON case per invocation. Required fields:
`kind` (`wdl` or `esmfold`), `workflow_type` (`PRIVATE`), `workflow_id`, `params`,
`role_arn`, `output_uri`, `run_name`, `data_classification` (`public` or `synthetic`).
Use `profile: default`, the explicit region, a pinned `workflow_version_name`,
and a bounded `run_group_id`. For WDL include `expected_greeting`; for ESMFold
include the public input's `expected_residues`.

```bash
python skills/healthomics-bridge/live_smoke.py \
  --input private-wdl-case.json --output output/wdl-check
```

Without `--confirm-submit`, this only checks live readiness and writes a plan.
With confirmation, it submits exactly one case, waits up to one hour, downloads
outputs and validates them. Run WDL first, then the separate private ESMFold
case. No patient data and no PRIVATE-to-READY2RUN fallback.

WDL asserts exact `greeting.txt` content. ESMFold uses Biopython to check residue
count and finite coordinates/B-factors in PDB/mmCIF output. These are structural
sanity tests, not evidence that the predicted structure is biologically correct.

## Test Commands

```bash
uv run --frozen --with pytest --with miniwdl python -m pytest skills/healthomics-bridge/tests -q
AWS_PROFILE=default CLAWBIO_RUN_LIVE_HEALTHOMICS=1 uv run --frozen --with pytest --with miniwdl --with boto3 python -m pytest skills/healthomics-bridge/tests/test_live_integration.py -q
```

The second command is read-only; it does not prove either private smoke workflow
can execute. Native OpenClaw eligibility should be checked with an isolated
configuration pointing `agents.defaults.workspace` at this checkout, so unrelated
user plugins/configuration cannot affect the result.
