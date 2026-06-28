"""Render findings + drafts into a single review email (HTML).

The email is for *you*: each finding shows a daily new-views sparkline and the
two draft variants in copy-paste boxes, with suggested platforms. Approve, edit,
post. Sparklines are inline SVG built by hand so there's no charting dependency.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from html import escape
from typing import Any

from movie_trailers.content.drafts import drafts_for
from movie_trailers.content.findings import Finding

_KIND_LABEL = {
    "trailer_due": "🎯 Prediction — trailer due",
    "surging": "🚀 Surging",
    "quality_slide": "📉 Cooling off",
    "excitement_fatigue": "🥱 Excitement fatigue",
}


def _sparkline(series: list[dict[str, object]], w: int = 260, h: int = 44) -> str:
    """Inline-SVG polyline of daily new-views; empty string if too few points."""
    vals = [float(p["dv"]) for p in series if p.get("dv") is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(vals)
    pts = " ".join(
        f"{i / (n - 1) * w:.1f},{h - (v - lo) / rng * h:.1f}" for i, v in enumerate(vals)
    )
    return (
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'style="background:#fafafa;border:1px solid #eee">'
        f'<polyline points="{pts}" fill="none" stroke="#d33" stroke-width="2"/></svg>'
    )


def _draft_box(label: str, text: str) -> str:
    body = escape(text).replace("\n", "<br>")
    return (
        f'<div style="margin:8px 0">'
        f'<div style="font:600 12px sans-serif;color:#666;text-transform:uppercase">{label}</div>'
        f'<div style="font:14px/1.5 sans-serif;background:#f6f8fa;border:1px solid #e1e4e8;'
        f'border-radius:6px;padding:12px;white-space:normal">{body}</div></div>'
    )


def _track_record_block(track: dict[str, Any]) -> str:
    hit = track.get("hits", 0)
    miss = track.get("misses", 0)
    open_ = track.get("open", 0)
    rate = track.get("hit_rate")
    rate_s = f"{rate:.0%}" if rate is not None else "—"
    lag = track.get("avg_hit_lag_days")
    lag_s = f", avg {lag:.1f}d to land" if lag is not None else ""
    return (
        f'<p style="font:14px sans-serif;color:#444;background:#f0f4f8;'
        f'border-radius:6px;padding:10px 12px">'
        f"📈 <b>Prediction track record:</b> {hit} hit / {miss} miss "
        f"(hit rate {rate_s}{lag_s}) · {open_} open</p>"
    )


def _finding_block(f: Finding, draft_fn: Callable[[Finding], dict[str, Any]]) -> str:
    d = draft_fn(f)
    label = _KIND_LABEL.get(f.kind, f.kind)
    platforms = " · ".join(escape(p) for p in d["platforms"])  # type: ignore[arg-type]
    return (
        f'<div style="border-top:2px solid #222;padding:16px 0">'
        f'<div style="font:700 18px sans-serif">{escape(f.title)}</div>'
        f'<div style="font:13px sans-serif;color:#888;margin:2px 0 8px">'
        f"{label} &nbsp;·&nbsp; salience {f.salience:.2f} &nbsp;·&nbsp; "
        f"best on: {platforms}</div>"
        f"{_sparkline(f.series)}"
        f"{_draft_box('Film-fan post', str(d['fan']))}"
        f"{_draft_box('Engineering / portfolio post', str(d['technical']))}"
        f"</div>"
    )


def render_content_section(
    findings: list[Finding],
    *,
    draft_fn: Callable[[Finding], dict[str, Any]] = drafts_for,
    track: dict[str, Any] | None = None,
) -> str:
    """The 'Predictions & draft posts' block — embeddable in the weekly digest."""
    parts = [
        '<h2 style="font-family:-apple-system,Helvetica,Arial,sans-serif;font-size:18px;'
        'margin:28px 0 8px 0;color:#222;">Predictions &amp; draft posts</h2>',
        '<p style="font:14px sans-serif;color:#666;margin:0 0 8px">Drafts only — review, '
        "edit, then post. Predictions are falsifiable; publish the misses too.</p>",
    ]
    if track:
        parts.append(_track_record_block(track))
    if findings:
        parts.append("".join(_finding_block(f, draft_fn) for f in findings))
    else:
        parts.append(
            '<p style="font:15px sans-serif;color:#666">No findings cleared the bar '
            "this week. Nothing worth posting beats posting nothing.</p>"
        )
    return "".join(parts)


def render_content_html(
    findings: list[Finding],
    *,
    today: date | None = None,
    draft_fn: Callable[[Finding], dict[str, Any]] = drafts_for,
    track: dict[str, Any] | None = None,
) -> str:
    today = today or date.today()
    return (
        f'<div style="max-width:680px;margin:0 auto">'
        f'<h1 style="font:700 22px sans-serif">Trailer content suggestions — {today.isoformat()}</h1>'
        f"{render_content_section(findings, draft_fn=draft_fn, track=track)}</div>"
    )
