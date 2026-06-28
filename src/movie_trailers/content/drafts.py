"""Turn a Finding into ready-to-edit posts — two angles per finding.

- `fan`: film-fan voice for r/boxoffice, r/dataisbeautiful, Letterboxd, film-X.
  Hook = a falsifiable prediction or a leaderboard-style fact.
- `technical`: engineering-portfolio voice for Hacker News, LinkedIn, dev.to.
  Hook = the methodology and the pipeline that produced the call.

Deterministic templates (no LLM, no API key): they fill in the numbers and stay
honest about uncertainty. A human edits and posts — these are first drafts.
"""

from __future__ import annotations

from movie_trailers.content.findings import Finding

# Where each kind tends to land best; surfaced in the review email as a nudge.
PLATFORMS: dict[str, list[str]] = {
    "trailer_due": ["r/boxoffice", "film-X/Twitter", "LinkedIn", "Hacker News"],
    "surging": ["r/movies", "r/dataisbeautiful", "film-X/Twitter"],
    "quality_slide": ["r/boxoffice", "r/dataisbeautiful", "LinkedIn"],
    "excitement_fatigue": ["r/movies", "r/dataisbeautiful", "LinkedIn", "Hacker News"],
}

_STACK = "TMDB + YouTube Data API → BigQuery, daily Cloud Run job"


def _pct(x: float | None) -> str:
    return f"{x:.0%}" if x is not None else "n/a"


def drafts_for(finding: Finding) -> dict[str, object]:
    """Return {'fan': str, 'technical': str, 'platforms': [...]} for a finding."""
    m = finding.metrics
    t = finding.title
    vs_peak = _pct(m.get("vs_peak"))
    d2r = m.get("days_to_release")
    age = m.get("last_trailer_age_days")

    if finding.kind == "trailer_due":
        fan = (
            f"🎬 {t} hits theaters in {d2r} days — but its trailers have gone quiet. "
            f"Daily views are down to {vs_peak} of their peak and nothing new has "
            f"dropped in {age} days.\n\n"
            f"Across 119 past launches I tracked, 77% of new trailers landed in exactly "
            f"this state. So here's a falsifiable call: a fresh {t} trailer within ~2 weeks. "
            f"Screenshot this. 📊"
        )
        technical = (
            f"My trailer-tracking pipeline just flagged {t} as 'due for a trailer'.\n\n"
            f"Signal: movie-level view velocity has decayed to {vs_peak} of peak, "
            f"{d2r} days to release, no launch in {age}d. I measured this against 119 "
            f"historical launches — 77% fired from this same spent-demand state, and a "
            f"launch lifts view velocity ~2.8× (while like/view barely moves).\n\n"
            f"It's an automated, timestamped prediction. Stack: {_STACK}."
        )
    elif finding.kind == "surging":
        fan = (
            f"🚀 {t}'s trailer is taking off — daily views are still {vs_peak} of peak "
            f"{age} days after dropping. Most trailers are half-dead inside a week; "
            f"this one has legs. One to watch."
        )
        technical = (
            f"{t} is an outlier in my dataset: view velocity holding at {vs_peak} of "
            f"peak {age}d post-launch, versus a ~6-day median half-life across trailers. "
            f"Sustained velocity (not the like/view ratio) is the demand signal that "
            f"actually moves. Stack: {_STACK}."
        )
    elif finding.kind == "quality_slide":
        lv_r, lv_o = m.get("like_view_recent"), m.get("like_view_older")
        drop = (1 - lv_r / lv_o) if lv_r and lv_o else None
        fan = (
            f"📉 Audiences are cooling on {t}: its like-to-view ratio fell {_pct(drop)} "
            f"this week even as the views keep coming. Broad reach, fading enthusiasm — "
            f"a tricky place to be {d2r} days from release."
        )
        technical = (
            f"{t} shows the two engagement axes diverging: views still flowing, but "
            f"like/view down {_pct(drop)} week-over-week. The axes are near-uncorrelated "
            f"(+0.10 in my data), so 'reach' and 'enthusiasm' have to be tracked "
            f"separately — this movie is a clean example. Stack: {_STACK}."
        )
    elif finding.kind == "excitement_fatigue":
        first = m.get("first_excitement")
        last = m.get("last_excitement")
        n_tr = m.get("n_trailers")
        fan = (
            f"🥱 Trailer fatigue is real for {t}. I scored the comment sections of its "
            f"{n_tr} trailers for excitement (0–100): the buzz fell from {first:.0f} on the "
            f"first to {last:.0f} on the latest. Each new trailer is landing a little flatter."
        )
        technical = (
            f"Measured 'trailer fatigue' on {t}: comment-section excitement dropped "
            f"{first:.0f}→{last:.0f} (0–100) across its {n_tr} trailers. The score comes from "
            f"a distilled local model — an LLM teacher labeled comment sets, then a tiny "
            f"ONNX-embedding + linear head reproduces it at ~zero cost per trailer (a regex "
            f"baseline is blind to this; the decay is semantic). Stack: {_STACK}."
        )
    else:  # unknown kind — fall back to the headline
        fan = technical = finding.headline

    return {"fan": fan, "technical": technical, "platforms": PLATFORMS.get(finding.kind, [])}
