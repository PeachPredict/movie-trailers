"""Benchmark music-identification services on a fixed set of trailers.

For each trailer:
  1. download audio via yt-dlp (cached)
  2. slice into 15-second non-overlapping windows via ffmpeg (cached)
  3. query each available method per window
  4. aggregate per-method hit rate and unique-tracks-found

Methods tested (each skipped silently if its dependency / key is missing):
  - shazamio    : unofficial Shazam client, no key needed
  - audd        : api.audd.io/recognize, AUDD_API_TOKEN env (defaults to "test")
  - acoustid    : AcoustID + Chromaprint, ACOUSTID_API_KEY env (requires fpcalc)

Run:
    cd benchmarks/music_id
    uv run --with yt-dlp --with shazamio --with pyacoustid --with httpx python run_benchmark.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
AUDIO_DIR = HERE / "audio"
WINDOWS_DIR = HERE / "windows"
TRAILERS_JSON = HERE / "trailers.json"
RESULTS_JSON = HERE / "results.json"
RESULTS_MD = HERE / "results.md"

WINDOW_SECONDS = 15
ACOUSTID_API_KEY = os.environ.get("ACOUSTID_API_KEY")
AUDD_API_TOKEN = os.environ.get("AUDD_API_TOKEN", "test")


@dataclass
class WindowMatch:
    window_index: int
    start_seconds: int
    method: str
    success: bool = False
    title: str | None = None
    artist: str | None = None
    score: float | None = None
    latency_seconds: float = 0.0
    error: str | None = None
    raw: dict | None = None  # trimmed raw response sample for inspection


@dataclass
class TrailerResult:
    youtube_video_id: str
    movie_title: str
    original_language: str | None
    duration_seconds: int
    view_count: int
    window_count: int = 0
    matches: list[WindowMatch] = field(default_factory=list)


# --- Audio prep --------------------------------------------------------------

def download_audio(video_id: str) -> Path | None:
    existing = list(AUDIO_DIR.glob(f"{video_id}.*"))
    if existing:
        return existing[0]
    AUDIO_DIR.mkdir(exist_ok=True)
    if shutil.which("yt-dlp") is None:
        raise RuntimeError("yt-dlp not on PATH (brew install yt-dlp)")
    cmd = [
        "yt-dlp",
        "-f", "bestaudio/best",
        "-x", "--audio-format", "m4a",
        "-o", str(AUDIO_DIR / "%(id)s.%(ext)s"),
        "--no-warnings",
        "--quiet",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    subprocess.run(cmd, check=True, timeout=180)
    matches = list(AUDIO_DIR.glob(f"{video_id}.*"))
    return matches[0] if matches else None


def slice_windows(video_id: str, audio_path: Path, duration_seconds: int) -> list[tuple[int, int, Path]]:
    """Return [(window_index, start_seconds, path), ...] for non-overlapping 15s windows."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not on PATH (brew install ffmpeg)")
    out_dir = WINDOWS_DIR / video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    windows: list[tuple[int, int, Path]] = []
    idx = 0
    start = 0
    # Leave a 1s tail margin so the final window isn't a stub
    while start + 5 < duration_seconds:
        win_path = out_dir / f"w{idx:02d}.m4a"
        if not win_path.exists():
            cmd = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", str(start),
                "-t", str(WINDOW_SECONDS),
                "-i", str(audio_path),
                "-vn", "-acodec", "copy",
                str(win_path),
            ]
            subprocess.run(cmd, check=True, timeout=30)
        windows.append((idx, start, win_path))
        idx += 1
        start += WINDOW_SECONDS
    return windows


# --- Method A: shazamio ------------------------------------------------------

def has_shazamio() -> bool:
    try:
        import shazamio  # noqa: F401
        return True
    except ImportError:
        return False


async def _shazam_one(shazam, path: Path) -> dict[str, Any]:
    # shazamio API changed over versions: try `recognize` then fall back.
    if hasattr(shazam, "recognize"):
        return await shazam.recognize(str(path))
    return await shazam.recognize_song(str(path))  # legacy


async def _shazam_all(jobs: list[tuple[int, int, Path]]) -> list[WindowMatch]:
    from shazamio import Shazam
    shazam = Shazam()
    results: list[WindowMatch] = []
    for idx, start, path in jobs:
        m = WindowMatch(window_index=idx, start_seconds=start, method="shazamio")
        t0 = time.perf_counter()
        try:
            data = await _shazam_one(shazam, path)
            track = (data or {}).get("track") or {}
            if track:
                m.success = True
                m.title = track.get("title")
                m.artist = track.get("subtitle")
                m.raw = {k: track.get(k) for k in ("title", "subtitle", "key", "url") if track.get(k)}
        except Exception as exc:  # noqa: BLE001
            m.error = f"{type(exc).__name__}: {exc}"
        m.latency_seconds = round(time.perf_counter() - t0, 3)
        results.append(m)
    return results


# --- Method B: AudD ----------------------------------------------------------

def has_audd() -> bool:
    try:
        import httpx  # noqa: F401
        return True
    except ImportError:
        return False


def run_audd(jobs: list[tuple[int, int, Path]]) -> list[WindowMatch]:
    import httpx
    out: list[WindowMatch] = []
    with httpx.Client(timeout=30.0) as client:
        for idx, start, path in jobs:
            m = WindowMatch(window_index=idx, start_seconds=start, method="audd")
            t0 = time.perf_counter()
            try:
                with open(path, "rb") as fh:
                    resp = client.post(
                        "https://api.audd.io/recognize",
                        data={"api_token": AUDD_API_TOKEN, "return": "timecode"},
                        files={"file": (path.name, fh, "audio/mp4")},
                    )
                resp.raise_for_status()
                body = resp.json()
                if body.get("status") == "success" and body.get("result"):
                    r = body["result"]
                    m.success = True
                    m.title = r.get("title")
                    m.artist = r.get("artist")
                    m.raw = {
                        "title": r.get("title"),
                        "artist": r.get("artist"),
                        "album": r.get("album"),
                        "release_date": r.get("release_date"),
                    }
                elif body.get("status") != "success":
                    m.error = str(body.get("error") or body)[:200]
            except Exception as exc:  # noqa: BLE001
                m.error = f"{type(exc).__name__}: {exc}"
            m.latency_seconds = round(time.perf_counter() - t0, 3)
            out.append(m)
    return out


# --- Method C: AcoustID ------------------------------------------------------

def has_acoustid() -> bool:
    if not ACOUSTID_API_KEY:
        return False
    if shutil.which("fpcalc") is None:
        return False
    try:
        import acoustid  # noqa: F401
        return True
    except ImportError:
        return False


def run_acoustid(jobs: list[tuple[int, int, Path]]) -> list[WindowMatch]:
    import acoustid
    out: list[WindowMatch] = []
    for idx, start, path in jobs:
        m = WindowMatch(window_index=idx, start_seconds=start, method="acoustid")
        t0 = time.perf_counter()
        try:
            # match() yields (score, recording_id, title, artist)
            best = None
            for hit in acoustid.match(ACOUSTID_API_KEY, str(path)):
                best = hit
                break  # already sorted by score desc
            if best is not None:
                score, _rid, title, artist = best
                m.success = True
                m.title = title
                m.artist = artist
                m.score = round(float(score), 3)
                m.raw = {"title": title, "artist": artist, "score": m.score}
        except acoustid.NoBackendError:
            m.error = "fpcalc not found"
        except acoustid.FingerprintGenerationError as exc:
            m.error = f"fp_gen: {exc}"
        except acoustid.WebServiceError as exc:
            m.error = f"web: {exc}"
        except Exception as exc:  # noqa: BLE001
            m.error = f"{type(exc).__name__}: {exc}"
        m.latency_seconds = round(time.perf_counter() - t0, 3)
        out.append(m)
    return out


# --- Orchestrator ------------------------------------------------------------

def main() -> int:
    trailers = json.loads(TRAILERS_JSON.read_text())
    if not trailers:
        print("trailers.json is empty. Run `pick_trailers.py` or hand-edit it.")
        return 1

    enabled = {
        "shazamio": has_shazamio(),
        "audd": has_audd(),
        "acoustid": has_acoustid(),
    }
    print(f"Methods enabled: {[m for m, ok in enabled.items() if ok]}")
    if not any(enabled.values()):
        print("No methods enabled. Install at least one (shazamio, httpx, pyacoustid).")
        return 1

    results: list[TrailerResult] = []
    for i, t in enumerate(trailers, 1):
        vid = t["youtube_video_id"]
        print(
            f"\n[{i}/{len(trailers)}] {vid} {t['movie_title']!r} "
            f"({t.get('original_language','?')}, {t['duration_seconds']}s)"
        )
        r = TrailerResult(
            youtube_video_id=vid,
            movie_title=t["movie_title"],
            original_language=t.get("original_language"),
            duration_seconds=int(t["duration_seconds"]),
            view_count=int(t.get("view_count", 0)),
        )

        try:
            audio_path = download_audio(vid)
        except Exception as exc:  # noqa: BLE001
            print(f"  audio_download_failed: {exc}")
            results.append(r)
            continue
        if audio_path is None:
            print("  audio_download_failed (no file produced)")
            results.append(r)
            continue

        jobs = slice_windows(vid, audio_path, r.duration_seconds)
        r.window_count = len(jobs)
        print(f"  {len(jobs)} windows of {WINDOW_SECONDS}s")

        if enabled["shazamio"]:
            shz = asyncio.run(_shazam_all(jobs))
            r.matches.extend(shz)
            hits = sum(1 for m in shz if m.success)
            print(f"  shazamio: {hits}/{len(shz)} hits")

        if enabled["audd"]:
            ad = run_audd(jobs)
            r.matches.extend(ad)
            hits = sum(1 for m in ad if m.success)
            print(f"  audd    : {hits}/{len(ad)} hits  (token={'set' if AUDD_API_TOKEN != 'test' else 'demo'})")

        if enabled["acoustid"]:
            ac = run_acoustid(jobs)
            r.matches.extend(ac)
            hits = sum(1 for m in ac if m.success)
            print(f"  acoustid: {hits}/{len(ac)} hits")

        results.append(r)

    RESULTS_JSON.write_text(json.dumps([asdict(r) for r in results], indent=2))
    write_markdown_report(results, enabled)
    print(f"\nWrote {RESULTS_JSON.name} and {RESULTS_MD.name}")
    return 0


def write_markdown_report(results: list[TrailerResult], enabled: dict[str, bool]) -> None:
    methods = [m for m, ok in enabled.items() if ok]
    lines: list[str] = []
    lines.append("# Music-ID benchmark results\n")
    lines.append(f"Window: {WINDOW_SECONDS}s. Methods: {', '.join(methods) or 'none'}.\n")

    lines.append("## Per-trailer hit counts\n")
    header = "| video | title | lang | dur | win | " + " | ".join(f"{m} hits" for m in methods) + " | " + " | ".join(f"{m} tracks" for m in methods) + " |"
    lines.append(header)
    lines.append("|" + "|".join(["---"] * (5 + 2 * len(methods))) + "|")
    for r in results:
        by_method: dict[str, list[WindowMatch]] = {m: [] for m in methods}
        for w in r.matches:
            if w.method in by_method:
                by_method[w.method].append(w)
        hits = []
        unique_tracks = []
        for m in methods:
            ws = by_method[m]
            hits.append(f"{sum(1 for w in ws if w.success)}/{len(ws)}")
            tracks = {(w.title, w.artist) for w in ws if w.success and w.title}
            unique_tracks.append(str(len(tracks)))
        lines.append(
            f"| `{r.youtube_video_id}` | {r.movie_title} | {r.original_language or '?'} | "
            f"{r.duration_seconds}s | {r.window_count} | "
            + " | ".join(hits) + " | " + " | ".join(unique_tracks) + " |"
        )

    lines.append("\n## Method totals\n")
    lines.append("| method | windows tested | windows matched | hit rate | trailers w/ ≥1 hit | mean latency |")
    lines.append("|---|---|---|---|---|---|")
    for m in methods:
        all_w = [w for r in results for w in r.matches if w.method == m]
        hits_w = [w for w in all_w if w.success]
        trailers_with_hit = sum(
            1 for r in results if any(w.method == m and w.success for w in r.matches)
        )
        hit_rate = (len(hits_w) / len(all_w) * 100) if all_w else 0.0
        mean_lat = sum(w.latency_seconds for w in all_w) / len(all_w) if all_w else 0.0
        lines.append(
            f"| {m} | {len(all_w)} | {len(hits_w)} | {hit_rate:.1f}% | "
            f"{trailers_with_hit}/{len(results)} | {mean_lat:.2f}s |"
        )

    lines.append("\n## Identified tracks (sample)\n")
    for r in results:
        hits = [w for w in r.matches if w.success]
        if not hits:
            continue
        lines.append(f"\n### {r.movie_title} (`{r.youtube_video_id}`)")
        seen: set[tuple[str, str, str]] = set()
        for w in hits:
            key = (w.method, w.title or "", w.artist or "")
            if key in seen:
                continue
            seen.add(key)
            score = f" score={w.score}" if w.score is not None else ""
            lines.append(f"- **{w.method}** @ t={w.start_seconds}s — {w.title!r} by {w.artist!r}{score}")

    lines.append("")
    RESULTS_MD.write_text("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
