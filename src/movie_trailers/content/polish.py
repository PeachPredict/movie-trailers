"""Optional LLM polish for draft posts (Claude / Anthropic SDK).

Rewrites the template drafts into more natural copy while keeping every number
exactly as given — the model is told not to invent facts. Entirely optional:
with no API key, or on any API error, it falls back to the deterministic
templates so `mt suggest-content` always works offline.

Uses claude-opus-4-8 with structured outputs (messages.parse) so the two post
variants come back validated, no parsing.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

from movie_trailers.config import Settings
from movie_trailers.content.drafts import drafts_for
from movie_trailers.content.findings import Finding

DraftFn = Callable[[Finding], dict[str, object]]

_SYSTEM = (
    "You are a sharp data-journalist ghostwriting short social posts for a movie-"
    "trailer analytics project. Rewrite the two supplied drafts so they read "
    "naturally and punchily. HARD RULES: keep every number, percentage, and movie "
    "title exactly as given; never invent statistics or facts not present; keep the "
    "falsifiable prediction if one is present; no spammy hashtag walls (0-2 tags max); "
    "keep each post under ~90 words. 'fan' targets film fans (Reddit/X); 'technical' "
    "targets engineers (LinkedIn/HN) and should keep the methodology angle."
)


class _Polished(BaseModel):
    fan: str
    technical: str


def _build_client(settings: Settings):
    """Return an Anthropic client, or None if no key/SDK is available.

    Gates on a resolved key (settings or env) so we never attempt doomed calls —
    the SDK doesn't raise at construction when the key is missing.
    """
    import os

    key = settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic

        return anthropic.Anthropic(api_key=key)
    except Exception:  # noqa: BLE001 — missing SDK or bad config → fall back to templates
        return None


def _polish_one(client, model: str, finding: Finding) -> dict[str, object]:
    base = drafts_for(finding)
    user = (
        f"Movie: {finding.title}\n"
        f"Finding type: {finding.kind}\n"
        f"Metrics (use verbatim, do not alter): {finding.metrics}\n\n"
        f"Draft — fan:\n{base['fan']}\n\n"
        f"Draft — technical:\n{base['technical']}"
    )
    try:
        resp = client.messages.parse(
            model=model,
            max_tokens=1024,
            system=_SYSTEM,
            messages=[{"role": "user", "content": user}],
            output_format=_Polished,
        )
        out = resp.parsed_output
        return {"fan": out.fan, "technical": out.technical, "platforms": base["platforms"]}
    except Exception:  # noqa: BLE001 — never let a polish failure break the digest
        return base


def make_draft_fn(settings: Settings, *, polish: bool = False) -> DraftFn:
    """Return a finding→drafts function; LLM-polished when `polish` and a key exist."""
    if not polish:
        return drafts_for
    client = _build_client(settings)
    if client is None:
        return drafts_for  # no key / SDK → silent fallback
    model = settings.content_polish_model
    return lambda finding: _polish_one(client, model, finding)
