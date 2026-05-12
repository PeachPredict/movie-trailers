Recommended next steps (not run automatically):

1. Create the GCP project + BQ dataset, apply schemas/bigquery.sql with ${DATASET} substituted.
2. Populate .env with TMDB + YouTube keys.
3. First dry-ish run: uv run mt run-daily --dataset=movie_trailers_dev --limit=20 --verbose.
4. Build/deploy the Cloud Run Job and wire Scheduler per the README.