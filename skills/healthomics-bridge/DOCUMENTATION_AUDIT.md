# Documentation Preservation Audit

Compared against committed SKILL.md at aee926d and the pre-existing readiness
additions on 2026-09-05. The first shortening removed useful details; this audit
records their restored locations. EXAMPLE.md, GOLDEN_WORKFLOWS.md and INTENTS.json
were not deleted or replaced. This is a semantic review, not a byte-for-byte claim.

| Original material | Current location or correction |
|---|---|
| Frontmatter: author, formats, dependencies, triggers | SKILL.md retained; version updated, miniwdl declared, mandatory AWS env gating removed |
| Quick start, why gates exist | SKILL.md Quick Start, Scope, Workflow and Safety |
| Every CLI flag and default | CLI_REFERENCE.md, derived from the current parser |
| Exact PRIVATE/READY2RUN type requirement | SKILL.md Workflow/Gotchas and READINESS.md |
| Upload/download/register confirmations | SKILL.md Inputs and Commands; CLI_REFERENCE.md |
| Storage rounding, DYNAMIC/STATIC differences | CLI_REFERENCE.md Operational Details |
| Cache behavior, run groups and workflow versions | CLI_REFERENCE.md and GOLDEN_WORKFLOWS.md |
| All tag modes and convergence/removal behavior | SKILL.md command table; CLI_REFERENCE.md |
| Params formats, S3 inputs, execution-role caveats | CLI_REFERENCE.md and READINESS.md |
| Historical output excerpt and complete real examples | SKILL.md Example Output and unchanged EXAMPLE.md |
| Mode-specific table filenames and reproducibility files | CLI_REFERENCE.md Output Contract and SKILL.md Output Structure |
| Task failure enrichment and its 25-task cap | SKILL.md Methodology and monitoring.py |
| Offline vs read-only live vs real execution evidence | COMPARISON.md, OPERATIONS.md and EXAMPLE.md |
| Pricing snapshot and private-run cost uncertainty | CLI_REFERENCE.md, SKILL.md Safety and healthomics_pricing.py |
| Timeout does not stop billing; manual cancellation | SKILL.md Gotchas and OPERATIONS.md |
| Container architecture/policy failures and fixes | GOTCHAS.md and READINESS.md |
| Disclaimer, credentials and provisioning boundaries | SKILL.md Safety/Agent Boundary; environment/README.md |
| Maintenance, stale API/pricing signals | SKILL.md Maintenance; live contract tests |

## Deliberate Corrections, Not Lost Features

- Removed unconditional claims that single-part ETags equal MD5: encryption can
  invalidate that interpretation.
- Removed the claim that only this bridge paginates/retries; local peers do too.
- Removed claims that every capability was live-tested and read-only calls are
  categorically free. Requests, logs and storage can incur charges.
- Replaced indefinite idempotency guarantees with preserved receipts, explicit
  uncertainty handling and a warning about token lifetime/account context.
- Replaced generic CSV/TSV analysis suggestions with conservative local metadata.
- Performance analysis is now an optional AWS tool adapter; image reachability
  checks already exist. The original blanket exclusion was stale.
- S3's method allowlist is not an API-operation allowlist; managed transfers fan
  out into underlying operations.

No storage-management or sequence/reference-store capability was silently added.
Infrastructure remains separately reviewed, and further integrations require
approval before implementation.
