# Transcript benchmark results

Whisper model: `small` on `auto`.

| video | duration | yta | yta sec | yta words | whisper | wh sec | wh words | jaccard |
|---|---|---|---|---|---|---|---|---|
| `bF2yS7EPD-o` (Scary Movie) | 31s | ✗ TranscriptsDisabled | 2.49 | 0 | ✓ | 521.79 | 75 | — |
| `06qdUs-JdV4` (Plants) | 66s | ✓ | 1.59 | 10 | ✓ | 0.59 | 1 | 0.125 |
| `M0pP7xKvFJ0` (500 Miles) | 110s | ✓ | 1.54 | 216 | ✓ | 9.91 | 204 | 0.673 |
| `tlSDDuWxO_0` (The Death of Robin Hood) | 147s | ✓ | 1.37 | 124 | ✓ | 3.91 | 93 | 0.579 |
| `_gQwG9Xi13g` (Pressure) | 150s | ✓ | 1.26 | 296 | ✓ | 15.42 | 296 | 0.91 |

Successes / total: yta 4/5, whisper 5/5

Inspect `out/<video_id>.{yta,whisper}.txt` for the full transcripts.