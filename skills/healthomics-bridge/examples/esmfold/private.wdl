version 1.0
workflow private_esmfold {
  input { String container File model_archive }
  call fold { input: container = container, model_archive = model_archive }
  output { File structure = fold.structure File metrics = fold.metrics }
}
task fold {
  input { String container File model_archive }
  command <<<
    set -euo pipefail
    python /opt/esmfold/inference.py --model "~{model_archive}" --output prediction.pdb
  >>>
  output { File structure = "prediction.pdb" File metrics = "metrics.json" }
  runtime {
    docker: container
    cpu: 8
    memory: "32 GiB"
    gpuCount: 1
    gpuType: "nvidia-tesla-a10g"
    maxRetries: 0
  }
}
