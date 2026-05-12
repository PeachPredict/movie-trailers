from __future__ import annotations

import logging
import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

import structlog
import typer

from movie_trailers import __version__
from movie_trailers.clients.bigquery import BigQueryClient
from movie_trailers.clients.tmdb import TMDBClient
from movie_trailers.clients.youtube import YouTubeClient
from movie_trailers.config import load_settings
from movie_trailers.models import DailyRunLogRow
from movie_trailers.pipeline.comments import run_comments
from movie_trailers.pipeline.discover_movies import run_discover_movies
from movie_trailers.pipeline.discover_tv import run_discover_tv
from movie_trailers.pipeline.stats import run_stats

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


if __name__ == "__main__":
    app()
