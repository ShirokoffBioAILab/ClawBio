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
Schema-3 receipts check the request digest and bind the resolved AWS account and
partition. Schema-1/2 receipts remain locally readable, but automatic recovery is
refused because they are unbound; inspect their recorded ID with `--run-status`.
Never edit receipt requests, and preserve the original directory.

Receipts contain region/profile and account/partition, not credentials. Recovery
resolves STS identity again before Omics access and refuses an account/partition
change. Do not assume AWS remembers tokens forever.
Intentional new runs need a new run name and fresh output directory.

## Logs and Costs

```bash
python skills/healthomics-bridge/healthomics_bridge.py \
  --run-status RUN_ID --logs --log-limit 100 --analyze-run \
  --profile default --region us-east-1 --output output/diagnostics
```

`--logs` reads at most 1,000 events, with at most 10 pages per stream, from the
run/engine streams returned by GetRun and task streams for up to 25 returned tasks.
It never enumerates unrelated account streams. Per-stream errors preserve other
streams' evidence. `logs.json` contains cursors and bounded failure-code evidence.
Use `--log-cursors previous/logs.json` to continue; its run/profile/region must
match. Optional `--log-start-time-ms` and `--log-end-time-ms` bound the UTC epoch
millisecond window (end exclusive). Retrieval remains explicitly incomplete.
Logs can contain sensitive application output; review before sharing artifacts.
Unavailable logs do not invalidate the run report.

`--analyze-run` invokes the optional `aws-healthomics-tools run_analyzer` CLI.
Install the tested version `aws-healthomics-tools==0.13.2` explicitly to enable it.
The adapter passes profile/region explicitly and records CSV row/column metadata
in `analysis.json`, retaining `analysis.csv`; estimates do not impose spending limits. Run-group duration and
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
`kind` (`wdl`, `giab` or `esmfold`), `workflow_type` (`PRIVATE`), `workflow_id`, `params`,
`role_arn`, `output_uri`, `run_name`, `data_classification` (`public` or `synthetic`).
Use `profile: default`, the explicit region, a pinned `workflow_version_name`,
and a bounded `run_group_id`. For WDL include `expected_greeting`; for ESMFold
include the public/synthetic input's `expected_residues` and `expected_sequence`
to verify residue identity as well as count. GIAB requires frozen `expected`
values for interval, samples, reference_headers, records and records_sha256;
derive these from the original public VCF, not the workflow's output.

```bash
python skills/healthomics-bridge/live_smoke.py \
  --input private-wdl-case.json --output output/wdl-check
```

Without `--confirm-submit`, this checks live readiness and writes a submission
preview and plan. With confirmation, it verifies the group has at most 20 minutes,
one concurrent run, 16 CPUs and one GPU, submits exactly one case with DYNAMIC
storage, observes for up to 30 minutes, downloads outputs and validates them.
Those resource limits are not a monetary cap. Run WDL first, then the separate private ESMFold
case. No patient data and no PRIVATE-to-READY2RUN fallback.

Use `--resume` with the same case and original output directory to recover the
receipt and revalidate without another StartRun. A changed case is refused.
Only an uncertain submission needs the additional explicit confirmation after
manual reconciliation. Repeated verification uses fresh numbered directories.
The harness enables `--strict-exit` and writes `smoke_result.json` after validation.

WDL asserts exact `greeting.txt` content. GIAB validates record/genotype fingerprints,
samples, reference headers, interval bounds and the complete five-file checksum set.
The index checksum establishes integrity, not independent index semantics.
ESMFold uses Biopython to check residue count, supplied sequence identity, alpha
carbons and finite coordinates/B-factors in PDB/mmCIF output. These are structural
sanity tests, not evidence that the predicted structure is biologically correct.

### Exit Codes

Default report mode retains compatibility: a reported failed AWS run can exit 0.
Use `--strict-exit`: completed execution exits 0, failed/cancelled/deleted exits 3,
and nonterminal execution or polling timeout exits 4. A readiness block exits 2,
explicit output-validation failure exits 5, and other errors exit 1. Ctrl-C does
not cancel AWS execution. Always preserve the receipt even after a nonzero exit.

## Test Commands

```bash
uv run --frozen --with pytest --with miniwdl python -m pytest skills/healthomics-bridge/tests -q
AWS_PROFILE=default CLAWBIO_RUN_LIVE_HEALTHOMICS=1 uv run --frozen --with pytest --with miniwdl --with boto3 python -m pytest skills/healthomics-bridge/tests/test_live_integration.py -q
```

The second command is read-only; it does not prove either private smoke workflow
can execute. Native OpenClaw eligibility should be checked with an isolated
configuration pointing `agents.defaults.workspace` at this checkout, so unrelated
user plugins/configuration cannot affect the result.
