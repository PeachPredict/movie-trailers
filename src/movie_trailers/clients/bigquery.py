from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from google.cloud import bigquery
from pydantic import BaseModel


def _serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return _serialize(value.model_dump())
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    return value


def _rows_to_json(rows: Sequence[BaseModel]) -> list[dict[str, Any]]:
    return [_serialize(r.model_dump()) for r in rows]


class BigQueryClient:
    """Thin wrapper that MERGEs pydantic rows into a target table via staging."""

    def __init__(
        self,
        project: str,
        dataset: str,
        location: str = "us-central1",
        client: bigquery.Client | None = None,
    ):
        self.project = project
        self.dataset = dataset
        self.location = location
        self._client = client or bigquery.Client(project=project, location=location)

    def _table(self, name: str) -> str:
        return f"`{self.project}.{self.dataset}.{name}`"

    def merge_rows(
        self,
        *,
        table: str,
        rows: Sequence[BaseModel],
        merge_keys: Sequence[str],
        update_fields: Sequence[str],
        insert_fields: Sequence[str],
        partition_filter: str | None = None,
    ) -> int:
        """MERGE `rows` into `{dataset}.{table}` using `merge_keys` as the match.

        - `update_fields`: columns updated on match.
        - `insert_fields`: all columns inserted on no-match. Must include `merge_keys`.
        - `partition_filter`: optional SQL fragment ANDed into the ON clause for
          tables declared with `require_partition_filter=TRUE` (e.g.
          `"T.collected_date = DATE('2026-05-12')"`).
        Returns the number of rows staged.
        """
        if not rows:
            return 0
        staging_name = f"_staging_{table}_{uuid.uuid4().hex[:8]}"
        staging_ref = f"{self.project}.{self.dataset}.{staging_name}"
        target_ref = f"{self.project}.{self.dataset}.{table}"

        target_schema = self._client.get_table(target_ref).schema
        load_cfg = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            schema=target_schema,
        )
        ndjson = "\n".join(_dump_json_line(r) for r in _rows_to_json(rows))
        load_job = self._client.load_table_from_file(
            file_obj=_str_to_filelike(ndjson),
            destination=staging_ref,
            job_config=load_cfg,
        )
        load_job.result()

        try:
            on_parts = [f"T.{k} = S.{k}" for k in merge_keys]
            if partition_filter:
                on_parts.append(partition_filter)
            on_clause = " AND ".join(on_parts)
            update_clause = ", ".join(f"{c} = S.{c}" for c in update_fields)
            insert_cols = ", ".join(insert_fields)
            insert_vals = ", ".join(f"S.{c}" for c in insert_fields)
            sql = f"""
            MERGE `{target_ref}` T
            USING `{staging_ref}` S
            ON {on_clause}
            WHEN MATCHED THEN UPDATE SET {update_clause}
            WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})
            """
            self._client.query(sql).result()
        finally:
            self._client.delete_table(staging_ref, not_found_ok=True)
        return len(rows)

    def update_from_dicts(
        self,
        *,
        table: str,
        rows: Sequence[dict[str, Any]],
        merge_keys: Sequence[str],
        update_clause_sql: str,
    ) -> int:
        """Stage `rows` (plain dicts) and run UPDATE … FROM staging … WHERE keys match.

        `update_clause_sql` is a SET-clause body referencing `T.` and `S.`, e.g.
        "T.channel_id = COALESCE(S.channel_id, T.channel_id), T.last_collected_at = S.last_collected_at".
        Rows are inserted only into staging; the target table is updated in place.
        """
        if not rows:
            return 0
        staging_name = f"_staging_{table}_{uuid.uuid4().hex[:8]}"
        staging_ref = f"{self.project}.{self.dataset}.{staging_name}"
        target_ref = f"{self.project}.{self.dataset}.{table}"

        load_cfg = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            autodetect=True,
        )
        ndjson = "\n".join(_dump_json_line(_serialize(r)) for r in rows)
        load_job = self._client.load_table_from_file(
            file_obj=_str_to_filelike(ndjson),
            destination=staging_ref,
            job_config=load_cfg,
        )
        load_job.result()

        try:
            on_clause = " AND ".join(f"T.{k} = S.{k}" for k in merge_keys)
            sql = f"""
            UPDATE `{target_ref}` T
            SET {update_clause_sql}
            FROM `{staging_ref}` S
            WHERE {on_clause}
            """
            self._client.query(sql).result()
        finally:
            self._client.delete_table(staging_ref, not_found_ok=True)
        return len(rows)

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        job_config = None
        if params:
            qparams = [_to_query_param(k, v) for k, v in params.items()]
            job_config = bigquery.QueryJobConfig(query_parameters=qparams)
        job = self._client.query(sql, job_config=job_config)
        return [dict(row) for row in job.result()]


def _dump_json_line(d: dict[str, Any]) -> str:
    import json

    return json.dumps(d, separators=(",", ":"), ensure_ascii=False)


def _str_to_filelike(s: str):
    import io

    return io.BytesIO(s.encode("utf-8"))


def _to_query_param(name: str, value: Any) -> bigquery.ScalarQueryParameter:
    if isinstance(value, bool):
        return bigquery.ScalarQueryParameter(name, "BOOL", value)
    if isinstance(value, int):
        return bigquery.ScalarQueryParameter(name, "INT64", value)
    if isinstance(value, float):
        return bigquery.ScalarQueryParameter(name, "FLOAT64", value)
    if isinstance(value, datetime):
        return bigquery.ScalarQueryParameter(name, "TIMESTAMP", value)
    if isinstance(value, date):
        return bigquery.ScalarQueryParameter(name, "DATE", value)
    return bigquery.ScalarQueryParameter(name, "STRING", str(value))
