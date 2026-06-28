#!/usr/bin/env bash
# Regenerate the portfolio site's read-only JSON snapshots on peach_server and
# push them to main. Runs from cron (see README). The box has BigQuery ADC, so
# this replaces the GitHub Action that couldn't auth (org policy blocks SA keys).
#
# - Pulls main fast-forward (picks up code/compose changes pushed from the Mac).
# - Runs the `site-data` compose service, which writes docs/data/*.json on the host.
# - Commits + pushes only if something changed. [skip ci] keeps Pages-only repos quiet.
#
# Push auth is a per-repo deploy key via the `origin` remote's github-mt host
# alias (see README "Site-data refresh"); independent of any user GitHub account.
set -euo pipefail

cd "$(dirname "$0")/.."   # repo root

git pull --ff-only origin main || true

/snap/bin/docker compose -f peach_server/compose.yaml run --rm site-data

if git diff --quiet -- docs/data; then
  echo "$(date -u +%FT%TZ) no docs/data changes"
  exit 0
fi

git add docs/data
git commit -m "chore(site): refresh data snapshots [skip ci]"
git push origin main
echo "$(date -u +%FT%TZ) pushed refreshed docs/data"
