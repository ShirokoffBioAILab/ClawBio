---
name: healthomics-bridge
description: >-
  Submit, monitor and import AWS HealthOmics genomics runs through the boto3
  API directly, behind an allow-listed client, a fail-closed data-egress gate
  and an estimate-first cost gate.
license: MIT
metadata:
  version: 0.2.0
  author: Dmitry Shirokov
  domain: bioinformatics
  tags:
    - aws
    - healthomics
    - cloud
    - boto3
    - workflow-execution
    - provenance
  inputs:
    - name: params
      type: file
      format:
        - json
      description: Workflow parameters for a run submission
      required: false
    - name: run_id
      type: string
      format:
        - string
      description: An existing HealthOmics run identifier to report on
      required: false
  outputs:
    - name: report
      type: file
      format:
        - md
      description: Human-readable run report with task outcomes and provenance
    - name: result
      type: file
      format:
        - json
      description: Machine-readable envelope for downstream chaining
    - name: tasks
      type: file
      format:
        - csv
      description: Per-task table with status, resources and timings
  dependencies:
    python: ">=3.11"
    packages:
      - boto3>=1.34
      - miniwdl>=1.12
  demo_data:
    - path: skills/healthomics-bridge/tests/fixtures/demo_run_bundle.json
      description: Synthetic API responses driving the fully offline demo
  endpoints:
    cli: python skills/healthomics-bridge/healthomics_bridge.py --run-status {run_id} --output {output_dir}
  openclaw:
    always: false
    emoji: "🧬"
    homepage: https://github.com/ClawBio/ClawBio
    os:
      - darwin
      - linux
    install:
      - kind: uv
        command: uv pip install boto3 miniwdl
    trigger_keywords:
      - AWS HealthOmics
      - HealthOmics run
      - submit Ready2Run workflow
      - omics run status
      - start genomics workflow on AWS
---

# AWS HealthOmics Bridge

Inspect, submit, monitor and import a HealthOmics run. Use the narrow service
adapters, not unrestricted boto3 calls. Provisioning and policy changes belong
to the separate [environment component](environment/README.md).

## Quick Start

```bash
uv run --frozen python skills/healthomics-bridge/healthomics_bridge.py --demo --output /tmp/healthomics-demo
```

The demo is entirely offline. Live dependencies: boto3 and miniwdl.
Use `--profile default --region us-east-1` when requested; environment variables
are optional, and credentials are resolved by boto3, never copied into artifacts.

## Trigger

Fire when asked to submit, inspect, monitor, recover or download an AWS
HealthOmics run; discover private/Ready2Run workflows; or inspect run readiness.

Do not fire for local Nextflow execution, general S3/ECR administration, image
publication, bucket/repository/IAM policy changes, or clinical interpretation.
Use the environment component for a reviewed infrastructure setup.

## Scope

One run lifecycle, including its input/output transfers and workflow registration.
The bridge does not create buckets, publish images, delete resources, cancel runs,
or alter shared policies. Opt-in CloudWatch reads and AWS Run Analyzer support
run diagnosis; they do not provide a dollar-denominated spending cap.

## Core Capabilities

- Offline demo and local validation; live fail-closed readiness before submission.
- Explicit PRIVATE/READY2RUN type, workflow version, image/resource provenance.
- Complete task pagination and bounded failed-task enrichment.
- Account/partition-bound submission receipt, journal and token-preserving recovery.
- Safe atomic downloads, opaque ETags and local SHA-256 fingerprints.
- Conservative VCF sample/reference handoffs; public/synthetic smoke assertions.
- Contextual recommendations, parameter templates and run tags.
- Bounded run/engine/task logs with cursors and optional AWS Run Analyzer adapter.
- Unified hello-WDL, GIAB and private ESMFold smoke harness with strict exit codes.

## Workflow

1. **Resolve mode (prescriptive):** use exactly one operation. `--demo` must
   return before credential resolution or network access.
2. **Inspect (prescriptive):** read [READINESS.md](READINESS.md) for live checks.
   Required FAIL blocks; required UNKNOWN blocks unless explicitly acknowledged.
   Caller access and policy evidence never prove effective execution-role access.
3. **Preview (prescriptive):** `--start-run` requires explicit type, parameters,
   role, name and output prefix. Require `--allow-remote-inputs`; no patient data.
   Without `--confirm-submit`, produce a request preview, not a run.
4. **Submit (prescriptive):** persist `submission.json` before StartRun; persist
   its returned run ID before polling. Never regenerate a token during recovery.
5. **Observe (prescriptive):** poll or resume by receipt/run ID. Timeout and Ctrl-C
   stop observation, not AWS execution or billing.
6. **Import (prescriptive):** use the run's authoritative output URI when present.
   Download only after confirmation, into a fresh directory.
7. **Report (flexible):** include failures, provenance, incomplete checks and the
   disclaimer. Do not interpret a successful report as a successful workflow.

## Inputs and Commands

All commands also accept `--output`, `--profile`, and `--region`.
Use `--help` for the full stable CLI; see [OPERATIONS.md](OPERATIONS.md) for
recovery, diagnostics, verification and smoke testing.
The complete [CLI reference](CLI_REFERENCE.md) preserves every parser option,
defaults, storage/cache/version behavior, and mode-specific output tables.
See [documentation audit](DOCUMENTATION_AUDIT.md) for where the original details live.

| Intent | Flags |
|---|---|
| Local validation | `--check` |
| Live validation | `--check --live --start-run ID` plus submission inputs |
| Discover workflows | `--list-workflows --workflow-type PRIVATE` |
| Search | `--search-workflows QUERY --workflow-type PRIVATE` |
| Recommend | `--recommend-workflow TASK --input-format fastq --recommend-engine WDL` |
| Parameter skeleton | `--params-template ID --workflow-type PRIVATE` |
| Submit preview | `--start-run ID --workflow-type PRIVATE --params p.json --role-arn ARN --output-uri s3://BUCKET/outputs/ --run-name NAME --allow-remote-inputs` |
| Observe | `--run-status ID [--wait] [--logs] [--analyze-run]` |
| Recover | `--resume-from path/to/submission.json` |
| Upload | `--upload-inputs FILE... --to s3://BUCKET/inputs/ --allow-remote-inputs --confirm-upload` |
| Download | `--download-outputs ID --to DIR --confirm-download` |
| Verify | `--run-status ID --verify-outputs manifest` |
| Register | `--register main.wdl --workflow-name NAME --confirm-register` |
| Version | `--register main.wdl --workflow-id ID --new-version-name VERSION --confirm-register` |
| Tags | `--list-tags ID`, `--tag-run ID --tags JSON`, `--untag-run ID --tag-keys KEY...`, `--sync-tags ID --tags JSON` |

## Algorithm / Methodology

HealthOmics operations are allowlisted in `omics_client.py`. S3 managed
transfers are method-allowlisted, not individual-API allowlisted. ECR inspection
reads image metadata/configuration, not layers; policy writes are excluded.
miniwdl parses archive-local imports and evaluates a restricted expression subset.
Other engines, scatter/conditional image contexts and unresolved expressions
remain explicitly UNKNOWN. Read [READINESS.md](READINESS.md) for coverage.

Run status uses GetRun, every ListRunTasks page, exact GetWorkflowVersion when
reported, and best-effort GetRunTask details for up to 25 failures. Runtime
resource digests are preserved. Discovery ranks metadata; absent format or
parameter evidence is UNKNOWN, not a compatibility claim.

## Example Output

The offline command above produces `report.md`, `result.json` and task tables.
Historical real outputs are in [EXAMPLE.md](EXAMPLE.md); their success does not
prove the environment still exists. [COMPARISON.md](COMPARISON.md) separates
peer comparisons, test evidence and remaining limitations.
The [2026-09-10 retest](LIVE_RETEST_20260910.md) records fresh CPU runs, recovery,
logging repairs and the still-pending private ESMFold execution.

Historical captured run excerpt (not a newly submitted run):

```text
Run id: 7049640
Run name: clawbio-boto3-r2r-verify
Status: COMPLETED
Workflow: ESMFold for up to 800 residues (1830181, READY2RUN)
Tasks: 2 completed, 0 failed
```

This is Ready2Run evidence only. It does not validate a private ESMFold workflow.
The separate [private ESMFold example](examples/esmfold/README.md) documents the
pinned model, explicit build boundary, budget plan and validation contract;
preparation alone must not be presented as a successful GPU run.

A real public CPU example is available in [GIAB variant QC](examples/giab/README.md):
a private WDL subsets the versioned HG002 benchmark and checks its results against
independently measured variant/genotype fingerprints. It uses no private patient
data and is not a variant-calling or clinical-accuracy benchmark.

## Output Structure

```text
report.md
result.json
tables/
reproducibility/commands.sh
reproducibility/replay_manifest.json
reproducibility/params.snapshot.json  # submission/check inputs, when supplied
environment.json                    # live readiness evidence
readiness.json
submission.json
submission.journal.jsonl
run_state.json
outputs.json                        # verification/download
handoff.json
logs.json                           # --logs
analysis.json                       # --analyze-run
smoke_validation.json               # --validate-smoke
```

Generated replay commands omit confirmation flags. Live checks retain their
scope; copied parameter snapshots preserve submitted values. Treat parameter
files, logs, sample identifiers and reports as potentially sensitive artifacts.

## Dependencies

Python >=3.11 for the ClawBio project. Live operations require `boto3>=1.34`;
WDL inspection requires `miniwdl>=1.12`. Protein structure smoke validation uses
ClawBio's Biopython dependency. Optional diagnostics use `aws-healthomics-tools`.
No cloud dependency or AWS environment variable is required to discover the skill
or run its offline demo.

## Safety

- No patient/genetic uploads. Live smoke cases must be public or synthetic.
- AWS execution, storage, requests and transfers may incur charges. Preview
  estimates are not quotes; unknown private-run costs must remain unknown.
- Only explicitly confirmed submission/transfer/registration may mutate AWS.
  Tag operations are explicit metadata mutations.
- Existing download files and symlink paths are refused. Use a fresh destination.
- ETags are opaque without upload/encryption evidence. A local SHA-256 identifies
  downloaded bytes; it is not automatically verified against an AWS checksum.
- Receipt checksums detect accidental changes, not malicious edits. New receipts
  bind the STS-resolved account/partition; recovery rechecks them as well as
  profile/region. Legacy unbound receipts require manual run-ID inspection.
- Include: ClawBio is a research and educational tool. It is not a medical device
  and does not provide clinical diagnoses. Consult a healthcare professional
  before making any medical decisions.

## Gotchas

- You will want to substitute Ready2Run ESMFold when PRIVATE is requested. Do not.
- You will want to treat exit 0 as execution success. Do not: inspect `summary.execution_ok`,
  `summary.run_status` and `smoke_validation.json`, or use `--strict-exit` in automation.
- You will want to retry an uncertain submission with a fresh token. Do not. Inspect AWS runs and
  use the original receipt; idempotency is not an indefinite guarantee.
- Do not widen ECR policies merely to get PASS; scoped conditions are supported.
- Do not call a polling timeout a budget limit. Apply run-group limits separately.
- Do not infer expression analysis from a generic CSV or TSV filename.
- STATIC storage rounds up to 1,200 GiB or multiples of 2,400 GiB, not arbitrary
  1,200 GiB increments. Requesting 5,000 GiB becomes 7,200 GiB. See the CLI reference.
- The bridge cannot cancel runs. An operator can use `aws omics cancel-run --id ID`
  with the original profile/region after separately authorizing cancellation.

## Agent Boundary

The agent chooses the mode, explains consequences and interprets evidence.
The bridge executes validated operations and records results. Infrastructure
changes require a separately reviewed environment deployment; passing readiness
is not authorization to provision, publish images, or run arbitrary workflows.

## Chaining Partners

`variant-annotation` receives locally inspected VCF metadata. Other downstream
skills require explicit output-role/sample mappings; generic table suffixes are
not sufficient. AWS Run Analyzer is optional diagnostic tooling, not a replacement
for the bridge or a scientific validation engine.

## Maintenance

Review service-model drift, miniwdl resolution coverage, SDK behavior and dated
pricing snapshots. Keep offline, read-only live and billable end-to-end evidence
separate. Re-run native OpenClaw eligibility when metadata changes.
Retire unsupported modes explicitly rather than silently changing workflow type.

## Citations

- [AWS GetRun](https://docs.aws.amazon.com/omics/latest/api/API_GetRun.html)
- [AWS ECR permissions](https://docs.aws.amazon.com/omics/latest/dev/permissions-ecr.html)
- [S3 integrity](https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html)
- [OpenClaw skills](https://docs.openclaw.ai/tools/skills)
