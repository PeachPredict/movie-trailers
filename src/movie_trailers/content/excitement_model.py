"""Torch-free runtime scorer for trailer comment-section *excitement* (0–100).

A distilled student model: ONNX MiniLM sentence-embeddings (run via `onnxruntime`)
+ a tiny Ridge head (numpy dot product). It reproduces — at ~zero marginal cost —
the excitement scores a Claude teacher produced offline, which a regex/lexicon
proxy was blind to (the decay across a movie's sequential trailers is *semantic*).

Runtime dependencies are only `onnxruntime`, `tokenizers`, and `numpy` — all
already present (transitive via faster-whisper). No torch, no scikit-learn, no API.

Artifacts:
  - embedder (`model.onnx` + `tokenizer.json`): large (~90 MB), loaded from the
    dir named by ``EXCITEMENT_MODEL_DIR`` (``/app/models/excitement`` in the image,
    downloaded at build time like the Whisper model).
  - head (`ridge.npz`): tiny (<1 MB), ships inside the package at
    ``content/artifacts/ridge.npz`` (override with ``EXCITEMENT_HEAD_PATH``).

The feature recipe (pooling + aux features) lives here so the offline trainer
imports the *exact* same code — there is no dev↔runtime drift by construction.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

EXCITEMENT_MODEL_DIR_ENV = "EXCITEMENT_MODEL_DIR"
EXCITEMENT_HEAD_PATH_ENV = "EXCITEMENT_HEAD_PATH"
_DEFAULT_MODEL_DIR = "/app/models/excitement"

TOP_N = 30  # comments pooled per trailer
MAX_TOKENS = 256  # MiniLM context
EMBED_DIM = 384
# Feature layout: the 384-d pooled embedding followed by these aux features, in
# this exact order. Bump the suffix when the recipe changes (it's checked against
# the trained head's stored layout).
FEATURE_LAYOUT = "emb384+log_mean_like+log_n+lexicon"
N_AUX = 3
N_FEATURES = EMBED_DIM + N_AUX

# The old lexicon proxy — kept as a *feature*, not the predictor. Cheap insurance
# for signal the embedding misses; the embedding carries the semantic load.
_HYPE = re.compile(
    r"\b(cant wait|can't wait|cannot wait|hype|hyped|insane|peak|goosebump|"
    r"masterpiece|cinema|goat|fire|banger|chills|epic|amazing|incredible|"
    r"perfect|legend|crazy|best|love|excited|finally|need this|day one)\b",
    re.I,
)
_POS_EMOJI = "🔥😍🤩😭❤️🥹🙌💯👏😱⚡🎬"

_HOLDER: dict[str, Any] = {}


# --------------------------------------------------------------------------- #
# Loading (lazy, cached in a holder dict — the _load_whisper pattern)
# --------------------------------------------------------------------------- #
def _model_dir() -> Path:
    return Path(os.environ.get(EXCITEMENT_MODEL_DIR_ENV, _DEFAULT_MODEL_DIR))


def _head_path() -> Path:
    override = os.environ.get(EXCITEMENT_HEAD_PATH_ENV)
    if override:
        return Path(override)
    return Path(__file__).parent / "artifacts" / "ridge.npz"


def _load_embedder(holder: dict[str, Any]) -> dict[str, Any]:
    """Load the ONNX session + tokenizer once. Used by both runtime and trainer."""
    if "session" in holder:
        return holder
    import onnxruntime as ort
    from tokenizers import Tokenizer

    base = _model_dir()
    session = ort.InferenceSession(
        str(base / "model.onnx"), providers=["CPUExecutionProvider"]
    )
    tok = Tokenizer.from_file(str(base / "tokenizer.json"))
    tok.enable_truncation(max_length=MAX_TOKENS)
    tok.enable_padding()
    holder["session"] = session
    holder["tokenizer"] = tok
    holder["input_names"] = {i.name for i in session.get_inputs()}
    return holder


def _load_head(holder: dict[str, Any]) -> dict[str, Any]:
    """Load the Ridge head (coef/intercept/scaler + model_version)."""
    if "coef" in holder:
        return holder
    import numpy as np

    npz = np.load(_head_path(), allow_pickle=True)
    holder["coef"] = npz["coef"].astype("float64")
    holder["intercept"] = float(npz["intercept"])
    holder["scaler_mean"] = npz["scaler_mean"].astype("float64")
    holder["scaler_scale"] = npz["scaler_scale"].astype("float64")
    holder["model_version"] = str(npz["model_version"])
    layout = str(npz["feature_layout"]) if "feature_layout" in npz else FEATURE_LAYOUT
    if layout != FEATURE_LAYOUT:  # guard against a stale head vs new recipe
        raise ValueError(f"head feature_layout {layout!r} != runtime {FEATURE_LAYOUT!r}")
    return holder


def model_version(*, holder: dict[str, Any] | None = None) -> str:
    """The trained head's version string, e.g. 'mlv1-minilm-l6-20260627'."""
    h = holder if holder is not None else _HOLDER
    return _load_head(h)["model_version"]


# --------------------------------------------------------------------------- #
# Embedding + feature building (shared offline/runtime — DO NOT fork)
# --------------------------------------------------------------------------- #
def embed_texts(texts: list[str], *, holder: dict[str, Any]) -> Any:
    """Tokenize + ONNX-encode + mask-aware mean-pool + L2-normalize → (n, 384)."""
    import numpy as np

    _load_embedder(holder)
    tok = holder["tokenizer"]
    enc = tok.encode_batch(texts)
    ids = np.asarray([e.ids for e in enc], dtype=np.int64)
    mask = np.asarray([e.attention_mask for e in enc], dtype=np.int64)
    feed = {"input_ids": ids, "attention_mask": mask}
    if "token_type_ids" in holder["input_names"]:
        feed["token_type_ids"] = np.zeros_like(ids)
    feed = {k: v for k, v in feed.items() if k in holder["input_names"]}
    last_hidden = holder["session"].run(None, feed)[0]  # (n, seq, 384)
    m = mask.astype("float32")[:, :, None]
    summed = (last_hidden * m).sum(axis=1)
    counts = np.clip(m.sum(axis=1), 1e-9, None)
    pooled = summed / counts
    norms = np.linalg.norm(pooled, axis=1, keepdims=True)
    return pooled / np.clip(norms, 1e-9, None)


def _lexicon_rate(texts: list[str]) -> float:
    """Mean per-comment hype signal (exclamations + caps + hype words + emoji)."""
    if not texts:
        return 0.0
    total = 0.0
    for t in texts:
        t = t or ""
        excl = t.count("!")
        caps = len(re.findall(r"\b[A-Z]{3,}\b", t))
        hype = len(_HYPE.findall(t))
        emoji = sum(t.count(e) for e in _POS_EMOJI)
        total += excl + caps + 2 * hype + emoji
    return total / len(texts)


def build_feature_vector(comments: list[dict[str, Any]], *, holder: dict[str, Any]) -> Any:
    """One trailer's pooled feature vector, or None if it has no usable text.

    `comments`: [{text, like_count, rank}, ...]. Uses the top-`TOP_N` by rank.
    Embeddings are like-weighted (log1p(like_count)) averaged; falls back to a
    plain mean when all like counts are zero. Aux features are appended.
    """
    import numpy as np

    rows = sorted(comments, key=lambda c: (c.get("rank") or 1_000_000))[:TOP_N]
    texts = [(c.get("text") or "").strip() for c in rows]
    keep = [i for i, t in enumerate(texts) if t]
    if not keep:
        return None
    texts = [texts[i] for i in keep]
    likes = np.asarray([max(0, rows[i].get("like_count") or 0) for i in keep], dtype="float64")

    emb = embed_texts(texts, holder=holder)  # (k, 384)
    w = np.log1p(likes)
    pooled = (emb * w[:, None]).sum(0) / w.sum() if w.sum() > 0 else emb.mean(0)

    aux = np.asarray(
        [np.log1p(float(likes.mean())), np.log1p(float(len(texts))), _lexicon_rate(texts)],
        dtype="float64",
    )
    return np.concatenate([pooled, aux])


# --------------------------------------------------------------------------- #
# Public scoring entry point
# --------------------------------------------------------------------------- #
def score_comment_sets(
    comment_sets: list[list[dict[str, Any]]],
    *,
    holder: dict[str, Any] | None = None,
) -> list[float | None]:
    """Score a batch of per-trailer comment sets → 0–100 excitement each.

    Returns one value per input set, aligned to input order; None for a set with
    no usable text. Pure numpy + ONNX — no torch, no sklearn, no network.
    """
    import numpy as np

    h = holder if holder is not None else _HOLDER
    _load_head(h)

    feats: list[Any] = []
    idx: list[int] = []
    for i, cs in enumerate(comment_sets):
        v = build_feature_vector(cs, holder=h)
        if v is not None:
            feats.append(v)
            idx.append(i)

    out: list[float | None] = [None] * len(comment_sets)
    if not feats:
        return out
    x = np.vstack(feats)
    x = (x - h["scaler_mean"]) / h["scaler_scale"]
    preds = np.clip(x @ h["coef"] + h["intercept"], 0.0, 100.0)
    for j, i in enumerate(idx):
        out[i] = float(preds[j])
    return out
