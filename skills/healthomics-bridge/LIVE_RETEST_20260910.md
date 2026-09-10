# Reliability Retest: 2026-09-10

Scope: requested next steps 1-5, 8 and 10. Baseline was committed as `36fcdf5`.
Steps 1-5 and 8 are implemented and tested. Step 10 has a prepared private ESMFold
example; its cloud source upload/build and GPU execution remain blocked pending
explicit approval of the five-file upload to the existing smoke bucket. The $5
ESMFold ceiling is approved, but does not bypass the separate egress review.

## Fresh Private Runs

Default AWS profile, account 792957228577, us-east-1. These are newly executed
PRIVATE workflows, not old successes or Ready2Run substitutes.

| Case | Workflow/version | Fresh run | Result |
|---|---|---|---|
| Synthetic hello WDL | 1067143 / cpu-repair-20260906 | 4200749 | COMPLETED; exact greeting matched |
| Public GIAB HG002 QC | 4064935 | 8940989 | COMPLETED; all record and checksum assertions passed |
| Private ESMFold | Prepared WDL, not registered | None | Not executed; upload approval pending |

Both CPU runs used the immutable previously verified CPU image, DYNAMIC storage,
and run group 5137532 (one concurrent run, 20 minutes, 16 CPUs, one GPU maximum).
The group limits were fetched and checked by the harness before submission.
Readiness passed without an uncertainty override: hello had 18 checks, GIAB 21.
Caller/effective-role distinctions remain explicit; completed runs provide dated
execution evidence, not a permanent permissions guarantee.

Hello output was `hello-healthomics-reliability-20260910`. Its schema-3 receipt
contains the STS account/partition and original request token. Repeating the
harness with `--resume` observed run 4200749 and revalidated into a new directory,
without another StartRun.

GIAB output contained 193 records, all within chr22:20000000-20100000, sample
HG002, with unchanged expected reference-header metadata (empty in this source).
The original-source-derived variant/genotype fingerprint matched:
`01757eea00ea4d358da4866da07226dc85e3b7b6523c7d0a4428c33dd3a258ec`.
All five distinct expected checksum entries matched downloaded files. The test
does not independently validate tabix index semantics or clinical accuracy.

## Live-Discovered Repairs

The execution role lacked `logs:DescribeLogStreams`. An inspected CloudFormation
change set modified only that role's policy, without replacing resources or
changing shared buckets/repositories. The fresh hello run exposed all three
run/engine/task stream types after repair.

The Analyzer adapter attempted to write a CSV before creating the fresh report
directory. A failing regression reproduced this and the fix creates the parent
before invoking AWS tools. The corrected adapter, tested with tools 0.13.2,
produced a two-row CSV for hello, containing run and task resource/cost estimates.
The fresh GIAB run also produced a successful two-row Analyzer CSV.
Profile/region are now explicit subprocess arguments. Earlier FAILED analysis
artifacts are retained rather than overwritten or represented as successes.

## Test Evidence

- Offline bridge plus catalog: 294 passed, seven gated live tests skipped.
- Opt-in read-only AWS API suite: seven passed, default profile.
- Isolated Python 3.10, 3.11 and 3.12: each 264 passed, seven live tests skipped.
  Initial isolated environments omitted shared ClawBio dependencies; reruns with
  pandas and opentelemetry-sdk included passed without changing project dependencies.
- ESMFold WDL: miniwdl typecheck passed; GPU inference has not run.
- Both CloudFormation templates: cfn-lint passed.
- Skill frontmatter validator: passed; catalog consistency test: passed.
- Regression coverage includes typed missing/denied File inputs, default File
  resolution, account/partition mismatch, legacy receipts, strict execution exits,
  task-log cursors/errors, bounded smoke groups, checksum duplicates, protein
  sequence mismatch and fresh Analyzer output directories.

## Artifacts and Cost

Local evidence root: `output/healthomics/reliability-20260910/`.
Key directories: `hello/`, `giab/`, `hello-diagnostics-fixed/`, and `giab-diagnostics/`.
Each harness directory retains its case, parameters, plan, preview, readiness,
submission receipt, run report, downloaded outputs and smoke validation.
These generated artifacts are intentionally not added to Git.

The hello Analyzer task row estimates $0.001913; this is not an AWS invoice or
the full account/storage bill. No ESMFold CodeBuild project/build or GPU run has
been launched in this retest. The proposed combined build/compute estimate and
retention caveats are in [the ESMFold example](examples/esmfold/README.md).
Existing S3/ECR artifacts and logs remain retained and may continue accruing
storage charges. No automatic deletion or cancellation was performed.

ClawBio is a research and educational tool. It is not a medical device and does
not provide clinical diagnoses. Consult a healthcare professional before making
any medical decisions.
