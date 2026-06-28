# movie-trailers

Daily collector for YouTube trailer metadata of upcoming movies and TV shows worldwide.
Discovers releases via TMDB, captures daily view/like/comment stats and twice-per-trailer
top-30 comment snapshots from YouTube, and stores everything in BigQuery.

## Portfolio site

A static showcase (carousel + Data-Engineering / Time-Series / MCP / Model-Distillation
cards) lives in [`docs/`](docs/) and is served on GitHub Pages at
**https://peachpredict.github.io/movie-trailers/**. Its data is read-only JSON snapshots
written by `uv run mt generate-site-data` and refreshed by a scheduled GitHub Action — see
[`docs/README.md`](docs/README.md).

## Local setup

```sh
uv sync
cp .env.example .env  # then fill in keys (see below)
```

Required env vars (see `src/movie_trailers/config.py`):

| Var | Notes |
|---|---|
| `TMDB_API_KEY` | TMDB v3 API key |
| `YOUTUBE_API_KEY` | YouTube Data API v3 key (default 10k unit/day quota) |
| `GCP_PROJECT` | GCP project hosting BigQuery |
| `BQ_DATASET` | Defaults to `movie_trailers` |
| `BQ_LOCATION` | Defaults to `us-central1` |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_SENDER` / `SMTP_PASSWORD` | Required only for `mt send-digest`. Port defaults to 587 (STARTTLS). `SMTP_SENDER` is both the From address and the SMTP login (e.g. Yahoo: `smtp.mail.yahoo.com:587`, app password). Override the login with `SMTP_USERNAME` if it differs. |
| `SMTP_SSL` | Set to `true` to use implicit TLS (SMTPS, port 465). Defaults to `false` (STARTTLS). |
| `DIGEST_EMAIL_TO` | Comma-separated recipients for the digest. Override per-call with `--to`. |
| `DIGEST_TOP_TRACKED` | Cap for the "currently tracked" section (top-N by Δviews). Defaults to 50. |
| `TMDB_IMAGE_BASE` | TMDB CDN prefix for poster `<img>` URLs. Defaults to `https://image.tmdb.org/t/p/w154`. |

## Provision BigQuery

```sh
bq --location=us-central1 mk -d "${GCP_PROJECT}:movie_trailers"
# Substitute ${DATASET} in the DDL and run:
sed "s/\${DATASET}/${GCP_PROJECT}.movie_trailers/g" schemas/bigquery.sql \
  | bq query --use_legacy_sql=false --project_id="${GCP_PROJECT}"
```

## Run the pipeline

```sh
uv run mt run-daily --verbose
# Dry-run a small batch first:
uv run mt run-daily --dataset=movie_trailers_dev --limit=20 --verbose
# Single phase only:
uv run mt run-daily --skip-movies --skip-tv --skip-comments --limit=20
```

## Weekly / monthly digest email

```sh
# Preview the HTML without sending (writes to stdout or --out file).
uv run mt send-digest --period=week --dry-run --out /tmp/digest.html

# Send for real (needs SMTP_* + DIGEST_EMAIL_* env vars).
uv run mt send-digest --period=week
uv run mt send-digest --period=month         # later, when the monthly cadence kicks in
```

The digest has two parts: **database statistics by country** (per-origin-country counts of trailers, transcripts, comments, and active / ended / unavailable status) and a **Predictions & draft posts** section — the trailer-prediction scoreboard plus this period's draft social posts. The predictions themselves are logged by the daily run's `predictions` phase (not here); the digest only displays them. Nothing is posted anywhere — you review the drafts and publish manually. Add `--polish` (needs `ANTHROPIC_API_KEY`) to have Claude rewrite the drafts; it falls back to templates without a key.

For Cloud Run, deploy a second job and a weekly Scheduler entry alongside the daily one:

```sh
gcloud run jobs deploy mt-digest \
  --image "us-central1-docker.pkg.dev/${GCP_PROJECT}/mt/mt:latest" \
  --region us-central1 \
  --command mt --args "send-digest,--period=week,--polish" \
  --set-env-vars "GCP_PROJECT=${GCP_PROJECT},BQ_DATASET=movie_trailers,SMTP_HOST=...,SMTP_PORT=587,SMTP_USERNAME=...,SMTP_PASSWORD=...,DIGEST_EMAIL_FROM=...,DIGEST_EMAIL_TO=...,ANTHROPIC_API_KEY=..."
gcloud scheduler jobs create http mt-digest-weekly \
  --location us-central1 \
  --schedule "0 15 * * 1"  `# Mondays 15:00 UTC, after Monday's daily run` \
  --uri "https://us-central1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${GCP_PROJECT}/jobs/mt-digest:run" \
  --oauth-service-account-email "${SCHEDULER_SA}"
```

## Tests + lint

```sh
uv run pytest
uv run ruff check src tests
```

## Deploy to Cloud Run Job

```sh
gcloud builds submit --tag "us-central1-docker.pkg.dev/${GCP_PROJECT}/mt/mt:latest"
gcloud run jobs deploy mt-daily \
  --image "us-central1-docker.pkg.dev/${GCP_PROJECT}/mt/mt:latest" \
  --region us-central1 \
  --set-env-vars "GCP_PROJECT=${GCP_PROJECT},BQ_DATASET=movie_trailers,TMDB_API_KEY=...,YOUTUBE_API_KEY=..." \
  --task-timeout 3600 \
  --max-retries 1
gcloud scheduler jobs create http mt-daily-schedule \
  --location us-central1 \
  --schedule "0 14 * * *" \
  --uri "https://us-central1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${GCP_PROJECT}/jobs/mt-daily:run" \
  --oauth-service-account-email "${SCHEDULER_SA}"
```

The `0 14 * * *` UTC schedule = 07:00 Pacific, clear of the YouTube quota reset boundary.
