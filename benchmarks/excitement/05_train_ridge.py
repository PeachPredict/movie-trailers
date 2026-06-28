"""Train the Ridge head on Claude's labels and export the shipped artifact.

Honest validation: GroupKFold by movie_tmdb_id (a movie's trailer #1 must not
leak into the fold scoring its #4). Reports out-of-fold MAE / RMSE / Spearman ρ,
then refits on all data and writes the tiny head into the package so the runtime
loads it.

Run:
    cd benchmarks/excitement
    uv run --with scikit-learn --with scipy python 05_train_ridge.py
Input: features.npz   Output: ../../src/movie_trailers/content/artifacts/ridge.npz
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr
from sklearn.linear_model import Ridge, RidgeCV
from sklearn.model_selection import GroupKFold, cross_val_predict
from sklearn.preprocessing import StandardScaler

HERE = Path(__file__).parent
FEATURES = HERE / "features.npz"
HEAD_OUT = HERE.parent.parent / "src" / "movie_trailers" / "content" / "artifacts" / "ridge.npz"
ALPHAS = np.logspace(-1, 4, 24)
MODEL_VERSION = f"mlv1-minilm-l6-{date.today():%Y%m%d}"


def main() -> None:
    data = np.load(FEATURES, allow_pickle=True)
    X, y, groups = data["X"].astype("float64"), data["y"].astype("float64"), data["movie"]
    layout = str(data["feature_layout"])
    n_groups = len(np.unique(groups))
    print(f"samples={len(y)}  features={X.shape[1]}  movies={n_groups}  layout={layout}")

    n_splits = min(5, n_groups)
    gkf = GroupKFold(n_splits=n_splits)

    # Out-of-fold predictions for honest metrics. Scale inside via a fresh Ridge
    # per fold (alpha picked by inner RidgeCV on each train fold).
    scaler_cv = StandardScaler()
    Xs = scaler_cv.fit_transform(X)  # ok: metrics use OOF preds, alpha picked per-fold
    model = RidgeCV(alphas=ALPHAS)
    oof = cross_val_predict(model, Xs, y, cv=gkf, groups=groups)
    oof = np.clip(oof, 0, 100)
    mae = float(np.mean(np.abs(oof - y)))
    rmse = float(np.sqrt(np.mean((oof - y) ** 2)))
    rho = float(spearmanr(oof, y).statistic)
    print(f"\nOUT-OF-FOLD (GroupKFold by movie, {n_splits} folds):")
    print(f"  MAE  = {mae:5.2f} pts   (target ≲ 8–10)")
    print(f"  RMSE = {rmse:5.2f} pts")
    print(f"  Spearman ρ = {rho:.3f}   (target ≳ 0.6)")
    print(f"  baseline MAE (predict mean) = {np.mean(np.abs(y - y.mean())):.2f}")

    # Final fit on all data.
    scaler = StandardScaler().fit(X)
    alpha = float(RidgeCV(alphas=ALPHAS).fit(scaler.transform(X), y).alpha_)
    ridge = Ridge(alpha=alpha).fit(scaler.transform(X), y)
    print(f"\nfinal alpha={alpha:g}")

    HEAD_OUT.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        HEAD_OUT,
        coef=ridge.coef_.astype("float64"),
        intercept=float(ridge.intercept_),
        scaler_mean=scaler.mean_.astype("float64"),
        scaler_scale=scaler.scale_.astype("float64"),
        model_version=MODEL_VERSION,
        feature_layout=layout,
    )
    size_kb = HEAD_OUT.stat().st_size / 1e3
    print(f"wrote head ({size_kb:.0f} KB, version={MODEL_VERSION}) → {HEAD_OUT}")


if __name__ == "__main__":
    main()
