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

Run nightly at **16:30 UTC**, ~30 min after the Cloud Run pipeline (which kicks
off at 14:00 UTC and typically wraps within two hours). That ordering matters:
the transcripts phase reads `trailers` rows the discover phase produced earlier
in the same day.

Snap-installed Docker isn't on cron's `PATH`, so the full binary path is
required. Pin the schedule to UTC explicitly — server local time would silently
drift relative to the Cloud Run schedule.

```cron
CRON_TZ=UTC
30 16 * * * cd /path/to/movie-trailers/peach_server && /snap/bin/docker compose run --rm transcripts >> $HOME/mt-transcripts.log 2>&1
```

Install non-destructively (preserves any existing entries):

```sh
(crontab -l 2>/dev/null; printf 'CRON_TZ=UTC\n30 16 * * * cd %s/peach_server && /snap/bin/docker compose run --rm transcripts >> $HOME/mt-transcripts.log 2>&1\n' "$(cd .. && pwd)") | crontab -
crontab -l
```

## MCP server (`mcp` service)

A long-running, **read-only** MCP server over streamable-HTTP, for a local MCP
client (Claude Desktop / IDE) on this machine. It reuses the same image, `.env`,
and mounted ADC as the batch services; it only ever reads BigQuery.

```sh
docker compose up -d mcp          # start (detached); restarts unless stopped
docker compose logs -f mcp        # tail
docker compose down mcp           # stop
```

The port is published to **`127.0.0.1` only** (`MCP_PORT`, default 8000), so the
endpoint is reachable from this machine and nowhere else — no LAN, no internet,
no auth layer needed. Point a local client at:

```
http://localhost:8000/mcp
```

e.g. in a Claude Desktop config (streamable-HTTP transport):

```json
{
  "mcpServers": {
    "movie-trailers": { "url": "http://localhost:8000/mcp" }
  }
}
```

Tool reference: [`../src/movie_trailers/mcp/README.md`](../src/movie_trailers/mcp/README.md).
To expose it beyond this machine later, front it with Tailscale / a tunnel +
auth — do **not** change the bind to `0.0.0.0` on the host side, since every tool
call runs a billable BigQuery query.

## Notes

- `WHISPER_MODEL_NAME` defaults to `medium` in `.env.example`, matching the
  `WHISPER_BAKE_MODEL` build arg in [compose.yaml](compose.yaml) so the model
  is already on disk inside the image. Changing either side without changing
  the other triggers a HuggingFace download on first run.
- `TMDB_API_KEY` / `YOUTUBE_API_KEY` are required by `Settings` but unused in
  this path; dummy values are fine.
- Transcripts already attempted (rows with `transcript_captured_at IS NOT NULL`)
  are skipped. To force a re-fetch, clear that column manually in BigQuery.
