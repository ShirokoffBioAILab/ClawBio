"""Build-stage only: download pinned public artifacts and verify publisher hash."""
from pathlib import Path
import hashlib
import json
import tarfile
from huggingface_hub import snapshot_download
from inference import MODEL_FILES, REVISION, WEIGHTS_SHA256

root = Path(snapshot_download("facebook/esmfold_v1", revision=REVISION,
    allow_patterns=sorted(MODEL_FILES), local_dir="model-download", max_workers=4))
hashes = {}
for name in sorted(MODEL_FILES):
    with (root / name).open("rb") as stream:
        hashes[name] = hashlib.file_digest(stream, "sha256").hexdigest()
if hashes["pytorch_model.bin"] != WEIGHTS_SHA256:
    raise ValueError("Publisher model checksum mismatch")
with tarfile.open("model.tar", "w") as archive:
    for name in sorted(MODEL_FILES):
        archive.add(root / name, arcname=name)
Path("model-provenance.json").write_text(json.dumps({"repo": "facebook/esmfold_v1",
    "revision": REVISION, "license": "MIT", "sha256": hashes}, indent=2))
