"""Read-only MCP server exposing the BigQuery trailer dataset to LLM clients.

Mirrors the `digest/` package: pure reads against BigQuery, zero writes, zero
YouTube quota. Safe to run anytime. See `server.py` for the tool surface and
`queries.py` for the SQL.
"""
