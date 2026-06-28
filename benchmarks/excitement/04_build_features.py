"""Build trailer-level feature vectors for the labeled training set.

Uses the EXACT runtime feature recipe (`excitement_model.build_feature_vector`)
so there is no dev↔runtime drift — the only thing the trainer adds on top is the
scaler + Ridge fit (script 05).

Run (onnxruntime/tokenizers/numpy are project deps):
    cd benchmarks/excitement
    EXCITEMENT_MODEL_DIR=artifacts uv run python 04_build_features.py
Input: training_set.jsonl + labels.jsonl   Output: features.npz
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

# Point the runtime module at the locally-downloaded embedder before importing it.
HERE = Path(__file__).parent
os.environ.setdefault("EXCITEMENT_MODEL_DIR", str(HERE / "artifacts"))

from movie_trailers.content import excitement_model as em  # noqa: E402

OUT = HERE / "features.npz"


def main() -> None:
    labels = {
        d["youtube_video_id"]: d["excitement"]
        for d in (json.loads(x) for x in (HERE / "labels.jsonl").read_text().splitlines() if x.strip())
    }
    trailers = [
        json.loads(x) for x in (HERE / "training_set.jsonl").read_text().splitlines() if x.strip()
    ]

    holder: dict = {}
    X, y, vids, movies = [], [], [], []
    for i, tr in enumerate(trailers):
        vid = tr["youtube_video_id"]
        if vid not in labels:
            continue
        vec = em.build_feature_vector(tr["comments"], holder=holder)
        if vec is None:
            continue
        X.append(vec)
        y.append(labels[vid])
        vids.append(vid)
        movies.append(tr["movie_tmdb_id"])
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(trailers)} processed, {len(X)} featurized")

    X = np.vstack(X)
    np.savez(
        OUT,
        X=X.astype("float32"),
        y=np.asarray(y, dtype="float32"),
        vid=np.asarray(vids),
        movie=np.asarray(movies, dtype="int64"),
        feature_layout=em.FEATURE_LAYOUT,
    )
    print(f"wrote features {X.shape} (layout={em.FEATURE_LAYOUT}) → {OUT}")


if __name__ == "__main__":
    main()
