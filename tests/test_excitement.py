"""Tests for the comment-excitement scorer.

The ONNX embedder isn't available in CI (it's downloaded at build time), so we
mock `embed_texts` and exercise the torch-free scoring path: feature assembly,
the real shipped Ridge head, input alignment, clipping, and None for empty sets.
"""
from unittest.mock import patch

import numpy as np

from movie_trailers.content import excitement_model as em


def _fake_embed(texts, *, holder):
    # Deterministic stand-in for the ONNX encoder: shape (n, EMBED_DIM).
    return np.full((len(texts), em.EMBED_DIM), 0.1, dtype="float64")


def _cs(*texts):
    return [{"text": t, "like_count": 5, "rank": i + 1} for i, t in enumerate(texts)]


def test_feature_vector_shape_and_aux():
    with patch.object(em, "embed_texts", _fake_embed):
        v = em.build_feature_vector(_cs("amazing!", "cannot wait 🔥"), holder={})
    assert v.shape == (em.N_FEATURES,)  # 384 + 3 aux
    assert v[em.EMBED_DIM + 1] > 0  # log1p(n_comments) aux is positive


def test_empty_and_blank_sets_return_none():
    with patch.object(em, "embed_texts", _fake_embed):
        assert em.build_feature_vector([], holder={}) is None
        assert em.build_feature_vector(_cs("", "   "), holder={}) is None


def test_score_comment_sets_aligned_and_clipped():
    sets = [_cs("incredible, day one!"), [], _cs("looks boring", "meh")]
    with patch.object(em, "embed_texts", _fake_embed):
        out = em.score_comment_sets(sets, holder={})
    assert len(out) == len(sets)
    assert out[1] is None  # empty set → None, in place
    for v in (out[0], out[2]):
        assert v is not None and 0.0 <= v <= 100.0


def test_model_version_is_loadable():
    assert em.model_version(holder={}).startswith("mlv1-")


def test_lexicon_rate_rewards_hype():
    assert em._lexicon_rate(["INSANE can't wait!! 🔥"]) > em._lexicon_rate(["it is a film"])
