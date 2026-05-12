# movie-trailers

Daily collector for YouTube trailer metadata of upcoming movies and TV shows worldwide.
Discovers releases via TMDB, captures daily view/like/comment stats and twice-per-trailer
top-30 comment snapshots from YouTube, and stores everything in BigQuery.

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
