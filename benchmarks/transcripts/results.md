# Transcript benchmark results

Whisper model: `base` on `auto`.

| video | duration | yta | yta sec | yta words | whisper | wh sec | wh words | jaccard |
|---|---|---|---|---|---|---|---|---|
| `bF2yS7EPD-o` (Scary Movie) | 31s | ✗ TranscriptsDisabled | 1.07 | 0 | ✓ | 1.99 | 75 | — |
| `06qdUs-JdV4` (Plants) | 66s | ✓ | 1.05 | 10 | ✓ | 0.33 | 0 | 0.0 |
| `M0pP7xKvFJ0` (500 Miles) | 110s | ✓ | 0.98 | 216 | ✓ | 3.62 | 211 | 0.606 |
| `tlSDDuWxO_0` (The Death of Robin Hood) | 147s | ✓ | 1.09 | 124 | ✓ | 1.65 | 88 | 0.485 |
| `_gQwG9Xi13g` (Pressure) | 150s | ✓ | 0.94 | 296 | ✓ | 5.59 | 295 | 0.899 |

Successes / total: yta 4/5, whisper 5/5

Inspect `out/<video_id>.{yta,whisper}.txt` for the full transcripts.