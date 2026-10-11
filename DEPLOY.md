Recommended next steps (not run automatically):

1. Create the GCP project + BQ dataset, apply schemas/bigquery.sql with ${DATASET} substituted.
2. Populate .env with TMDB + YouTube keys.
3. First dry-ish run: uv run mt run-daily --dataset=movie_trailers_dev --limit=20 --verbose.
4. Build/deploy the Cloud Run Job and wire Scheduler per the README.
## 2026-10-11 release: data-quality fixes + box-office outcomes

Order matters — code that reads the new columns ships after the DDL is applied.

1. **BigQuery (prod `movie_trailers`)** — run the migrations section at the end of
   `schemas/bigquery.sql` with `${DATASET}` substituted: the three `ALTER TABLE … ADD COLUMN
   IF NOT EXISTS` statements, then `CREATE TABLE box_office_observations` and
   `CREATE OR REPLACE VIEW vw_movie_box_office` (the empty `box_office` table can be dropped).
   Optional: the `trailer_comment_excitement` table + `vw_movie_trailer_excitement` view so the
   excitement phase can run in prod.
2. **Cloud Run job** — add `OMDB_API_KEY` to `--set-env-vars` (free key, 1,000 calls/day),
   rebuild the image via Cloud Build, `gcloud run jobs update mt-daily --image …`. Keep
   `--skip-transcripts --skip-excitement` in the job args; the new `box_office` phase needs no flag.
3. **peach_server** — `git pull`, `docker compose build transcripts` (new yt-dlp), then one
   repair pass: `docker compose run --rm transcripts requeue-transcripts --where music --where
   hallucinated --where transient --dry-run` (drop `--dry-run` to apply). Add the weekly `ancine`
   cron (peach_server/README.md) and set `ANCINE_SOURCE` if the ANCINE URL is blocked.
4. **One-off search cleanup (any machine with BQ access)** —
   `uv run mt recheck-search-trailers --dry-run`, review the list (33 rows at the time of
   writing: games, TV seasons, same-title other films), then run it without `--dry-run`.
5. **Verify** after the next daily run: `daily_run_log` has `transcripts` (peach_server) and
   `box_office` rows with `errors=0`; `SELECT quality, COUNT(*) FROM trailer_transcripts GROUP BY 1`;
   `mt send-digest --period=week --dry-run --out /tmp/d.html` shows the "Resolved since" and
   "Trailer signal vs box office" sections.

## 2026-10-11 release (b): Indian box office + news signal

1. **BigQuery (prod)** — `CREATE` the three tables `sacnilk_pages`, `wikipedia_pages`,
   `movie_news_daily` from `schemas/bigquery.sql` (no ALTERs). The GDELT
   public dataset needs no setup; the query runs in our project and is billed to it (~$0.01/day).
2. **Cloud Run job** — rebuild + update as above. New phases `news`, `sacnilk`, `wikipedia`
   need no env vars (`CONTACT_EMAIL` overrides the User-Agent contact). First run: pass
   `--news-days 30` once to backfill the news signal.
3. **Watch** the first runs' `daily_run_log`: `sacnilk` with `pages>0`; if the logs show
   `sacnilk.blocked`, add `--skip-sacnilk` to the job args and enable the peach_server
   `sacnilk` cron (peach_server/README.md). `wikipedia` should report `observations>0` within
   a week for released titles.
