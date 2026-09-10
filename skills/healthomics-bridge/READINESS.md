# Live readiness and durable runs

Keep HealthOmics, run-specific ECR inspection, and S3 I/O behind one skill.
Repository/bucket creation, image publication, IAM and resource-policy changes
belong to a separately authorized environment setup task. No bridge flag grants
those capabilities.

## Checks

`--check` remains local and labels its scope. For live inspection use
`--check --live --start-run ID` with the same workflow type, parameters, role,
region and output URI intended for submission. Supply `--profile default` to
select that profile explicitly. Install `boto3` and `miniwdl` for private WDL
inspection; neither is imported by the offline demo.

Confirmed submissions automatically repeat live checks. Required FAIL blocks
submission; required UNKNOWN also blocks unless the user explicitly accepts
that uncertainty and the command includes `--allow-unverified-readiness`.
Acknowledging uncertainty never overrides a known failure. Exit 2 means a
readiness block; the report and structured checks explain why.

| Evidence | Meaning and limits |
|---|---|
| Workflow lookup | Exact PRIVATE/READY2RUN type, active workflow/version, required parameter keys and string values |
| Container inventory | miniwdl loads/typechecks exported WDL with archive-only local imports; resolves literals, input/default bindings, string concatenation/interpolation and direct task/subworkflow calls |
| Incomplete inventory | Unsupported expressions, scatter/conditional calls, unsafe/missing imports, unsupported engines/maps or unavailable parser/definition remain UNKNOWN |
| ECR image | DescribeImages resolves the requested image; missing repository/image is FAIL, caller permission denial is UNKNOWN |
| Image architecture | Manifest-index platforms or digest-verified config metadata establish linux/amd64; unavailable metadata is UNKNOWN |
| Repository policy | Explicit service-principal pull grants with supported matching SourceArn/SourceAccount conditions are evidence only; denies, unsupported semantics or missing/mismatched context remain UNKNOWN |
| S3 paths | HEAD on explicit input objects and bounded output-prefix listing use caller credentials, not the execution role |
| Effective permissions | Execution-role, service-principal, SCP, KMS and networking authorization remain advisory UNKNOWN |

S3 metadata failures are advisory: a parameter may represent a prefix, and the
caller and execution role can have different access. PASS means the named check
passed, never that AWS guarantees a successful run. An ACTIVE workflow also does
not establish that its image still exists.

Definition/config metadata reads are bounded and do not follow redirects.
Signed metadata URLs are not written to reports. The bridge never reads registry
authentication tokens, pulls image layers, or publishes images.

Policy conditions support StringEquals, ArnEquals, StringLike and ArnLike,
with case-insensitive condition key names, alternative values within a key,
and all keys/operators required to match. Wildcards use IAM's `*` and `?`
semantics. SourceArn uses the base workflow ARN returned by GetWorkflow, also
when inspecting a version. SourceAccount uses the intended run-role account as
subscriber context, which can differ from the workflow owner. No account is
inferred from the image URI. This is a comparison against supplied run context,
not verification of the service's eventual request context or full authorization.
Negated conditions, IfExists/set operators, policy variables, NotResource,
NotPrincipal, NotAction and any explicit Deny remain UNKNOWN.

WDL inspection reads only sources already present in the export, never local
filesystem imports or remote imports. Absolute, parent-traversing, URL-like,
backslash and encoded paths are rejected; duplicate normalized WDL archive
paths are rejected. Import and call depth are capped at 10; archive limits are
100 WDL files and 2 MB uncompressed. Standard-library function calls (including
file reads/writes) and task execution are disabled during expression resolution.
Missing inputs needed for an image remain unresolved. Unsupported engines,
including WDL_LENIENT, remain UNKNOWN. Parameter image hints are retained only
when the parsed inventory is incomplete, so an image-building prefix does not
become a spurious container when resolution succeeds.

Exact image mappings take precedence over supported registry mappings. Registry
rewrites support explicit registry hostnames and ECR prefixes, using the workflow
ARN's partition/region/account unless the mapping supplies ecrAccountId. Ambiguous
or malformed maps, upstreamRepositoryPrefix and URI-only maps remain UNKNOWN.
Docker Hub short-name/alias normalization is not inferred; use exact image maps.
Mapping an image establishes its expected URI only, not pull-through cache
configuration, synchronization or permissions. No cache population is attempted.

`environment.json` records workflow digest, requested profile/region/role,
resolved image digests and inspection time. It is evidence, not a portable grant
of access. Live checks are repeated before submission rather than trusting a
stale manifest. Mutable image tags may still change after inspection; pin image
digests in maintained workflows.

## Submission and recovery

The bridge writes `readiness.json` before submission and atomically writes
`submission.json` with the request before StartRun. The returned run ID is saved
immediately, followed by an initial report, before monitoring begins.
`run_state.json` checkpoints observations during `--wait`.

After a terminal/session interruption, resume with `--run-status RUN_ID --wait`
and the original profile/region in a new output directory. Never infer that a
lost terminal stopped an AWS run. If the receipt remains SUBMITTING without an
ID, the call outcome is uncertain: retain its exact requestId when reconciling,
and inspect recent runs before considering another submission.

Request IDs now hash the full normalized StartRun request, including optional
version, cache, storage and tags. This changes the token scheme from earlier
bridge releases. Do not replay old submissions by regenerating their tokens;
inspect/resume by recorded run ID. Use a new run name for intentional reruns.

For compatibility the result envelope's `ok` still denotes successful reporting.
Run summaries add `report_ok`, nullable `execution_ok`, and AWS `failure_reason`.
A FAILED run with zero tasks is a failed execution, not a successful empty run.

## Regression and live validation

Run `uv run --frozen --with pytest --with miniwdl python -m pytest
skills/healthomics-bridge/tests -q` for offline tests. Live API regression remains
opt-in with `CLAWBIO_RUN_LIVE_HEALTHOMICS=1` and `--with boto3` as well.
No test should create a billable run automatically.

Private ESMFold requires its own definition, accessible images/model inputs and
an active PRIVATE workflow. Ready2Run ESMFold is a distinct choice, never a
fallback for a missing private workflow.

References: [AWS container requirements](https://docs.aws.amazon.com/omics/latest/dev/workflows-ecr.html),
[ECR permissions](https://docs.aws.amazon.com/omics/latest/dev/permissions-ecr.html),
[GetWorkflow export](https://docs.aws.amazon.com/boto3/latest/reference/services/omics/client/get_workflow.html).
