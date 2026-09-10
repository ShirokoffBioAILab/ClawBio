# HealthOmics Bridge: Comparison and Evidence

Reviewed 2026-09-05 against local source and official AWS/OpenClaw documentation.
This is a focused comparison, not a complete ClawHub marketplace audit.
LOC and test counts are not quality rankings.

| Peer | Useful local pattern | HealthOmics application |
|---|---|---|
| [Galaxy](../galaxy-bridge/tool_recommender.py) | Multi-signal scoring, format compatibility and reasons | Explainable recommendation metadata; unsupported/absent evidence stays UNKNOWN |
| [Illumina](../illumina-bridge/illumina_bundle.py) | Sample-sheet parsing, normalized sample metadata and QC | Bounded local VCF header metadata; no generic CSV-to-expression inference |
| [Flow.bio](../flow-bio/flow_bio.py) | Paginated samples/projects/executions | Complete task pagination with repeated-token detection |
| [nf-core RNA-seq](../nfcore-rnaseq-wrapper/provenance.py) | Provenance and resume manifests; separated modules | Receipt/recovery, monitoring, rendering and output-manifest modules |
| [Protocols.io](../protocols-io/protocols_io.py) | Timeouts and bounded rate-limit retries | SDK adaptive retry policies and bounded log reads |

## Corrections to the Earlier Comparison

Flow.bio does paginate, and Protocols.io configures retries. Those capabilities
are not unique to HealthOmics. S3 managed helpers are method-gated and fan out
into multiple AWS operations; operation-count ratios were misleading. Structured
error reports are intentional, not evidence that errors never become data.
The earlier claim that every capability was live-tested was incorrect.

## Current Architecture

The run CLI composes narrow Omics/S3/ECR adapters, local/live preflight,
monitoring, reporting, submission recovery, semantic output manifests and
optional observability. The separate environment template owns proposed S3,
ECR, IAM and run-group resources. It is not invoked by StartRun.

## Evidence Classes

- Offline tests verify fake-service contracts and regressions. They do not
  establish real execution-role authorization or scientific accuracy.
- Seven opt-in read-only AWS tests passed on 2026-09-05 using the default profile.
- Historical completed private WDL and Ready2Run ESMFold examples are documented
  in [EXAMPLE.md](EXAMPLE.md). Ready2Run evidence is not private ESMFold evidence.
- The latest pre-improvement WDL rerun failed before tasks; subsequent readiness
  found its ECR repository absent. Private ESMFold discovery found no matching
  workflow. Infrastructure templates are not evidence of a deployed environment.
- Billable end-to-end validation uses [live_smoke.py](live_smoke.py), one explicit
  PRIVATE public/synthetic case per invocation. It never silently substitutes a
  Ready2Run workflow.

## Remaining Limits

Effective IAM/KMS/SCP/network authorization is advisory UNKNOWN. WDL expression
coverage is intentionally restricted; unsupported engines/contexts stay UNKNOWN.
Generic S3 listings do not supply reliable scientific output roles or format
schemas. A profile can be reconfigured; saved profile/region is not cryptographic
account binding. Historical pricing and analyzer estimates are not billing caps.
A local checksum proves a byte fingerprint, not independent source integrity.

See [OPERATIONS.md](OPERATIONS.md), [READINESS.md](READINESS.md) and
[environment/README.md](environment/README.md) for operational boundaries.
