"""HTML renderer for the digest email.

Table-based layout for broad email-client support. The digest aggregates
trailer counts by origin country — no per-trailer detail.
"""

from __future__ import annotations

from datetime import date
from html import escape
from typing import Any


def _fmt_int(n: Any) -> str:
    if n is None:
        return "—"
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "—"


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


def _empty_note(text: str) -> str:
    return (
        f'<div style="font-size:14px;color:#666;font-style:italic;'
        f'font-family:-apple-system,Helvetica,Arial,sans-serif;">{escape(text)}</div>'
    )


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


def render_digest_html(
    *,
    period: str,
    today: date,
    country_stats: list[dict[str, Any]],
) -> str:
    header = (
        '<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;color:#222;">'
        f'<h1 style="font-size:22px;margin:0 0 4px 0;">Movie-trailers '
        f'{escape(period)}ly digest</h1>'
        f'<div style="font-size:13px;color:#666;">As of {today.isoformat()}</div>'
        "</div>"
    )
    body = header + _render_country_stats(country_stats)
    return (
        "<!doctype html>"
        '<html><body style="margin:0;padding:24px;background:#fff;">'
        f"{body}"
        "</body></html>"
    )
