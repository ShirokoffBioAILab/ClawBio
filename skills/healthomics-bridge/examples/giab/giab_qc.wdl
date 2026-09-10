version 1.0

workflow giab_qc {
  input {
    String container
    File benchmark_vcf
    File benchmark_index
    File validator
  }
  call subset_qc {
    input: container = container, benchmark_vcf = benchmark_vcf,
           benchmark_index = benchmark_index, validator = validator
  }
  output {
    File variants = subset_qc.variants
    File index = subset_qc.index
    File statistics = subset_qc.statistics
    File metrics = subset_qc.metrics
    File checksums = subset_qc.checksums
    File tool_version = subset_qc.tool_version
  }
}

task subset_qc {
  input {
    String container
    File benchmark_vcf
    File benchmark_index
    File validator
  }
  command <<<
    set -euo pipefail
    bcftools view --no-version --regions-overlap 0 \
      -r chr22:20000000-20100000 -Oz -o subset.vcf.gz \
      "~{benchmark_vcf}##idx##~{benchmark_index}"
    bcftools index --tbi subset.vcf.gz
    bcftools stats subset.vcf.gz > stats.txt
    bcftools --version > tool-version.txt
    python3 "~{validator}" --input subset.vcf.gz --output metrics.json
    sha256sum subset.vcf.gz subset.vcf.gz.tbi stats.txt metrics.json tool-version.txt > checksums.sha256
  >>>
  output {
    File variants = "subset.vcf.gz"
    File index = "subset.vcf.gz.tbi"
    File statistics = "stats.txt"
    File metrics = "metrics.json"
    File checksums = "checksums.sha256"
    File tool_version = "tool-version.txt"
  }
  runtime {
    docker: container
    cpu: 2
    memory: "4 GiB"
  }
}
