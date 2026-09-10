"""Bounded synthetic ESMFold inference using reviewed local model artifacts."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import tarfile

REVISION = "75a3841ee059df2bf4d56688166c8fb459ddd97a"
WEIGHTS_SHA256 = "2ee07356b125d1e3e57503c204111fd7323347fc4735d41d3caac57c2a78e116"
MODEL_FILES = {"config.json", "pytorch_model.bin", "special_tokens_map.json", "tokenizer_config.json", "vocab.txt", "README.md"}


def validate_sequence(sequence: str) -> str:
    if not re.fullmatch(r"[ACDEFGHIKLMNPQRSTVWY]{1,80}", sequence):
        raise ValueError("Smoke sequence must contain 1-80 canonical amino acids")
    return sequence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--sequence", default="ACDEFGHIKLMNPQRSTVWY")
    parser.add_argument("--output", type=Path, default=Path("prediction.pdb"))
    args = parser.parse_args()
    sequence = validate_sequence(args.sequence)
    directory = Path("model")
    directory.mkdir(exist_ok=False)
    with tarfile.open(args.model) as archive:
        members = archive.getmembers()
        if {m.name for m in members} != MODEL_FILES or len(members) != len(MODEL_FILES):
            raise ValueError("Unexpected model archive contents")
        for member in members:
            if not member.isfile() or member.size > 8_500_000_000:
                raise ValueError("Unsafe model archive entry")
            with archive.extractfile(member) as source, (directory / member.name).open("xb") as dest:
                import shutil
                shutil.copyfileobj(source, dest)
    with (directory / "pytorch_model.bin").open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != WEIGHTS_SHA256:
            raise ValueError("Model weights differ from publisher LFS SHA-256")
    import torch
    from transformers import AutoTokenizer, EsmForProteinFolding
    if not torch.cuda.is_available():
        raise RuntimeError("Private ESMFold smoke requires a GPU; no CPU fallback")
    torch.manual_seed(0)
    tokenizer = AutoTokenizer.from_pretrained(directory, local_files_only=True)
    model = EsmForProteinFolding.from_pretrained(directory, local_files_only=True,
        low_cpu_mem_usage=True, weights_only=True).eval().cuda()
    model.esm = model.esm.half()
    model.trunk.set_chunk_size(32)
    tokens = tokenizer(sequence, return_tensors="pt", add_special_tokens=False).to("cuda")
    with torch.inference_mode():
        output = model(**tokens, num_recycles=1)
    cpu_output = {key: value.cpu() for key, value in output.items()}
    with args.output.open("x") as stream:
        stream.write(model.output_to_pdb(cpu_output)[0])
    Path("metrics.json").write_text(json.dumps({"model": "facebook/esmfold_v1", "revision": REVISION,
        "weights_sha256": WEIGHTS_SHA256, "sequence": sequence, "residues": len(sequence),
        "gpu": torch.cuda.get_device_name(0), "torch": torch.__version__,
        "num_recycles": 1, "scope": "synthetic output sanity, not prediction accuracy"}, indent=2))


if __name__ == "__main__":
    main()
