"""HTML renderer for the digest email.

Table-based layout for broad email-client support. Posters reference TMDB's
public CDN by URL — no inline attachments, so the email body stays small.
"""

from __future__ import annotations

from datetime import date, datetime
from html import escape
from typing import Any


def _yt_link(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={escape(video_id)}"


def _poster_img(image_base: str, poster_path: str | None) -> str:
    if not poster_path:
        return ""
    src = f"{image_base.rstrip('/')}{poster_path}"
    return (
        f'<img src="{escape(src)}" alt="" width="92" '
        f'style="display:block;border:0;border-radius:4px;">'
    )


def _fmt_int(n: Any) -> str:
    if n is None:
        return "—"
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "—"


def _fmt_date(value: Any) -> str:
    """Render a date / datetime / ISO-string value as YYYY-MM-DD."""
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    s = str(value)
    # BQ may hand back ISO timestamps like "2026-05-15T07:54:00+00:00"; trim to date.
    return s[:10] if len(s) >= 10 else s


def _fmt_delta(n: Any) -> str:
    if n is None:
        return "—"
    try:
        v = int(n)
    except (TypeError, ValueError):
        return "—"
    if v > 0:
        return f"+{v:,}"
    return f"{v:,}"


def _row(cells: list[str]) -> str:
    return "<tr>" + "".join(
        f'<td style="padding:8px 10px;border-bottom:1px solid #eee;vertical-align:top;font-size:14px;">{c}</td>'
        for c in cells
    ) + "</tr>"


def _section_title(text: str) -> str:
    return (
        f'<h2 style="font-family:-apple-system,Helvetica,Arial,sans-serif;'
        f'font-size:18px;margin:28px 0 8px 0;color:#222;">{escape(text)}</h2>'
    )


def _table_open(headers: list[str]) -> str:
    th_html = "".join(
        f'<th align="left" style="padding:8px 10px;background:#f5f5f7;border-bottom:1px solid #ddd;font-size:13px;color:#555;">{escape(h)}</th>'
        for h in headers
    )
    return (
        '<table cellspacing="0" cellpadding="0" border="0" width="100%" '
        'style="border-collapse:collapse;font-family:-apple-system,Helvetica,Arial,sans-serif;">'
        f"<thead><tr>{th_html}</tr></thead><tbody>"
    )


def _table_close() -> str:
    return "</tbody></table>"


def _trailer_title_cell(title: str | None, trailer_name: str | None, video_id: str) -> str:
    title_html = escape(title or "—")
    name_html = escape(trailer_name or "(untitled trailer)")
    href = _yt_link(video_id)
    return (
        f'<div style="font-weight:600;color:#111;">{title_html}</div>'
        f'<div style="font-size:13px;color:#555;margin-top:2px;">'
        f'<a href="{href}" style="color:#1a73e8;text-decoration:none;">{name_html}</a>'
        f'</div>'
    )


def _render_new_trailers(rows: list[dict[str, Any]], image_base: str) -> str:
    if not rows:
        return _section_title("New trailers added") + _empty_note(
            "No new trailers were discovered in this period."
        )
    body = [_section_title(f"New trailers added ({len(rows)})")]
    body.append(_table_open([
        "Poster", "Title / Trailer", "Type", "Trailer Date", "Release Date", "Views", "Likes"
    ]))
    for r in rows:
        body.append(_row([
            _poster_img(image_base, r.get("poster_path")),
            _trailer_title_cell(r.get("title"), r.get("trailer_name"), r["youtube_video_id"]),
            escape(str(r.get("video_type") or "")),
            _fmt_date(r.get("trailer_published_at")),
            _fmt_date(r.get("release_date")),
            _fmt_int(r.get("view_count")),
            _fmt_int(r.get("like_count")),
        ]))
    body.append(_table_close())
    return "".join(body)


def _render_top_tracked(rows: list[dict[str, Any]], image_base: str, top_n: int) -> str:
    if not rows:
        return _section_title("Currently tracked trailers") + _empty_note(
            "No tracked trailers had stats in this period."
        )
    body = [_section_title(
        f"Currently tracked — most recent {len(rows)} by trailer date (cap {top_n})"
    )]
    body.append(_table_open([
        "Poster", "Title / Trailer", "Trailer Date", "Release Date",
        "Δ Views", "Total Views", "Δ Likes", "Total Likes",
    ]))
    for r in rows:
        body.append(_row([
            _poster_img(image_base, r.get("poster_path")),
            _trailer_title_cell(r.get("title"), r.get("trailer_name"), r["youtube_video_id"]),
            _fmt_date(r.get("trailer_published_at")),
            _fmt_date(r.get("release_date")),
            _fmt_delta(r.get("delta_views")),
            _fmt_int(r.get("view_count")),
            _fmt_delta(r.get("delta_likes")),
            _fmt_int(r.get("like_count")),
        ]))
    body.append(_table_close())
    return "".join(body)


def _render_country_stats(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return _section_title("Database statistics by country") + _empty_note(
            "No trailers in the database yet."
        )
    body = [_section_title("Database statistics by country")]
    body.append(_table_open([
        "Country", "Trailers", "Active", "Ended", "Unavailable", "Transcripts", "With comments"
    ]))
    for r in rows:
        body.append(_row([
            escape(str(r.get("country") or "—")),
            _fmt_int(r.get("total_trailers")),
            _fmt_int(r.get("active_trailers")),
            _fmt_int(r.get("ended_trailers")),
            _fmt_int(r.get("unavailable_trailers")),
            _fmt_int(r.get("with_transcript")),
            _fmt_int(r.get("with_comments")),
        ]))
    body.append(_table_close())
    body.append(
        '<div style="font-size:12px;color:#888;margin-top:6px;">'
        "A trailer with multiple origin countries is counted in each. "
        "Column totals can therefore exceed the unique trailer count."
        "</div>"
    )
    return "".join(body)


def _empty_note(text: str) -> str:
    return (
        f'<div style="font-size:14px;color:#666;font-style:italic;'
        f'font-family:-apple-system,Helvetica,Arial,sans-serif;">{escape(text)}</div>'
    )


def render_digest_html(
    *,
    period: str,
    cutoff_date: date,
    today: date,
    new_trailers: list[dict[str, Any]],
    top_tracked: list[dict[str, Any]],
    country_stats: list[dict[str, Any]],
    image_base: str,
    top_tracked_cap: int,
) -> str:
    header = (
        '<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;color:#222;">'
        f'<h1 style="font-size:22px;margin:0 0 4px 0;">Movie-trailers '
        f'{escape(period)}ly digest</h1>'
        f'<div style="font-size:13px;color:#666;">Window: {cutoff_date.isoformat()} → '
        f'{today.isoformat()}</div>'
        "</div>"
    )
    sections = [
        _render_new_trailers(new_trailers, image_base),
        _render_top_tracked(top_tracked, image_base, top_tracked_cap),
        _render_country_stats(country_stats),
    ]
    body = header + "".join(sections)
    return (
        "<!doctype html>"
        '<html><body style="margin:0;padding:24px;background:#fff;">'
        f"{body}"
        "</body></html>"
    )
