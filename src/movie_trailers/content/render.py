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
    "opening_tier": "🎟️ Prediction — opening tier",
    "surging": "🚀 Surging",
    "quality_slide": "📉 Cooling off",
    "excitement_fatigue": "🥱 Excitement fatigue",
    "media_surge": "📰 Media surge",
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


_KIND_SHORT = {"trailer_due": "trailer due", "opening_tier": "opening tier"}
_TD = 'style="padding:4px 8px;border-bottom:1px solid #eee;font:13px sans-serif;vertical-align:top"'
_TH = 'style="padding:4px 8px;border-bottom:2px solid #ccc;font:600 12px sans-serif;text-align:left;color:#555"'


def _score_line(s: dict[str, Any]) -> str:
    rate = s.get("hit_rate")
    rate_s = f"{rate:.0%}" if rate is not None else "—"
    lag = s.get("avg_hit_lag_days")
    lag_s = f", avg {lag:.1f}d to land" if lag is not None else ""
    unsc = s.get("unscorable") or 0
    unsc_s = f" · {unsc} unscorable" if unsc else ""
    return (
        f"{s.get('hits', 0)} hit / {s.get('misses', 0)} miss (hit rate {rate_s}{lag_s}) "
        f"· {s.get('open', 0)} open{unsc_s}"
    )


def _track_record_block(track: dict[str, Any]) -> str:
    by_kind = track.get("by_kind") or {}
    lines = [f"📈 <b>Prediction track record:</b> {_score_line(track)}"]
    if len(by_kind) > 1 or (by_kind and "opening_tier" in by_kind):
        for kind, s in by_kind.items():
            lines.append(f"&nbsp;&nbsp;· <b>{escape(_KIND_SHORT.get(kind, kind))}:</b> {_score_line(s)}")
    return (
        '<p style="font:14px sans-serif;color:#444;background:#f0f4f8;'
        'border-radius:6px;padding:10px 12px">' + "<br>".join(lines) + "</p>"
    )


def _num(v: Any, money: bool = False) -> str:
    if v is None:
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return escape(str(v))
    if money:
        return f"${n / 1e6:.1f}M" if n >= 1e6 else f"${n:,.0f}"
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n:,.0f}"


def _inr(v: Any) -> str:
    if v is None:
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return escape(str(v))
    return f"₹{n / 1e7:.1f} Cr" if n >= 1e6 else f"₹{n:,.0f}"


def _outcome_text(r: dict[str, Any]) -> str:
    """What happened, for one resolved prediction row (see predictions.resolved_since)."""
    if r.get("kind") == "trailer_due":
        if r.get("status") == "hit":
            name = r.get("resolved_trailer_name") or r.get("resolved_youtube_video_id") or "a trailer"
            return f"“{escape(str(name))}” landed after {r.get('resolved_lag_days')}d"
        return f"no trailer within {r.get('horizon_days')}d"
    metric = r.get("observed_metric")
    if metric == "none":
        return "no box-office data arrived (unscorable)"
    label = {"admissions": "BR admissions", "domestic_gross_usd": "US gross",
             "revenue_usd": "WW revenue", "india_net_inr": "India net",
             "gross_inr": "WW gross"}.get(str(metric), str(metric))
    if str(metric).endswith("_inr"):
        return f"{label} {_inr(r.get('observed_value'))} vs the cohort"
    money = metric != "admissions"
    return f"{label} {_num(r.get('observed_value'), money)} vs the cohort"


def _resolved_block(rows: list[dict[str, Any]], *, cutoff: date | None) -> str:
    """'Resolved this period': one line per hit/miss with the evidence."""
    head = (
        '<h3 style="font:600 15px sans-serif;margin:16px 0 6px;color:#222">Resolved '
        f"{'since ' + cutoff.isoformat() if cutoff else 'this period'}</h3>"
    )
    if not rows:
        return head + '<p style="font:13px sans-serif;color:#666">No predictions resolved in this window.</p>'
    trs = []
    for r in rows:
        kind = r.get("kind")
        if kind == "trailer_due":
            called = f"trailer within {r.get('horizon_days')}d (opens in {r.get('days_to_release_at_prediction')}d)"
        else:
            called = f"{escape(str(r.get('predicted_value') or '?'))} tier from {_num(r.get('signal_value'))} pre-release views"
        unscorable = r.get("observed_metric") == "none"
        mark = "⚪ n/a" if unscorable else ("✅ hit" if r.get("status") == "hit" else "❌ miss")
        trs.append(
            f"<tr><td {_TD}>{escape(str(r.get('resolved_at') or ''))}</td>"
            f"<td {_TD}><b>{escape(str(r.get('title') or r.get('movie_tmdb_id')))}</b><br>"
            f'<span style="color:#888">{escape(_KIND_SHORT.get(str(kind), str(kind)))} · called {escape(str(r.get("predicted_at") or ""))}</span></td>'
            f"<td {_TD}>{called}</td><td {_TD}>{_outcome_text(r)}</td><td {_TD}>{mark}</td></tr>"
        )
    return (
        head + '<table style="border-collapse:collapse;width:100%">'
        f"<tr><th {_TH}>Resolved</th><th {_TH}>Movie</th><th {_TH}>Called</th><th {_TH}>What happened</th><th {_TH}>Result</th></tr>"
        + "".join(trs) + "</table>"
    )


def _outcomes_block(rows: list[dict[str, Any]]) -> str:
    """'Trailer signal vs box office': recent releases with any observed outcome."""
    head = (
        '<h3 style="font:600 15px sans-serif;margin:16px 0 6px;color:#222">Trailer signal vs box office</h3>'
        '<p style="font:13px sans-serif;color:#666;margin:0 0 6px">Releases of the last weeks with an '
        "observed outcome (ANCINE admissions / Sacnilk India net / OMDb US gross / TMDB or Wikipedia "
        "worldwide revenue) next to the pre-release trailer views and news coverage (GDELT, 14 days "
        "before release) the opening-tier call was based on.</p>"
    )
    if not rows:
        return head + '<p style="font:13px sans-serif;color:#666">No box-office observations yet.</p>'
    trs = []
    for r in rows:
        tier = r.get("predicted_tier")
        st = r.get("prediction_status")
        call = "—" if not tier else f"{escape(str(tier))}" + (
            f" ({'✅' if st == 'hit' else '❌' if st == 'miss' else 'open'})" if st else ""
        )
        trs.append(
            f"<tr><td {_TD}><b>{escape(str(r.get('title') or ''))}</b><br>"
            f'<span style="color:#888">{escape(str(r.get("countries") or ""))} · {escape(str(r.get("release_date") or ""))}</span></td>'
            f"<td {_TD}>{_num(r.get('total_views'))}</td><td {_TD}>{_num(r.get('peak_daily_views'))}</td>"
            f"<td {_TD}>{_num(r.get('news_pre_release'))}</td>"
            f"<td {_TD}>{_num(r.get('br_admissions'))}</td><td {_TD}>{_inr(r.get('in_net_inr'))}</td>"
            f"<td {_TD}>{_num(r.get('us_gross_usd'), True)}</td>"
            f"<td {_TD}>{_num(r.get('ww_revenue_usd'), True) if r.get('ww_revenue_usd') is not None else _inr(r.get('ww_gross_inr'))}</td>"
            f"<td {_TD}>{call}</td></tr>"
        )
    return (
        head + '<table style="border-collapse:collapse;width:100%">'
        f"<tr><th {_TH}>Movie</th><th {_TH}>Pre-release views</th><th {_TH}>Peak daily</th>"
        f"<th {_TH}>News (14d)</th><th {_TH}>BR admissions</th><th {_TH}>India net</th>"
        f"<th {_TH}>US gross</th><th {_TH}>WW revenue</th><th {_TH}>Call</th></tr>"
        + "".join(trs) + "</table>"
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
    resolved: list[dict[str, Any]] | None = None,
    outcomes: list[dict[str, Any]] | None = None,
    cutoff: date | None = None,
) -> str:
    """The 'Predictions & draft posts' block — embeddable in the weekly digest.

    `resolved` (predictions.resolved_since) lists every call scored in the period
    with its evidence; `outcomes` (digest.queries.fetch_signal_vs_outcome) puts
    recent releases' trailer signal next to their box office.
    """
    parts = [
        '<h2 style="font-family:-apple-system,Helvetica,Arial,sans-serif;font-size:18px;'
        'margin:28px 0 8px 0;color:#222;">Predictions &amp; draft posts</h2>',
        '<p style="font:14px sans-serif;color:#666;margin:0 0 8px">Drafts only — review, '
        "edit, then post. Predictions are falsifiable; publish the misses too.</p>",
    ]
    if track:
        parts.append(_track_record_block(track))
    if resolved is not None:
        parts.append(_resolved_block(resolved, cutoff=cutoff))
    if outcomes is not None:
        parts.append(_outcomes_block(outcomes))
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
