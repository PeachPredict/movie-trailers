"""Content engine: turn interesting predictions into ready-to-edit posts.

Read-only over BigQuery. `findings.py` scans the dataset for newsworthy states
(a movie due for a new trailer, a surging trailer, a cooling audience);
`drafts.py` renders each finding as both a film-fan and an engineering-portfolio
post; `render.py` assembles them into a review email you approve before posting.

Nothing here posts or contacts anyone — it drafts; a human publishes.
"""
