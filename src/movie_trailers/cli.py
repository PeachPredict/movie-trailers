from __future__ import annotations

import logging
import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

import structlog
import typer

from movie_trailers import __version__
from movie_trailers.clients.bigquery import BigQueryClient
from movie_trailers.clients.tmdb import TMDBClient
from movie_trailers.clients.youtube import YouTubeClient
from movie_trailers.config import load_settings
from movie_trailers.digest.mailer import send_email
from movie_trailers.digest.queries import fetch_country_stats
from movie_trailers.digest.render import render_digest_html
from movie_trailers.models import DailyRunLogRow
from movie_trailers.pipeline.comments import run_comments
from movie_trailers.pipeline.discover_movies import run_discover_movies
from movie_trailers.pipeline.discover_tv import run_discover_tv
from movie_trailers.pipeline.stats import run_stats
from movie_trailers.pipeline.transcripts import run_transcripts

app = typer.Typer(add_completion=False, help="Movie/TV trailer metadata collector.")


@app.command("version")
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
    )
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if verbose else logging.INFO
        ),
    )


log = structlog.get_logger()


@app.command("run-daily")
def run_daily(
    dataset: str | None = typer.Option(None, help="Override BQ_DATASET for this run."),
    limit: int | None = typer.Option(None, help="Cap movies/shows/trailers per phase (testing)."),
    skip_movies: bool = typer.Option(False, help="Skip discover_movies."),
    skip_tv: bool = typer.Option(False, help="Skip discover_tv."),
    skip_stats: bool = typer.Option(False, help="Skip stats."),
    skip_comments: bool = typer.Option(False, help="Skip comments."),
    skip_excitement: bool = typer.Option(False, help="Skip comment-excitement scoring."),
    skip_transcripts: bool = typer.Option(False, help="Skip transcripts."),
    skip_predictions: bool = typer.Option(False, help="Skip the prediction log update."),
    transcripts_limit: int | None = typer.Option(
        None, help="Override TRANSCRIPTS_MAX_PER_RUN for this run."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the full daily pipeline: discover (movies + tv) → stats → comments."""
    _configure_logging(verbose)
    settings = load_settings()
    if dataset:
        settings.bq_dataset = dataset

    run_id = uuid.uuid4().hex
    log.info("run.start", run_id=run_id, dataset=settings.bq_dataset)

    tmdb = TMDBClient(settings.tmdb_api_key)
    youtube = YouTubeClient(settings.youtube_api_key)
    bq = BigQueryClient(
        project=settings.gcp_project,
        dataset=settings.bq_dataset,
        location=settings.bq_location,
    )

    log_rows: list[DailyRunLogRow] = []

    try:
        if not skip_movies:
            with _phase(run_id, "discover_movies", log_rows) as ctx:
                movies, trailers = run_discover_movies(
                    tmdb=tmdb, bq=bq, settings=settings, limit=limit
                )
                ctx["trailers_processed"] = trailers
                ctx["notes"] = f"movies={movies}"

        if not skip_tv:
            with _phase(run_id, "discover_tv", log_rows) as ctx:
                shows, seasons, trailers = run_discover_tv(
                    tmdb=tmdb, bq=bq, settings=settings, limit=limit
                )
                ctx["trailers_processed"] = trailers
                ctx["notes"] = f"shows={shows} seasons={seasons}"

        if not skip_stats:
            with _phase(run_id, "stats", log_rows) as ctx:
                n = run_stats(youtube=youtube, bq=bq, settings=settings, limit=limit)
                ctx["trailers_processed"] = n
                ctx["quota_units_used"] = youtube.quota_units_used

        quota_before_comments = youtube.quota_units_used
        if not skip_comments:
            with _phase(run_id, "comments", log_rows) as ctx:
                at_disco, pre_rel = run_comments(
                    youtube=youtube, bq=bq, settings=settings, limit=limit
                )
                ctx["trailers_processed"] = at_disco + pre_rel
                ctx["quota_units_used"] = (
                    youtube.quota_units_used - quota_before_comments
                )
                ctx["notes"] = f"at_discovery={at_disco} pre_release={pre_rel}"

        # Comment-excitement scoring: distilled local model (ONNX MiniLM + Ridge),
        # zero quota / zero LLM cost. Runs after comments (needs at_discovery
        # snapshots); feeds the per-movie excitement-decay metric.
        if not skip_excitement:
            with _phase(run_id, "excitement", log_rows) as ctx:
                from movie_trailers.pipeline.excitement import run_excitement

                scored, skipped = run_excitement(
                    bq=bq, settings=settings, today=date.today(), limit=limit
                )
                ctx["trailers_processed"] = scored
                ctx["notes"] = f"scored={scored} skipped={skipped}"

        if not skip_transcripts:
            with _phase(run_id, "transcripts", log_rows) as ctx:
                yta_ok, whisper_ok, failures = run_transcripts(
                    bq=bq, settings=settings, limit=transcripts_limit
                )
                ctx["trailers_processed"] = yta_ok + whisper_ok + failures
                ctx["notes"] = (
                    f"yta_ok={yta_ok} whisper_ok={whisper_ok} failures={failures}"
                )

        # Prediction log: record new trailer-due calls + resolve past ones.
        # Private, read-mostly, no posting — builds the track record daily so the
        # weekly digest can report an unbiased hit rate.
        if not skip_predictions:
            with _phase(run_id, "predictions", log_rows) as ctx:
                from movie_trailers.content.findings import detect_findings
                from movie_trailers.content.predictions import (
                    record_predictions,
                    resolve_predictions,
                )

                today = date.today()
                findings = detect_findings(bq, days=30, top=50)
                resolved = resolve_predictions(bq, today=today)
                recorded = record_predictions(bq, findings, today=today)
                ctx["trailers_processed"] = recorded
                ctx["notes"] = (
                    f"recorded={recorded} hits={resolved['hits']} "
                    f"misses={resolved['misses']}"
                )
    finally:
        _persist_run_log(bq, log_rows)
        tmdb.close()
        youtube.close()

    log.info("run.end", run_id=run_id, quota_total=youtube.quota_units_used)


class _PhaseCtx(dict):  # type: ignore[type-arg]
    pass


@contextmanager
def _phase(run_id: str, phase: str, log_rows: list[DailyRunLogRow]):
    started = datetime.now(UTC)
    ctx = _PhaseCtx()
    errors = 0
    try:
        log.info("phase.start", run_id=run_id, phase=phase)
        yield ctx
    except Exception as exc:
        errors = 1
        log.exception("phase.failed", run_id=run_id, phase=phase, error=str(exc))
        raise
    finally:
        log_rows.append(
            DailyRunLogRow(
                run_id=run_id,
                phase=phase,  # type: ignore[arg-type]
                started_at=started,
                finished_at=datetime.now(UTC),
                trailers_processed=ctx.get("trailers_processed"),
                quota_units_used=ctx.get("quota_units_used"),
                errors=errors,
                notes=ctx.get("notes"),
            )
        )
        log.info("phase.end", run_id=run_id, phase=phase)


def _persist_run_log(bq: BigQueryClient, rows: list[DailyRunLogRow]) -> None:
    if not rows:
        return
    try:
        fields = [
            "run_id", "phase", "started_at", "finished_at",
            "trailers_processed", "quota_units_used", "errors", "notes",
        ]
        bq.merge_rows(
            table="daily_run_log",
            rows=rows,
            merge_keys=["run_id", "phase"],
            update_fields=[c for c in fields if c not in {"run_id", "phase"}],
            insert_fields=fields,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("run_log.persist_failed", error=str(exc))


@app.command("suggest-content")
def suggest_content(
    days: int = typer.Option(30, help="Lookback window for the scan."),
    top: int = typer.Option(6, help="Max findings to include."),
    polish: bool = typer.Option(
        False, "--polish", help="Rewrite drafts with Claude (needs ANTHROPIC_API_KEY)."
    ),
    dataset: str | None = typer.Option(None, help="Override BQ_DATASET for this run."),
    to: str | None = typer.Option(None, "--to", help="Recipients; overrides DIGEST_EMAIL_TO."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Render HTML to --out (or stdout); do not send."
    ),
    out: str | None = typer.Option(None, help="When --dry-run: write HTML here instead of stdout."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Preview/email the content section standalone (read-only).

    Scans for newsworthy movie states and renders dual-format drafts + the
    prediction scoreboard. Read-only: the prediction log is written by the daily
    pipeline (`run-daily`), not here. The same section is folded into the weekly
    `send-digest` email — this command is for ad-hoc previews. Drafts nothing to
    studios; a human publishes.
    """
    from movie_trailers.content.findings import detect_findings
    from movie_trailers.content.polish import make_draft_fn
    from movie_trailers.content.predictions import track_record
    from movie_trailers.content.render import render_content_html

    _configure_logging(verbose)
    settings = load_settings()
    if dataset:
        settings.bq_dataset = dataset

    today = date.today()
    bq = BigQueryClient(
        project=settings.gcp_project, dataset=settings.bq_dataset, location=settings.bq_location
    )

    log.info("content.scan", days=days, top=top)
    findings = detect_findings(bq, days=days, top=top)
    log.info("content.found", count=len(findings))
    track = track_record(bq)

    draft_fn = make_draft_fn(settings, polish=polish)
    html = render_content_html(findings, today=today, draft_fn=draft_fn, track=track)

    if dry_run:
        if out:
            Path(out).write_text(html, encoding="utf-8")
            log.info("content.dry_run.wrote", path=out)
        else:
            typer.echo(html)
        return

    recipients_raw = to or settings.digest_email_to
    sender, host = settings.smtp_sender, settings.smtp_host
    if not recipients_raw or not sender or not host:
        raise typer.BadParameter(
            "suggest-content requires SMTP_HOST, SMTP_SENDER, and DIGEST_EMAIL_TO "
            "(or --to). Use --dry-run to preview without sending."
        )
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    subject = f"Trailer content suggestions — {today.isoformat()}"
    log.info("content.send", recipients=recipients)
    send_email(
        host=host,
        port=settings.smtp_port,
        username=settings.smtp_username or sender,
        password=settings.smtp_password,
        sender=sender,
        recipients=recipients,
        subject=subject,
        html=html,
        starttls=not settings.smtp_ssl,
    )
    log.info("content.sent", recipients=recipients)


@app.command("mcp")
def mcp(
    http: bool = typer.Option(
        False,
        "--http",
        help="Serve over streamable-HTTP instead of stdio (long-running local server at /mcp).",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        help="Bind address for --http. Default 127.0.0.1 keeps it reachable only from this machine.",
    ),
    port: int = typer.Option(8000, help="TCP port for --http."),
) -> None:
    """Serve the read-only trailer dataset over MCP.

    Default (stdio): point a Claude Desktop / IDE MCP client at `uv run mt mcp`.
    With --http: run a long-running server (e.g. the peach_server `mcp` compose
    service); connect a client to http://<host>:<port>/mcp. Needs the same
    GCP_PROJECT / BQ_DATASET env as the pipeline; performs no writes.
    """
    from movie_trailers.mcp.server import main as serve_mcp

    if http:
        serve_mcp(transport="streamable-http", host=host, port=port)
    else:
        serve_mcp()


@app.command("send-digest")
def send_digest(
    period: str = typer.Option("week", help="Digest window: 'week' or 'month'."),
    dataset: str | None = typer.Option(None, help="Override BQ_DATASET for this run."),
    to: str | None = typer.Option(
        None,
        "--to",
        help="Comma-separated recipients. Overrides DIGEST_EMAIL_TO.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Render HTML and write to --out (or stdout); do not send email.",
    ),
    out: str | None = typer.Option(None, help="When --dry-run: write HTML here instead of stdout."),
    polish: bool = typer.Option(
        False, "--polish", help="Rewrite the draft posts with Claude (needs ANTHROPIC_API_KEY)."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Build and email the weekly/monthly digest: country stats + predictions & drafts.

    Read-only. The country aggregation plus a 'Predictions & draft posts' section
    (the prediction scoreboard and this period's draft posts). Predictions are
    written by `run-daily`, not here — this only displays them. Nothing is posted
    anywhere; you review the drafts and publish manually.
    """
    from movie_trailers.content.findings import detect_findings
    from movie_trailers.content.polish import make_draft_fn
    from movie_trailers.content.predictions import track_record
    from movie_trailers.content.render import render_content_section

    _configure_logging(verbose)
    settings = load_settings()
    if dataset:
        settings.bq_dataset = dataset

    today = date.today()

    bq = BigQueryClient(
        project=settings.gcp_project,
        dataset=settings.bq_dataset,
        location=settings.bq_location,
    )

    log.info("digest.fetch", period=period)
    countries = fetch_country_stats(bq)
    findings = detect_findings(bq, days=30, top=6)
    track = track_record(bq)
    log.info("digest.fetched", countries=len(countries), findings=len(findings))

    content_html = render_content_section(
        findings, draft_fn=make_draft_fn(settings, polish=polish), track=track
    )
    html = render_digest_html(
        period=period, today=today, country_stats=countries, content_html=content_html
    )

    if dry_run:
        if out:
            out_path = Path(out)
            out_path.write_text(html, encoding="utf-8")
            log.info("digest.dry_run.wrote", path=str(out_path))
        else:
            typer.echo(html)
        return

    recipients_raw = to or settings.digest_email_to
    sender = settings.smtp_sender
    host = settings.smtp_host
    if not recipients_raw or not sender or not host:
        raise typer.BadParameter(
            "send-digest requires SMTP_HOST, SMTP_SENDER, and DIGEST_EMAIL_TO "
            "(or --to). Use --dry-run to preview without sending."
        )
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    subject = f"Movie-trailers {period}ly digest — {today.isoformat()}"

    log.info("digest.send", host=host, recipients=recipients, subject=subject)
    send_email(
        host=host,
        port=settings.smtp_port,
        username=settings.smtp_username or sender,
        password=settings.smtp_password,
        sender=sender,
        recipients=recipients,
        subject=subject,
        html=html,
        starttls=not settings.smtp_ssl,
    )
    log.info("digest.sent", recipients=recipients)


@app.command("generate-site-data")
def generate_site_data_cmd(
    dataset: str | None = typer.Option(None, help="Override BQ_DATASET for this run."),
    out_dir: str = typer.Option("docs/data", help="Directory to write JSON snapshots."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Write read-only JSON snapshots for the static portfolio site (docs/data).

    Calls only the existing read-only query layer (no writes, no YouTube quota) and
    emits carousel / excitement / engagement / coverage / MCP-example / meta JSON for
    the GitHub Pages site. Refreshed on a schedule by a GitHub Action.
    """
    from movie_trailers.site.builder import generate_site_data

    _configure_logging(verbose)
    settings = load_settings()
    if dataset:
        settings.bq_dataset = dataset
    bq = BigQueryClient(
        project=settings.gcp_project,
        dataset=settings.bq_dataset,
        location=settings.bq_location,
    )
    log.info("site.generate", out_dir=out_dir, dataset=settings.bq_dataset)
    written = generate_site_data(bq, settings, out_dir)
    log.info("site.generated", files=list(written))


if __name__ == "__main__":
    app()
