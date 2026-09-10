# Complete CLI Reference

Generated from the current parser on 2026-09-05. No command below is executed by reading this document.
Confirmation flags remain explicit. Environment-dependent defaults are described below.

| Flag | Purpose / Choices | Default |
|---|---|---|
| `--demo` | Offline demo; no AWS account needed | `False` |
| `--logs` | Fetch bounded run/engine CloudWatch logs | `False` |
| `--log-limit` | See operational details below. | `100` |
| `--analyze-run` | Invoke optional AWS Run Analyzer | `False` |
| `--input-format` | Declared input format to match in workflow recommendations | `None` |
| `--recommend-engine` | See operational details below. Choices: WDL, CWL, NEXTFLOW, WDL_LENIENT | `None` |
| `--validate-smoke` | Validate downloaded synthetic/public smoke outputs Choices: wdl, esmfold | `None` |
| `--expected-residues` | See operational details below. | `None` |
| `--expected-greeting` | See operational details below. | `None` |
| `--check` | Run read-only preflight checks and exit before any live action | `False` |
| `--live` | With --check: inspect the workflow and run prerequisites in AWS | `False` |
| `--allow-unverified-readiness` | Acknowledge required UNKNOWN readiness checks; never overrides FAIL | `False` |
| `--output` | Output directory | `output/healthomics` |
| `--resume-from` | Recover the original submission receipt without regenerating its token | `None` |
| `--list-runs` | List recent runs (read-only) | `False` |
| `--list-workflows` | List workflows (read-only) | `False` |
| `--run-status` | Report one run (read-only) | `None` |
| `--start-run` | Submit a run (gated) | `None` |
| `--upload-inputs` | Upload a run's input files to S3 (gated) | `None` |
| `--download-outputs` | Download one run's outputs from S3 (gated) | `None` |
| `--register` | Register a WDL/CWL/Nextflow definition as a private workflow (gated). Dry-run without --confirm-register. With --workflow-id instead of --workflow-name, adds a version to an existing workflow. | `None` |
| `--list-run-groups` | List run groups referenced by --run-group-id (read-only) | `False` |
| `--list-run-caches` | List run caches referenced by --cache-id (read-only) | `False` |
| `--describe-run-group` | One run group's own detail (read-only) | `None` |
| `--describe-run-cache` | One run cache's own detail (read-only) | `None` |
| `--list-workflow-versions` | List a workflow's versions (read-only) | `None` |
| `--search-workflows` | Search workflows by name, description, id and type | `None` |
| `--recommend-workflow` | Recommend workflows for a plain-English task | `None` |
| `--params-template` | Write a starter params.template.json for a workflow | `None` |
| `--tag-run` | Set tags on an existing run (gated). Needs --tags. | `None` |
| `--untag-run` | Remove tags from a run by key (gated). Needs --tag-keys. | `None` |
| `--list-tags` | List tags on a run (read-only) | `None` |
| `--sync-tags` | Converge a run's tags to --tags JSON | `None` |
| `--workflow-type` | Workflow type. REQUIRED with --start-run: AWS needs it to resolve a Ready2Run workflow, and being explicit avoids the not-found error a missing type produces. Also filters --list-workflows. Choices: PRIVATE, READY2RUN | `None` |
| `--params` | JSON parameters file (with --start-run) | `None` |
| `--output-uri` | S3 URI for run outputs (with --start-run) | `None` |
| `--role-arn` | HealthOmics execution role ARN (with --start-run) | `None` |
| `--run-name` | Run name (with --start-run) | `None` |
| `--to` | Destination: an s3:// URI for --upload-inputs, a local directory for --download-outputs | `None` |
| `--confirm-upload` | Actually upload. Without it --upload-inputs is a dry run. | `False` |
| `--confirm-download` | Actually download. S3 egress is billable. | `False` |
| `--verify-outputs` | With --run-status: record what the run produced. 'manifest' lists sizes and ETags and moves no bytes; 'deep' downloads and computes real SHA-256 checksums (needs --confirm-download). Choices: manifest, deep | `None` |
| `--workflow-name` | Name for the new workflow (with --register) | `None` |
| `--engine` | Workflow engine. Inferred from the definition's extension (.wdl/.cwl/.nf) when omitted. Choices: CWL, NEXTFLOW, WDL, WDL_LENIENT | `None` |
| `--additional-files` | Extra files for a multi-file WDL/CWL bundle | `[]` |
| `--description` | Workflow description (with --register) | `None` |
| `--parameter-template` | JSON parameter template (with --register) | `None` |
| `--allow-duplicate-name` | Register even though a workflow of this name exists | `False` |
| `--confirm-register` | Actually create the workflow (or version). Without it --register is a dry run. | `False` |
| `--workflow-id` | Existing workflow id to add a version to (with --register) | `None` |
| `--new-version-name` | Version name to create (with --register --workflow-id) | `None` |
| `--tags` | JSON tags to set (with --tag-run or --sync-tags) | `None` |
| `--tag-keys` | Tag keys to remove (with --untag-run) | `None` |
| `--storage-type` | Omit to take AWS's preferred DYNAMIC default Choices: STATIC, DYNAMIC | `None` |
| `--storage-capacity` | Run storage in GiB; only meaningful with --storage-type STATIC | `None` |
| `--cache-id` | Run cache to reuse task results from | `None` |
| `--cache-behavior` | See operational details below. Choices: CACHE_ALWAYS, CACHE_ON_FAILURE | `None` |
| `--run-group-id` | Associate service-side resource/duration limits; not a dollar budget | `None` |
| `--workflow-version-name` | Pin the workflow version to run | `None` |
| `--run-tags` | Cost-allocation tags for the RUN as JSON, e.g. '{"team":"genomics"}'. Per-run cost allocation, applied at submission time. | `None` |
| `--region` | See operational details below. | `us-east-1` |
| `--profile` | See operational details below. | `None` |
| `--limit` | Max results for list modes | `25` |
| `--wait` | Poll until the run reaches a terminal state before reporting. Watching does not stop billing, and Ctrl-C stops watching, not the run. | `False` |
| `--poll-interval` | Seconds between --wait polls (default: 30) | `30.0` |
| `--wait-timeout-seconds` | Give up watching after this long (default: 24h). The run continues. | `86400.0` |
| `--allow-remote-inputs` | Acknowledge that submitting a run sends genomic data to AWS | `False` |
| `--confirm-submit` | Actually submit. Without it, --start-run only estimates and bills nothing. | `False` |

## Operational Details

- `--params` is a JSON object. HealthOmics parameter values must be strings;
  required keys come from the selected workflow/version's parameter template.
  Tags are separate JSON maps, not embedded parameter metadata.
- `--start-run` requires an explicit workflow type. `--workflow-version-name`
  pins a private version. `--list-workflow-versions` inventories existing versions.
- `--engine` applies to registration; `--recommend-engine` applies to discovery.
  Registration supports WDL/CWL/Nextflow; live container inspection has narrower,
  explicitly documented coverage in READINESS.md.
- `--additional-files` builds a reproducible archive with fixed ZIP metadata and
  recorded hashes. Registration creates persistent resources; it does not execute
  the workflow. Duplicate names are refused unless explicitly allowed.
- Omit `--storage-type` to use AWS's private-workflow default. Ready2Run may report
  STATIC storage without the bridge requesting it. `--storage-capacity` requires
  STATIC and rounds to 1,200 GiB or multiples of 2,400 GiB. 5,000 becomes 7,200.
- `--cache-behavior` requires `--cache-id`. The bridge references existing caches
  and groups; list/describe modes do not create them. Groups are service-side
  resource/duration limits, not a guaranteed monetary cap.
- `--run-tags` attaches initial allocation metadata. `--tag-run` sets supplied
  keys; `--untag-run` removes named keys. `--sync-tags` removes keys not in the
  desired map, as well as adding/changing supplied keys. It is a mutation.
- `--region` defaults to AWS_REGION, otherwise us-east-1. `--profile` defaults
  to AWS_PROFILE, otherwise boto3's default credential chain. Explicit
  `--profile default` selects that named profile.
- `--limit` defaults to 25 for list modes; discovery considers a bounded
  candidate set. Run task pagination is complete and independent of that limit.
- `--wait` applies to submitted/observed runs. Defaults: 30-second polls,
  86,400-second observation timeout. Ctrl-C and timeout do not cancel billing.
- The bridge cannot cancel. After separate operator authorization, use
  `aws omics cancel-run --id ID --profile PROFILE --region REGION`.
- Ready2Run fee estimates are dated snapshots in healthomics_pricing.py.
  Private-run costs remain unknown before execution; do not invent a price.
  S3 requests, transfers, storage and CloudWatch reads may incur charges.
- The execution role and the caller have different permissions. A caller-visible
  S3 object is not proof the run can read it. Public example input paths such as
  `s3://omics-us-east-1/sample-inputs/1830181/target.fasta` are workflow-specific;
  inspect access and existence instead of assuming every workflow supplies one.
- Deep verification requires confirmation. Smoke mode additionally requires
  expected greeting/residue count. Generic files do not become clinically or
  scientifically validated merely because the workflow completed.
- `--logs` / `--analyze-run` require status or receipt recovery. Log limits are
  1..1000. `--input-format` / `--recommend-engine` require recommendation mode.
- Recovery, unknown readiness overrides and safe replay are in OPERATIONS.md.

## Output Contract

Every successful reporting mode emits report.md, result.json, one mode-specific
CSV, reproducibility/commands.sh, environment.yml, replay_manifest.json and
checksums.sha256. Optional outputs are declared in SKILL.md.

| Mode | Primary table / additional artifact |
|---|---|
| Run status | tables/tasks.csv |
| Run listing | tables/runs.csv |
| Workflow listing/search/recommendation | tables/workflows.csv |
| Run groups / caches | tables/run-groups.csv / tables/run-caches.csv |
| Workflow versions | tables/workflow-versions.csv |
| Verification | tables/outputs.csv, outputs.json, handoff.json |
| Upload / download | tables/uploads.csv / tables/downloads.csv |
| Readiness | tables/checks.csv, environment.json for live evidence |
| Parameters | tables/params-template.csv, params.template.json |
| Registration | tables/definition.csv, tables/workflow.zip |
| Tags | tables/tags.csv |

Check the mode rather than assuming tasks.csv. Reports warn before reusing a
nonempty report directory. Downloaded data is not silently overwritten.
Failed reports write structured error codes when the destination is writable.
