# Portfolio site (`docs/`)

A **no-build static site** that showcases the movie-trailers project — a top-10
carousel plus four feature cards (Data Engineering, Time Series, the MCP server,
Model Distillation). Hosted on **GitHub Pages** as a project page:
**https://peachpredict.github.io/movie-trailers/**.

## How it works

- **Static.** Hand-written `index.html` + `assets/style.css` + `assets/app.js`. No
  framework, no build step. Chart.js is loaded from a pinned CDN. All asset/data
  refs are **relative** (`./assets/…`, `./data/…`) because it's served from a
  project subpath. `.nojekyll` disables Jekyll so files serve verbatim.
- **Data is read-only JSON snapshots** in `data/`, produced by
  `uv run mt generate-site-data` (see `src/movie_trailers/site/builder.py`). The
  command only runs SELECTs — it never writes to BigQuery.
- **Refreshed by a GitHub Action** (`.github/workflows/refresh-site-data.yml`):
  daily it regenerates `data/*.json` with a least-privilege read-only BigQuery
  service account and commits the result. Pages redeploys automatically.

## Regenerate locally

```sh
# Whichever dataset has the data you want to show (excitement scores currently
# live in movie_trailers_dev; carousel/coverage are richest in prod).
uv run mt generate-site-data --dataset=movie_trailers_dev --out-dir docs/data
python -m http.server -d docs 8077   # then open http://localhost:8077
```

## Files in `data/`

| File | Source | Drives |
|---|---|---|
| `carousel.json` | `trending` | the poster carousel |
| `coverage.json` | `fetch_country_stats` + a count | hero stat tiles |
| `views_timeseries.json` | per-movie daily `delta_views` + launch dates | the Time-Series chart (two films, ★ = new-trailer launch) |
| `engagement_decay.json` | `engagement_decay` | Time-Series stat line |
| `excitement_examples.json` | `movie_excitement_trend` | reserved — a future excitement card |
| `excitement_decay.json` | `comment_excitement_decay` | reserved / live decay summary |
| `mcp_examples.json` | all 10 MCP tools, called once | the MCP explorer |
| `distillation_static.json` | **hand-authored**, validated benchmark numbers | the dose-response chart + facts |
| `meta.json` | generator | footer "data refreshed" stamp |

`distillation_static.json` holds the offline acceptance-test results (Spearman ρ,
dose-response, proxy-blind) — these aren't in BigQuery, so they're committed once
and the refresh Action never touches them.

## One-time setup

1. Create a **read-only** BigQuery service account: `roles/bigquery.dataViewer`
   (dataset) + `roles/bigquery.jobUser` (project). No write/admin.
2. Add repo secrets: `GCP_SA_KEY` (the SA JSON), `GCP_PROJECT`, `BQ_DATASET`,
   `BQ_LOCATION`. (No TMDB/YouTube keys needed — they're optional in `config.py`.)
3. Settings → Pages → **Deploy from a branch** → `main` / `/docs`.
4. Trigger **Refresh site data** via *workflow_dispatch* once to seed `data/`.

> The excitement cards (Time Series chart, Distillation dose-response) are fully
> populated only when the target `BQ_DATASET` has excitement scores. Until the
> excitement tables are provisioned in prod, point `BQ_DATASET` at the dataset that
> has them, or those sections degrade gracefully to an empty state.
