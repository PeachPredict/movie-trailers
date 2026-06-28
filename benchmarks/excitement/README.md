# Comment-excitement distillation

Trains the cheap local model that scores a trailer's comment-section **excitement**
(0–100) — the signal behind the per-movie *excitement decay across sequential
trailers* finding (later trailers draw less-excited comments; the decay is
*semantic*, so a regex/lexicon proxy is blind to it).

**Why distill.** Claude scores excitement well but is too expensive to run daily
per trailer. So Claude is the one-time **teacher**: it labels comment sets, and a
tiny **student** (ONNX MiniLM embeddings + a Ridge head) learns to reproduce the
labels. The student runs in the daily pipeline with no API calls and no torch —
only `onnxruntime` + `tokenizers` + `numpy` (all already in the image).

Everything here is **dev-only** and never ships. The only artifacts that cross
into the app are the embedder (downloaded at Docker build time) and the tiny head
`src/movie_trailers/content/artifacts/ridge.npz` (committed, shipped in the wheel).

## Refresh runbook (quarterly, to fight drift)

Run from the repo root (so `.env` and the project venv resolve):

```sh
# 1. Pull the trailer-level training set (read-only BigQuery).
uv run python benchmarks/excitement/01_pull_training_set.py

# 2. Label with the Claude teacher (order-blinded — see the script).
uv run python benchmarks/excitement/02_label_with_claude.py

# 3. Fetch the ONNX embedder (byte-identical to what the Docker build downloads).
cd benchmarks/excitement && uv run --with huggingface_hub python 03_export_embedder.py && cd -

# 4. Build features with the EXACT runtime recipe (no dev↔runtime drift).
EXCITEMENT_MODEL_DIR=benchmarks/excitement/artifacts \
  uv run python benchmarks/excitement/04_build_features.py

# 5. Train the Ridge head → writes src/movie_trailers/content/artifacts/ridge.npz.
uv run --with scikit-learn --with scipy python benchmarks/excitement/05_train_ridge.py

# 6. ACCEPTANCE GATE — the student must reproduce the decay finding via the
#    shipped runtime path, and the lexicon proxy must stay blind.
EXCITEMENT_MODEL_DIR=benchmarks/excitement/artifacts \
  uv run python benchmarks/excitement/06_acceptance_test.py
```

Only commit the new `ridge.npz` (and rebuild/redeploy the image) once step 6
passes. The `model_version` (e.g. `mlv1-minilm-l6-YYYYMMDD`) is stamped from the
training date, so a refresh re-scores every trailer under the new version on the
next daily run — old scores are retained for comparison.

## Validation targets (step 5, GroupKFold by movie)

- Spearman ρ (teacher↔student) ≳ 0.6 — rank order is what drives the decay finding.
- MAE ≲ 8–10 pts (informational; ρ matters more for the aggregate pattern).

## Notes / caveats

- **Cost knob.** `02_label_with_claude.py` uses concurrent synchronous Claude
  calls for an immediate, reliable run. For a cheaper scheduled refresh, the
  Message Batches API is ~50% off (async) — swap it in if cost matters more than
  turnaround.
- **Order-blinding is load-bearing.** Batches mix unrelated trailers behind opaque
  labels; the teacher never sees a trailer's ordinal/title/date/movie, so it
  scores excitement-from-text, not "this is the 4th trailer → score it lower."
- **English-centric embedder.** all-MiniLM-L6-v2 is English-first; the dataset is
  worldwide. Step 6 is the gate — if non-English comment sets drag the pattern
  below threshold, switch to `paraphrase-multilingual-MiniLM-L12-v2` (~470 MB,
  still < the 4 GiB Cloud Run cap) in scripts 03/04 and the Dockerfile.
- `artifacts/`, `training_set.jsonl`, `features.npz` are gitignored (large /
  regenerable). `labels.jsonl` is kept for auditability.
```
