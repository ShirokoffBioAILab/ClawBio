# Private ESMFold Smoke Example

Prepared example, not yet proof of a successful private GPU run. Do not substitute
the historical Ready2Run example. See the dated live evidence for execution status.

This WDL runs `facebook/esmfold_v1` on the synthetic 20-residue sequence
`ACDEFGHIKLMNPQRSTVWY`. It uses one A10G GPU, 8 CPUs and 32 GiB RAM, with one recycle
and chunk size 32. The output is a PDB and model/runtime provenance in metrics.json.
This exercises infrastructure and output integrity, not biological accuracy.

## Reproducibility

- Model revision: `75a3841ee059df2bf4d56688166c8fb459ddd97a` (MIT).
- Weights: 8,442,062,570 bytes; publisher LFS SHA-256
  `2ee07356b125d1e3e57503c204111fd7323347fc4735d41d3caac57c2a78e116`.
- The Docker base is pinned by digest; direct Python dependencies are pinned.
  Transitive dependencies are not independently locked. Pin the resulting ECR
  image digest in run parameters and preserve the build record.
- `prepare_model.py` downloads only six named files at the immutable revision,
  verifies weights against the publisher digest and records every file's hash.
- The workflow reads the prepared model archive as a typed S3 File. Inference
  verifies the weights again, uses local-files-only loading and disables network
  model resolution. Archive paths/types are restricted to the six named files.
- No remote model code is enabled. PyTorch weights-only loading is requested.
  CUDA must be available; there is no silent CPU or Ready2Run fallback.

## Explicit Preparation

`builder.yaml` is a separate infrastructure component, not a bridge capability.
Review and approve the exact source archive, owned bucket/repository, IAM scope
and budget before deployment or upload. The build receives only Dockerfile,
inference.py, prepare_model.py, buildspec.yaml and .dockerignore. Do not include
the repository, credentials, local data or the incomplete local model download.

The CodeBuild project permits one concurrent Linux medium build, with a 20-minute
timeout and a five-minute queue timeout. It writes only the owned model prefix
and repository. The model is staged in S3, not duplicated inside the image;
.dockerignore excludes downloads from the Docker build context.

After a successful build, verify the S3 provenance and ECR digest. Register
private.wdl through the bridge, then construct a `live_smoke.py` case with
`kind: esmfold`, `workflow_type: PRIVATE`, `data_classification: synthetic`,
`expected_residues: 20`, `expected_sequence: ACDEFGHIKLMNPQRSTVWY`, and parameters
`container` (digest URI) and `model_archive` (owned S3 object). Use an explicitly
bounded run group, the default AWS profile when requested, and DYNAMIC storage.

Run readiness/preview first; confirmation authorizes one run. The harness checks
sequence, residue count, alpha carbons, finite coordinates and B-factors. Preserve
the receipt and use `--resume` after interruption, never another submission token.

## Cost Plan

User ceiling for the dated test: **$5 total**, including cloud preparation and GPU
execution. The 2026-09-10 us-east-1 public price lookup gave Linux medium CodeBuild
at $0.01/minute and HealthOmics omics.g5.2xlarge at $1.6362/hour. Thus a 20-minute
build plus 20 GPU minutes estimates $0.7454 before storage, requests and logs.
This is an estimate, not a bill or hard monetary cap. Recheck prices before reuse.

Do not automatically retry builds or workflows. Account for every attempt before
authorizing another. Run-group duration limits constrain execution, not dollars;
local observation timeout does not stop AWS billing. Retained model/image objects
and logs incur ongoing charges and are not covered indefinitely by a test budget.
Review exact resources and obtain deletion authorization for cleanup; never delete
the shared smoke bucket/repository or unrelated objects to save costs.

Sources: [model](https://huggingface.co/facebook/esmfold_v1),
[Transformers ESM API](https://huggingface.co/docs/transformers/model_doc/esm),
[AWS drug-discovery workflows](https://github.com/aws-samples/drug-discovery-workflows),
[CodeBuild prices](https://aws.amazon.com/codebuild/pricing/),
[HealthOmics prices](https://aws.amazon.com/healthomics/pricing/).

ClawBio is a research and educational tool. It is not a medical device and does
not provide clinical diagnoses. Consult a healthcare professional before making
any medical decisions.
