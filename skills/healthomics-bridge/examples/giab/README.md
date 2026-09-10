# Public GIAB CPU Example

This private WDL processes a real public Genome in a Bottle benchmark. It is a
small regression for staging, containers, variant-data processing and output
provenance, not variant calling or a clinical accuracy benchmark.

## Source and Expected Result

Use the versioned HG002 GRCh38 v4.2.1 VCF and index in [source.json](source.json).
The [AWS registry](https://registry.opendata.aws/giab/) documents this public
dataset; credit NIST and the Genome in a Bottle Consortium. No private patient
inputs are permitted. This is Registry of Open Data, not a paid Data Exchange
subscription.

The VCF is 156,252,944 bytes; its index is 1,657,747 bytes. The recorded SHA-256
values fingerprint bytes downloaded over HTTPS, not a publisher signature.
The publisher's root md5.in covers supplementary files, not these selected
objects. Do not substitute its checksums or interpret the multipart ETag as MD5.
The VCF has no ##reference header: GRCh38 is release-path metadata, not a claim
inferred from the variants themselves.

The fixed 1-based inclusive POS interval chr22:20000000-20100000 contains
193 records for HG002. These expectations were independently computed by
streaming the original 4,048,342-record VCF before cloud execution. The local
bcftools result matched the recorded variant/genotype fingerprint.

Live verification on 2026-09-06: PRIVATE workflow 4064935, run 4380351,
COMPLETED. The downloaded VCF was byte-for-byte identical to the local result;
bcftools reported 167 SNPs and 26 indels (193 records), the index read returned
193, and all five workflow checksum entries matched. The preceding repaired
private hello run 8763974 also completed with its exact greeting assertion.
Account-specific receipts and results are recorded in
output/healthomics/cpu-giab-20260906/RESULTS.md at the repository root. These
IDs are historical evidence, not portable defaults for another account.

## Workflow

1. Inspect source.json and verify the downloaded file sizes and SHA-256 values.
2. Run giab_qc.py on the original VCF to produce an independent expected JSON.
3. Build environment/Dockerfile.cpu for linux/amd64, publish to the separately
   approved ECR repository, and use its immutable digest in params.
4. Register giab_qc.wdl through the bridge. Stage only these public VCF/index
   files and the reviewed giab_qc.py into the isolated bucket's inputs prefix.
5. Run --check --live for the exact private workflow and parameters. Use one
   bounded run group and DYNAMIC storage; do not acknowledge required UNKNOWN
   checks merely to continue.
6. Preview the request, then submit once with --confirm-submit after approval.
   Preserve the receipt. --wait and Ctrl-C do not cancel execution or billing.
7. Download through --verify-outputs deep --confirm-download into a fresh
   destination. Run giab_qc.py --expected against the downloaded subset.
8. Also verify output checksums, stats record count, index readability and tool
   version. A completed AWS run alone is not sufficient.

The task requests 2 CPUs and 4 GiB, no GPU. It uses bcftools 1.16 view with
--regions-overlap 0 to select records by POS rather than overlapping indel span,
then builds a tabix index and runs stats. The independent helper fingerprints
CHROM, POS, REF, ALT, FORMAT and sample fields in order. It deliberately does
not replace bcftools/htslib's VCF processing or validate clinical meaning.
[bcftools semantics](https://samtools.github.io/bcftools/bcftools.html).

## Commands

From the repository root, with downloaded source files in a fresh directory:

```bash
uv run --frozen --with miniwdl miniwdl check skills/healthomics-bridge/examples/giab/giab_qc.wdl
uv run --frozen python skills/healthomics-bridge/examples/giab/giab_qc.py \
  --input SOURCE.vcf.gz --output expected.json
uv run --frozen python skills/healthomics-bridge/examples/giab/giab_qc.py \
  --input DOWNLOADED_SUBSET.vcf.gz --expected expected.json --output validation.json
```

The helper refuses to overwrite its output. The bridge's live commands require
--with boto3 --with miniwdl in a uv environment where those optional packages
are not already installed. CLI options are in ../../CLI_REFERENCE.md.

Parameters are string-valued: container (digest-pinned private ECR URI),
benchmark_vcf, benchmark_index and validator (three staged S3 objects).
The workflow's interval is intentionally fixed for this regression; change the
workflow and independently regenerate expectations for a different interval.

## Outputs and Costs

Outputs: subset.vcf.gz, subset.vcf.gz.tbi, stats.txt, metrics.json,
checksums.sha256 and tool-version.txt. HealthOmics may place each workflow
output in a different directory; locate by the recorded output manifest,
not by assuming one flat folder.

Expected compute for a 1-10 minute task is roughly $0.002-$0.02 at the reviewed
us-east-1 omics.c.large rate of $0.1148/hour, with a 60-second minimum. This is
an estimate, not an invoice or hard cap. Input staging, S3/ECR retention,
requests and logs add costs. One-run concurrency and a 20-minute service-side
duration limit bound resources, not dollars. S3 and ECR retention requires
separately approved cleanup; stack deletion alone does not remove them.
[AWS pricing](https://aws.amazon.com/healthomics/pricing/).

The base image is pinned and bcftools is fixed at Debian package 1.16-1.
Other Debian packages are resolved at build time and inventoried at
/usr/local/share/package-versions.txt. Reusing the published image digest is
reproducible; rebuilding this recipe is not guaranteed byte-identical.

ClawBio is a research and educational tool. It is not a medical device and does
not provide clinical diagnoses. Consult a healthcare professional before making
any medical decisions.
