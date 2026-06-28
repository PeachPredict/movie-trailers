"""Fetch the ONNX sentence-embedder artifacts (no torch needed).

Downloads a pre-exported ONNX build of all-MiniLM-L6-v2 (Apache-2.0) so dev and
the container ship byte-identical files. The Dockerfile runs the same download at
build time into /app/models/excitement.

Run:
    cd benchmarks/excitement
    uv run --with huggingface_hub python 03_export_embedder.py
Output: artifacts/model.onnx, artifacts/tokenizer.json
"""
from __future__ import annotations

import shutil
from pathlib import Path

from huggingface_hub import hf_hub_download

HERE = Path(__file__).parent
ARTIFACTS = HERE / "artifacts"
REPO = "Xenova/all-MiniLM-L6-v2"
FILES = {"onnx/model.onnx": "model.onnx", "tokenizer.json": "tokenizer.json"}


def main() -> None:
    ARTIFACTS.mkdir(exist_ok=True)
    for remote, local in FILES.items():
        path = hf_hub_download(repo_id=REPO, filename=remote)
        dest = ARTIFACTS / local
        shutil.copyfile(path, dest)
        size = dest.stat().st_size / 1e6
        print(f"  {remote} → {dest}  ({size:.1f} MB)")
    print(f"embedder ready in {ARTIFACTS}")


if __name__ == "__main__":
    main()
