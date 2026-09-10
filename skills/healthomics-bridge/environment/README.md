# Isolated HealthOmics Environment

This component proposes infrastructure; the bridge never deploys it implicitly.
`template.yaml` creates a private encrypted/versioned artifact bucket, immutable
ECR repository, scoped execution role and a one-run group (16 CPUs, one GPU,
default 60 minutes). The duration setting is a service-side run-group limit,
not a dollar budget. Review capacity against the actual ESMFold implementation.

The template has been locally validated. An isolated instance was deployed on
2026-09-06 as clawbio-healthomics-cpu-smoke in us-east-1 with a 20-minute limit;
its account-specific evidence is in output/healthomics/cpu-giab-20260906.
The original shared environment was not changed. Other accounts must deploy
their own reviewed environment; these names are historical evidence, not defaults.
Create/review a CloudFormation change set with an explicit owner and account,
including its IAM changes, before execution. Do not replace or modify the older
shared `ho-bucket-test` bucket, repository policies or console-created roles.

## Publication and Workflow Ownership

1. Record stack/account/region, owner and cleanup responsibility.
2. Review and publish an approved Linux/amd64 image to the generated repository.
   Record source revision, license, build recipe and immutable image digest.
   An empty ECR repository does not make a workflow runnable.
3. Register `hello.wdl` through the bridge with a private workflow name, or add
   an explicitly named version to the existing private workflow. Pass
   its `container` parameter as the repository URI plus `@sha256:...`, not latest.
4. Register a separately reviewed private ESMFold definition and its matching
   image/model artifacts. Do not use Ready2Run ID 1830181 as a private workflow.
   Pin its workflow version and image digest; document model-weight provenance.
5. Record the returned workflow IDs/versions, role, run group and prefixes in
   the public/synthetic smoke case manifests described in `../OPERATIONS.md`.
6. Run readiness before confirming one WDL case, then one ESMFold case.

The role reads only this bucket's inputs and writes only its outputs. Public
sample/model sources outside this bucket require an explicitly reviewed setup,
not broadening policies on the fly. No patient data is permitted.

For the CPU-only hello and GIAB examples, Dockerfile.cpu provides a small
linux/amd64 image with bcftools and Python. Pin the published ECR digest rather
than its tag. Read ../examples/giab/README.md for public-data provenance and
validation. The scoped ECR grant needs BatchGetImage, GetDownloadUrlForLayer
and BatchCheckLayerAvailability; GetRepositoryPolicy is not a substitute for
the third action. The live repair caught this omission before submission and
test_environment.py now guards against it.

The 2026-09-10 live observability retest also found that task logs require
`logs:DescribeLogStreams` on the HealthOmics log group. The template now grants
that action alongside log writes, scoped to `/aws/omics/WorkflowLog`. The reviewed
update changed only the existing execution-role policy, without replacement;
fresh hello run 4200749 subsequently exposed run, engine and task streams.

## Retention and Cleanup

S3 and ECR use Retain on deletion/replacement. No automatic object expiration,
image deletion or force-empty operation is included. CloudFormation stack
deletion therefore leaves artifacts/images and their storage costs behind.
After recording results, review owned workflow versions, bucket object versions,
multipart uploads and ECR images before separately approving their deletion.
The run bridge cannot perform cleanup. Never delete resources solely because a
name resembles a fixture; verify ownership and dependencies.

## Validation

```bash
uv run --frozen --with cfn-lint cfn-lint skills/healthomics-bridge/environment/template.yaml
uv run --frozen --with miniwdl miniwdl check skills/healthomics-bridge/environment/hello.wdl
```

Sources: [Run groups](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-omics-rungroup.html),
[ECR permissions](https://docs.aws.amazon.com/omics/latest/dev/permissions-ecr.html).
