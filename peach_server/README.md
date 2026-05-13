# peach_server transcripts runner

Runs only the **transcripts** phase of `mt run-daily`, from a residential IP.
The Cloud Run deployment skips this phase because YouTube blocks GCP egress IPs
(both `youtube-transcript-api` and `yt-dlp`).

## One-time setup

Auth uses **user ADC** (Application Default Credentials), not a service-account
key — the org policy `constraints/iam.disableServiceAccountKeyCreation` blocks
SA key creation in this project. The compose file mounts
`~/.config/gcloud` into the container read-only so the container picks up the
host's ADC automatically.

```sh
# On peach_server, in this directory:
cp .env.example .env                 # edit as needed

# Once per machine: produce ~/.config/gcloud/application_default_credentials.json
gcloud auth application-default login
gcloud auth application-default set-quota-project project-3a2a0060-1f47-4423-8ff

# Snap-installed Docker hides hidden files in $HOME, so copy the ADC to a
# non-hidden path adjacent to this compose file.
mkdir -p gcloud
cp ~/.config/gcloud/application_default_credentials.json gcloud/
chmod 600 gcloud/application_default_credentials.json

docker compose build
```

## Running

```sh
docker compose run --rm transcripts
```

This processes up to `TRANSCRIPTS_MAX_PER_RUN` trailers per invocation (default
500 in `.env.example`). Adjust there or override at the command line:

```sh
docker compose run --rm transcripts run-daily \
  --skip-movies --skip-tv --skip-stats --skip-comments \
  --transcripts-limit=100
```

## Scheduling

Add to peach_server's crontab to run nightly at 03:00 local time:

```cron
0 3 * * * cd /path/to/movie-trailers/peach_server && docker compose run --rm transcripts >> /var/log/mt-transcripts.log 2>&1
```

## Notes

- `WHISPER_MODEL_NAME` is locked to `small` in `.env.example` — that's what the
  baked-in model layer of the image contains. Changing this triggers a
  HuggingFace download on first run.
- `TMDB_API_KEY` / `YOUTUBE_API_KEY` are required by `Settings` but unused in
  this path; dummy values are fine.
- Transcripts already attempted (rows with `transcript_captured_at IS NOT NULL`)
  are skipped. To force a re-fetch, clear that column manually in BigQuery.
