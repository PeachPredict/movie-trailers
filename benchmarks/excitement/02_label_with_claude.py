"""Teacher labels: score each trailer's comment-section excitement 0–100 with Claude.

ORDER-BLINDED (load-bearing for validity): a batch mixes random trailers from
different movies behind opaque labels — Claude never sees the trailer's ordinal
position, title, date, or movie, so it scores excitement-from-text, never
"this is the 4th trailer so it should be lower". Batching ~12 trailers/call keeps
cost/latency low while the rubric stays consistent.

Run (anthropic is already a project dep):
    cd benchmarks/excitement
    uv run python 02_label_with_claude.py
Input: training_set.jsonl   Output: labels.jsonl  ({youtube_video_id, excitement, rationale})

Note: this uses concurrent synchronous calls for an immediate, reliable result.
For a cheaper scheduled refresh, the Message Batches API (~50% off) is the
production path — see README.md.
"""
from __future__ import annotations

import json
import os
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic
from pydantic import BaseModel

from movie_trailers.config import load_settings

HERE = Path(__file__).parent
IN = HERE / "training_set.jsonl"
OUT = HERE / "labels.jsonl"
MODEL = "claude-opus-4-8"
BATCH = 12          # trailers per call
MAX_WORKERS = 8
TOP_N = 20          # comments shown per trailer
SEED = 42

SYSTEM = (
    "You rate how EXCITED and hyped a YouTube trailer comment section is about the "
    "upcoming film. 0 = hostile, bored, mocking, or fatigued; 50 = mixed/neutral; "
    "100 = euphoric, can't-wait hype. Judge genuine anticipation, not just "
    "punctuation. Sarcasm and 'trailer fatigue' lower the score. You are shown "
    "several UNRELATED trailers' comment sets behind opaque labels; score each on "
    "its own merits using the same rubric. Return one score per label."
)


class _Score(BaseModel):
    label: str
    excitement: int
    rationale: str


class _Out(BaseModel):
    scores: list[_Score]


def _client() -> anthropic.Anthropic:
    s = load_settings()
    key = s.anthropic_api_key or os.environ["ANTHROPIC_API_KEY"]
    return anthropic.Anthropic(api_key=key)


def _score_batch(client: anthropic.Anthropic, batch: list[dict]) -> dict[str, _Score]:
    parts = []
    for i, tr in enumerate(batch):
        comments = sorted(tr["comments"], key=lambda c: (c.get("rank") or 1_000_000))[:TOP_N]
        body = "\n".join(f"- {(c['text'] or '').replace(chr(10), ' ')[:220]}" for c in comments)
        parts.append(f"### L{i}\n{body or '(no comments)'}")
    prompt = "Rate each labeled comment set's excitement.\n\n" + "\n\n".join(parts)
    resp = client.messages.parse(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_format=_Out,
    )
    by_label = {sc.label: sc for sc in resp.parsed_output.scores}
    return {
        tr["youtube_video_id"]: by_label[f"L{i}"]
        for i, tr in enumerate(batch)
        if f"L{i}" in by_label
    }


def main() -> None:
    rng = random.Random(SEED)
    trailers = [json.loads(line) for line in IN.read_text().splitlines() if line.strip()]
    rng.shuffle(trailers)  # mix movies across batches → no within-movie sequence in a call
    batches = [trailers[i : i + BATCH] for i in range(0, len(trailers), BATCH)]
    client = _client()

    results: dict[str, _Score] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(_score_batch, client, b): bi for bi, b in enumerate(batches)}
        for done, fut in enumerate(as_completed(futs), 1):
            try:
                results.update(fut.result())
            except Exception as e:  # noqa: BLE001 — log and continue; partial labels are fine
                print(f"  batch {futs[fut]} failed: {str(e)[:160]}")
            if done % 10 == 0:
                print(f"  {done}/{len(batches)} batches, {len(results)} labels")

    with OUT.open("w") as f:
        for vid, sc in results.items():
            f.write(
                json.dumps(
                    {"youtube_video_id": vid, "excitement": sc.excitement, "rationale": sc.rationale},
                    ensure_ascii=False,
                )
                + "\n"
            )
    print(f"wrote {len(results)} labels → {OUT}")


if __name__ == "__main__":
    main()
