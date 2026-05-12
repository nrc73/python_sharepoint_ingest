from __future__ import annotations

import logging
import uuid
from urllib.parse import quote_plus, urlencode
from typing import Any, Optional

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from src.config import SqlSettings
from src.models import IngestionConfig


def _quote_identifier(name: str) -> str:
    return "[" + name.replace("]", "]]" ) + "]"


def _parse_table_name(table_name: str) -> tuple[str, str]:
    if "." in table_name:
        schema, table = table_name.split(".", 1)
        return schema.strip(), table.strip()
    return "dbo", table_name.strip()


class SqlClient:
    def __init__(self, settings: SqlSettings, logger: Optional[logging.Logger] = None):
        self._settings = settings
        self._logger = logger or logging.getLogger(__name__)
        self._engine: Engine = self._build_engine(settings)

    @staticmethod
    def _build_engine(settings: SqlSettings) -> Engine:
        driver = settings.odbc_driver
        trust_cert = "yes" if settings.trust_server_certificate else "no"

        auth_mode = (settings.auth_mode or "sql_password").strip().lower()
        query_params = {
            "driver": driver,
            "TrustServerCertificate": trust_cert,
        }

        if auth_mode in {"ad_integrated", "integrated", "sspi", "trusted_connection", "active_directory_integrated"}:
            query_params["Trusted_Connection"] = "yes"
            query_string = urlencode(query_params)
            conn_str = (
                f"mssql+pyodbc://@{settings.host}:{settings.port}/"
                f"{settings.database}?{query_string}"
            )
            return create_engine(conn_str, fast_executemany=True, future=True)

        if not settings.username or not settings.password:
            raise ValueError(
                "SQL username/password are required for SQL password or AD password auth modes"
            )

        username = quote_plus(settings.username)
        password = quote_plus(settings.password)

        if auth_mode in {"ad_password", "active_directory_password", "aad_password"}:
            query_params["Authentication"] = "ActiveDirectoryPassword"

        query_string = urlencode(query_params)
        conn_str = (
            f"mssql+pyodbc://{username}:{password}@{settings.host}:{settings.port}/"
            f"{settings.database}?{query_string}"
        )
        return create_engine(conn_str, fast_executemany=True, future=True)

    @property
    def engine(self) -> Engine:
        return self._engine

    def test_connection(self) -> None:
        with self._engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    def execute(self, sql_text: str, params: Optional[dict[str, Any]] = None) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(sql_text), params or {})

    def query_rows(self, sql_text: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            result = conn.execute(text(sql_text), params or {})
            rows = []
            for row in result:
                row_dict = {k.lower(): v for k, v in row._mapping.items()}
                rows.append(row_dict)
            return rows

    def fetch_ingestion_configs(
        self,
        process_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
        active_only: bool = True,
    ) -> list[IngestionConfig]:
        query = "SELECT * FROM config.sharepoint_ingestion WHERE 1=1"
        params: dict[str, Any] = {}

        if process_id:
            query += " AND CAST(process_id AS NVARCHAR(100)) = :process_id"
            params["process_id"] = str(process_id)

        if workflow_id:
            query += " AND workflow_id = :workflow_id"
            params["workflow_id"] = workflow_id

        if active_only:
            query += " AND (is_active = '1' OR is_active = 1 OR is_active = 'Y' OR is_active = 'y')"

        query += " ORDER BY id"
        rows = self.query_rows(query, params)

        configs: list[IngestionConfig] = []
        for row in rows:
            configs.append(self._to_config(row))
        return configs

    @staticmethod
    def _to_config(row: dict[str, Any]) -> IngestionConfig:
        process_id = row.get("process_id")
        if process_id is not None:
            process_id = str(process_id)

        return IngestionConfig(
            id=int(row.get("id")),
            sharepoint_base_url=str(row.get("sharepoint_base_url") or ""),
            sharepoint_process_folder=str(row.get("sharepoint_process_folder") or ""),
            excel_tab_name=str(row.get("excel_tab_name") or ""),
            sharepoint_process_archive_folder=row.get("sharepoint_process_archive_folder"),
            sharepoint_process_failed_folder=row.get("sharepoint_process_failed_folder"),
            process_frequency=row.get("process_frequency"),
            header_skip_rows=int(row.get("header_skip_rows") or 0),
            check_source_dest_columns=row.get("check_source_dest_columns"),
            multi_file_ingest=row.get("multi_file_ingest"),
            error_notification_email_address=row.get("error_notification_email_address"),
            process_id=process_id,
            workflow_id=row.get("workflow_id"),
            staging_table_name=str(row.get("staging_table_name") or ""),
            is_active=row.get("is_active", "1"),
            file_name_pattern=row.get("file_name_pattern"),
            load_strategy=row.get("load_strategy"),
            merge_key_columns=row.get("merge_key_columns"),
            column_mapping_json=row.get("column_mapping_json"),
        )

    def get_table_columns(self, table_name: str) -> list[dict[str, Any]]:
        schema, table = _parse_table_name(table_name)
        sql_text = """
        SELECT
            COLUMN_NAME,
            DATA_TYPE,
            CHARACTER_MAXIMUM_LENGTH,
            NUMERIC_PRECISION,
            NUMERIC_SCALE,
            IS_NULLABLE,
            ORDINAL_POSITION
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = :schema
          AND TABLE_NAME = :table
        ORDER BY ORDINAL_POSITION
        """
        return self.query_rows(sql_text, {"schema": schema, "table": table})

    def get_primary_key_columns(self, table_name: str) -> list[str]:
        schema, table = _parse_table_name(table_name)
        sql_text = """
        SELECT KU.COLUMN_NAME
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS AS TC
        INNER JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE AS KU
            ON TC.CONSTRAINT_NAME = KU.CONSTRAINT_NAME
            AND TC.TABLE_SCHEMA = KU.TABLE_SCHEMA
            AND TC.TABLE_NAME = KU.TABLE_NAME
        WHERE TC.CONSTRAINT_TYPE = 'PRIMARY KEY'
          AND KU.TABLE_SCHEMA = :schema
          AND KU.TABLE_NAME = :table
        ORDER BY KU.ORDINAL_POSITION
        """
        rows = self.query_rows(sql_text, {"schema": schema, "table": table})
        return [str(r["column_name"]) for r in rows]

    def insert_audit_record(
        self,
        config_id: int,
        workflow_id: Optional[str],
        process_id: Optional[str],
        file_name: Optional[str],
        status: str,
        records_loaded: Optional[int],
        message: Optional[str],
    ) -> None:
        sql_text = """
        IF OBJECT_ID('log.sharepoint_ingestion_audit', 'U') IS NOT NULL
        BEGIN
            INSERT INTO log.sharepoint_ingestion_audit (
                config_id,
                workflow_id,
                process_id,
                file_name,
                status,
                records_loaded,
                message
            )
            VALUES (
                :config_id,
                :workflow_id,
                TRY_CONVERT(uniqueidentifier, :process_id),
                :file_name,
                :status,
                :records_loaded,
                :message
            );
        END
        """
        self.execute(
            sql_text,
            {
                "config_id": config_id,
                "workflow_id": workflow_id,
                "process_id": process_id,
                "file_name": file_name,
                "status": status,
                "records_loaded": records_loaded,
                "message": message,
            },
        )

    def truncate_and_load(self, df: pd.DataFrame, table_name: str) -> None:
        schema, table = _parse_table_name(table_name)
        qualified_table = f"{_quote_identifier(schema)}.{_quote_identifier(table)}"

        with self._engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE {qualified_table}"))

        self.append_load(df, table_name)

    def append_load(self, df: pd.DataFrame, table_name: str) -> None:
        if df.empty:
            return

        schema, table = _parse_table_name(table_name)

        # SQL Server / pyodbc caps at 2100 parameters per statement.
        # Use 2099 (not 2100) so that num_cols * chunksize is strictly < 2100.
        num_cols = max(len(df.columns), 1)
        safe_chunksize = max(1, min(300, 2099 // num_cols))

        df.to_sql(
            name=table,
            schema=schema,
            con=self._engine,
            if_exists="append",
            index=False,
            chunksize=safe_chunksize,
            method="multi",
        )

    def merge_load(self, df: pd.DataFrame, table_name: str, merge_keys: list[str]) -> None:
        if not merge_keys:
            raise ValueError("merge_keys must be supplied for merge strategy")

        schema, table = _parse_table_name(table_name)
        temp_table = f"_tmp_{table}_{uuid.uuid4().hex[:8]}"

        df.to_sql(
            name=temp_table,
            schema=schema,
            con=self._engine,
            if_exists="replace",
            index=False,
            chunksize=1000,
            method="multi",
        )

        try:
            merge_sql = self._build_merge_sql(
                schema=schema,
                target_table=table,
                source_table=temp_table,
                source_columns=list(df.columns),
                merge_keys=merge_keys,
            )
            with self._engine.begin() as conn:
                conn.execute(text(merge_sql))
        finally:
            with self._engine.begin() as conn:
                conn.execute(text(f"DROP TABLE IF EXISTS {_quote_identifier(schema)}.{_quote_identifier(temp_table)}"))

    @staticmethod
    def _build_merge_sql(
        schema: str,
        target_table: str,
        source_table: str,
        source_columns: list[str],
        merge_keys: list[str],
    ) -> str:
        target_qualified = f"{_quote_identifier(schema)}.{_quote_identifier(target_table)}"
        source_qualified = f"{_quote_identifier(schema)}.{_quote_identifier(source_table)}"

        normalized_keys = [k.strip() for k in merge_keys if k and k.strip()]
        missing_keys = [k for k in normalized_keys if k not in source_columns]
        if missing_keys:
            raise ValueError(f"Merge keys not present in source data: {missing_keys}")

        on_clause = " AND ".join([f"target.{_quote_identifier(k)} = source.{_quote_identifier(k)}" for k in normalized_keys])

        update_columns = [c for c in source_columns if c not in normalized_keys]
        if update_columns:
            update_clause = ",\n    ".join(
                [f"target.{_quote_identifier(c)} = source.{_quote_identifier(c)}" for c in update_columns]
            )
            matched_clause = f"WHEN MATCHED THEN\n    UPDATE SET\n    {update_clause}\n"
        else:
            matched_clause = ""

        insert_columns = ", ".join([_quote_identifier(c) for c in source_columns])
        insert_values = ", ".join([f"source.{_quote_identifier(c)}" for c in source_columns])

        return f"""
MERGE {target_qualified} AS target
USING {source_qualified} AS source
ON {on_clause}
{matched_clause}WHEN NOT MATCHED BY TARGET THEN
    INSERT ({insert_columns})
    VALUES ({insert_values});
"""
