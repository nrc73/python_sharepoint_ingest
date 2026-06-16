"""SQL access layer for configuration retrieval and data loading."""

from __future__ import annotations

import logging
import uuid
from urllib.parse import quote_plus, urlencode
from typing import Any, Optional

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError as SqlIntegrityError
from sqlalchemy.sql import sqltypes as satypes

from sharepoint_ingest.config import SqlSettings
from sharepoint_ingest.models import IngestionConfig
from sharepoint_ingest.sql._identifiers import (
    DEFAULT_DESTINATION_SCHEMA,  # re-exported for any external callers
    parse_table_name as _parse_table_name,
    quote_identifier as _quote_identifier,
)


INTEGRATED_AUTH_MODES = {
    "windows",
    "integrated",
    "sspi",
    "trusted_connection",
    "ad_integrated",
    "active_directory_integrated",
}

PASSWORDLESS_TOKEN_AUTH_MODES = {
    "managed_identity",
}

PASSWORD_AUTH_MODES = {
    "sql_password",
    "ad_password",
    "active_directory_password",
    "aad_password",
}

SUPPORTED_AUTH_MODES = (
    INTEGRATED_AUTH_MODES | PASSWORDLESS_TOKEN_AUTH_MODES | PASSWORD_AUTH_MODES
)



def normalize_sql_auth_mode(auth_mode: Optional[str]) -> str:
    return (auth_mode or "sql_password").strip().lower()


def is_integrated_auth_mode(auth_mode: Optional[str]) -> bool:
    return normalize_sql_auth_mode(auth_mode) in INTEGRATED_AUTH_MODES


def _format_server_endpoint(host: str, port: int, *, integrated_auth: bool) -> str:
    normalized_host = (host or "").strip()
    if not normalized_host:
        normalized_host = "."

    # For local/default-instance integrated auth on Windows, omitting the TCP port
    # lets SQL Native Client/ODBC use the native endpoint resolution (shared
    # memory / named pipes), which is more reliable than forcing TCP :1433.
    local_markers = {".", "(local)", "localhost"}
    if integrated_auth and normalized_host.lower() in local_markers:
        return normalized_host

    if "\\" in normalized_host:
        # Named instance already encoded in host (e.g., MACHINE\SQLEXPRESS)
        return normalized_host

    return f"{normalized_host},{port}"




class SqlClient:
    def __init__(self, settings: SqlSettings, logger: Optional[logging.Logger] = None):
        self._settings = settings
        self._logger = logger or logging.getLogger(__name__)
        self._engine: Engine = self._build_engine(settings)

    @staticmethod
    def _build_engine(settings: SqlSettings) -> Engine:
        driver = settings.odbc_driver
        trust_cert = "yes" if settings.trust_server_certificate else "no"

        auth_mode = normalize_sql_auth_mode(settings.auth_mode)
        if auth_mode not in SUPPORTED_AUTH_MODES:
            supported = ", ".join(sorted(SUPPORTED_AUTH_MODES))
            raise ValueError(
                f"Unsupported SQL auth mode '{settings.auth_mode}'. Supported values: {supported}"
            )

        query_params = {
            "driver": driver,
            "TrustServerCertificate": trust_cert,
        }

        server_endpoint = _format_server_endpoint(
            settings.host,
            settings.port,
            integrated_auth=is_integrated_auth_mode(auth_mode),
        )

        if is_integrated_auth_mode(auth_mode):
            query_params["Trusted_Connection"] = "yes"
            query_string = urlencode(query_params)
            conn_str = (
                f"mssql+pyodbc://@{server_endpoint}/"
                f"{settings.database}?{query_string}"
            )
            return create_engine(conn_str, fast_executemany=True, future=True)

        if auth_mode in PASSWORDLESS_TOKEN_AUTH_MODES:
            query_params["Authentication"] = "ActiveDirectoryMsi"
            query_string = urlencode(query_params)
            conn_str = (
                f"mssql+pyodbc://@{server_endpoint}/"
                f"{settings.database}?{query_string}"
            )
            return create_engine(conn_str, fast_executemany=True, future=True)

        if not settings.username or not settings.password:
            raise ValueError(
                "SQL username/password are required for credential-based auth modes"
            )

        username = quote_plus(settings.username)
        password = quote_plus(settings.password)

        if auth_mode in {"ad_password", "active_directory_password", "aad_password"}:
            query_params["Authentication"] = "ActiveDirectoryPassword"

        query_string = urlencode(query_params)
        conn_str = (
            f"mssql+pyodbc://{username}:{password}@{server_endpoint}/"
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
        ingestion_scope: Optional[str] = "real",
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

        normalized_scope = (ingestion_scope or "all").strip().upper()
        configs: list[IngestionConfig] = []
        for row in rows:
            config = IngestionConfig.from_sql_row(row)

            if normalized_scope != "ALL":
                config_scope = (config.ingestion_scope or "").strip().upper()
                if not config_scope:
                    config_scope = "TEST" if config.test_data_enabled else "REAL"
                if config_scope != normalized_scope:
                    continue

            configs.append(config)
        return configs



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

    def get_primary_key_columns_in_database(self, database_name: str, table_name: str) -> list[str]:
        """Return PK columns for a table in another database on the same SQL Server."""
        schema, table = _parse_table_name(table_name)
        db_q = _quote_identifier(database_name)
        sql_text = f"""
        SELECT KU.COLUMN_NAME
        FROM {db_q}.INFORMATION_SCHEMA.TABLE_CONSTRAINTS AS TC
        INNER JOIN {db_q}.INFORMATION_SCHEMA.KEY_COLUMN_USAGE AS KU
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

    def count_duplicate_keys(self, table_name: str, key_columns: list[str]) -> int:
        """Count duplicate key groups currently present in a table."""
        normalized_keys = [k.strip() for k in key_columns if k and k.strip()]
        if not normalized_keys:
            return 0

        schema, table = _parse_table_name(table_name)
        qualified = f"{_quote_identifier(schema)}.{_quote_identifier(table)}"
        key_expr = ", ".join(_quote_identifier(k) for k in normalized_keys)
        sql_text = f"""
        SELECT COUNT(*) AS n
        FROM (
            SELECT {key_expr}
            FROM {qualified}
            GROUP BY {key_expr}
            HAVING COUNT(*) > 1
        ) AS dupes
        """
        rows = self.query_rows(sql_text)
        return int(rows[0]["n"]) if rows else 0

    def count_key_conflicts_with_int(
        self,
        stg_table_name: str,
        int_database: str,
        int_table_name: str,
        key_columns: list[str],
    ) -> int:
        """Count distinct staging keys that already exist in the integrated table."""
        normalized_keys = [k.strip() for k in key_columns if k and k.strip()]
        if not normalized_keys:
            return 0

        stg_schema, stg_table = _parse_table_name(stg_table_name)
        int_schema, int_table = _parse_table_name(int_table_name)
        stg_q = f"{_quote_identifier(stg_schema)}.{_quote_identifier(stg_table)}"
        int_q = (
            f"{_quote_identifier(int_database)}"
            f".{_quote_identifier(int_schema)}"
            f".{_quote_identifier(int_table)}"
        )
        join_predicate = " AND ".join(
            f"stg.{_quote_identifier(k)} = integ.{_quote_identifier(k)}"
            for k in normalized_keys
        )
        key_expr = ", ".join(f"stg.{_quote_identifier(k)}" for k in normalized_keys)
        sql_text = f"""
        SELECT COUNT(*) AS n
        FROM (
            SELECT DISTINCT {key_expr}
            FROM {stg_q} AS stg
            INNER JOIN {int_q} AS integ
                ON {join_predicate}
        ) AS conflicts
        """
        rows = self.query_rows(sql_text)
        return int(rows[0]["n"]) if rows else 0

    def insert_audit_record(
        self,
        config_id: int,
        workflow_id: Optional[str],
        process_id: Optional[str],
        file_name: Optional[str],
        status: str,
        records_loaded: Optional[int],
        message: Optional[str],
        rows_scanned: Optional[int] = None,
        validation_error_count: Optional[int] = None,
        memory_peak_mb: Optional[float] = None,
        duration_seconds: Optional[float] = None,
        ingestion_scope: Optional[str] = None,
        is_test_data: Optional[bool] = None,
        destination_database: Optional[str] = None,
        destination_table: Optional[str] = None,
    ) -> Optional[int]:
        sql_text = """
        SET NOCOUNT ON;
        DECLARE @new_audit TABLE (audit_id BIGINT);

        IF OBJECT_ID('log.sharepoint_ingestion_audit', 'U') IS NOT NULL
        BEGIN
            IF COL_LENGTH('log.sharepoint_ingestion_audit', 'destination_database') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'destination_table') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'ingestion_scope') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'is_test_data') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'rows_scanned') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'validation_error_count') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'memory_peak_mb') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'duration_seconds') IS NOT NULL
            BEGIN
                INSERT INTO log.sharepoint_ingestion_audit (
                    config_id,
                    workflow_id,
                    process_id,
                    file_name,
                    status,
                    records_loaded,
                    rows_scanned,
                    validation_error_count,
                    memory_peak_mb,
                    duration_seconds,
                    ingestion_scope,
                    is_test_data,
                    destination_database,
                    destination_table,
                    message
                )
                OUTPUT CAST(inserted.audit_id AS BIGINT) INTO @new_audit(audit_id)
                VALUES (
                    :config_id,
                    :workflow_id,
                    TRY_CONVERT(uniqueidentifier, :process_id),
                    :file_name,
                    :status,
                    :records_loaded,
                    COALESCE(:rows_scanned, 0),
                    :validation_error_count,
                    :memory_peak_mb,
                    :duration_seconds,
                    :ingestion_scope,
                    :is_test_data,
                    :destination_database,
                    :destination_table,
                    :message
                );
            END
            ELSE IF COL_LENGTH('log.sharepoint_ingestion_audit', 'ingestion_scope') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'is_test_data') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'rows_scanned') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'validation_error_count') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'memory_peak_mb') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'duration_seconds') IS NOT NULL
            BEGIN
                INSERT INTO log.sharepoint_ingestion_audit (
                    config_id,
                    workflow_id,
                    process_id,
                    file_name,
                    status,
                    records_loaded,
                    rows_scanned,
                    validation_error_count,
                    memory_peak_mb,
                    duration_seconds,
                    ingestion_scope,
                    is_test_data,
                    message
                )
                OUTPUT CAST(inserted.audit_id AS BIGINT) INTO @new_audit(audit_id)
                VALUES (
                    :config_id,
                    :workflow_id,
                    TRY_CONVERT(uniqueidentifier, :process_id),
                    :file_name,
                    :status,
                    :records_loaded,
                    COALESCE(:rows_scanned, 0),
                    :validation_error_count,
                    :memory_peak_mb,
                    :duration_seconds,
                    :ingestion_scope,
                    :is_test_data,
                    :message
                );
            END
            ELSE IF COL_LENGTH('log.sharepoint_ingestion_audit', 'rows_scanned') IS NOT NULL
                AND COL_LENGTH('log.sharepoint_ingestion_audit', 'validation_error_count') IS NOT NULL
                AND COL_LENGTH('log.sharepoint_ingestion_audit', 'memory_peak_mb') IS NOT NULL
                AND COL_LENGTH('log.sharepoint_ingestion_audit', 'duration_seconds') IS NOT NULL
            BEGIN
                INSERT INTO log.sharepoint_ingestion_audit (
                    config_id,
                    workflow_id,
                    process_id,
                    file_name,
                    status,
                    records_loaded,
                    rows_scanned,
                    validation_error_count,
                    memory_peak_mb,
                    duration_seconds,
                    message
                )
                OUTPUT CAST(inserted.audit_id AS BIGINT) INTO @new_audit(audit_id)
                VALUES (
                    :config_id,
                    :workflow_id,
                    TRY_CONVERT(uniqueidentifier, :process_id),
                    :file_name,
                    :status,
                    :records_loaded,
                    COALESCE(:rows_scanned, 0),
                    :validation_error_count,
                    :memory_peak_mb,
                    :duration_seconds,
                    :message
                );
            END
            ELSE
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
                OUTPUT CAST(inserted.audit_id AS BIGINT) INTO @new_audit(audit_id)
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
        END

        SELECT TOP 1 audit_id FROM @new_audit;
        """
        params = {
            "config_id": config_id,
            "workflow_id": workflow_id,
            "process_id": process_id,
            "file_name": file_name,
            "status": status,
            "records_loaded": records_loaded,
            "message": message,
            "rows_scanned": rows_scanned,
            "validation_error_count": validation_error_count,
            "memory_peak_mb": memory_peak_mb,
            "duration_seconds": duration_seconds,
            "ingestion_scope": ingestion_scope,
            "is_test_data": 1 if is_test_data else 0 if is_test_data is not None else None,
            "destination_database": destination_database,
            "destination_table": destination_table,
        }

        with self._engine.begin() as conn:
            result = conn.execute(text(sql_text), params)
            row = result.mappings().first()

        if not row:
            return None

        audit_id = row.get("audit_id")
        return int(audit_id) if audit_id is not None else None

    def update_audit_record(
        self,
        audit_id: int,
        status: str,
        records_loaded: Optional[int],
        message: Optional[str],
        rows_scanned: Optional[int] = None,
        validation_error_count: Optional[int] = None,
        memory_peak_mb: Optional[float] = None,
        duration_seconds: Optional[float] = None,
        ingestion_scope: Optional[str] = None,
        is_test_data: Optional[bool] = None,
        destination_database: Optional[str] = None,
        destination_table: Optional[str] = None,
    ) -> bool:
        sql_text = """
        IF OBJECT_ID('log.sharepoint_ingestion_audit', 'U') IS NOT NULL
        BEGIN
            IF COL_LENGTH('log.sharepoint_ingestion_audit', 'destination_database') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'destination_table') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'ingestion_scope') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'is_test_data') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'rows_scanned') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'validation_error_count') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'memory_peak_mb') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'duration_seconds') IS NOT NULL
            BEGIN
                UPDATE log.sharepoint_ingestion_audit
                SET
                    status = :status,
                    records_loaded = :records_loaded,
                    rows_scanned = COALESCE(:rows_scanned, 0),
                    validation_error_count = :validation_error_count,
                    memory_peak_mb = :memory_peak_mb,
                    duration_seconds = :duration_seconds,
                    ingestion_scope = :ingestion_scope,
                    is_test_data = :is_test_data,
                    destination_database = :destination_database,
                    destination_table = :destination_table,
                    message = :message
                WHERE audit_id = :audit_id;
            END
            ELSE IF COL_LENGTH('log.sharepoint_ingestion_audit', 'ingestion_scope') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'is_test_data') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'rows_scanned') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'validation_error_count') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'memory_peak_mb') IS NOT NULL
               AND COL_LENGTH('log.sharepoint_ingestion_audit', 'duration_seconds') IS NOT NULL
            BEGIN
                UPDATE log.sharepoint_ingestion_audit
                SET
                    status = :status,
                    records_loaded = :records_loaded,
                    rows_scanned = COALESCE(:rows_scanned, 0),
                    validation_error_count = :validation_error_count,
                    memory_peak_mb = :memory_peak_mb,
                    duration_seconds = :duration_seconds,
                    ingestion_scope = :ingestion_scope,
                    is_test_data = :is_test_data,
                    message = :message
                WHERE audit_id = :audit_id;
            END
            ELSE IF COL_LENGTH('log.sharepoint_ingestion_audit', 'rows_scanned') IS NOT NULL
                AND COL_LENGTH('log.sharepoint_ingestion_audit', 'validation_error_count') IS NOT NULL
                AND COL_LENGTH('log.sharepoint_ingestion_audit', 'memory_peak_mb') IS NOT NULL
                AND COL_LENGTH('log.sharepoint_ingestion_audit', 'duration_seconds') IS NOT NULL
            BEGIN
                UPDATE log.sharepoint_ingestion_audit
                SET
                    status = :status,
                    records_loaded = :records_loaded,
                    rows_scanned = COALESCE(:rows_scanned, 0),
                    validation_error_count = :validation_error_count,
                    memory_peak_mb = :memory_peak_mb,
                    duration_seconds = :duration_seconds,
                    message = :message
                WHERE audit_id = :audit_id;
            END
            ELSE
            BEGIN
                UPDATE log.sharepoint_ingestion_audit
                SET
                    status = :status,
                    records_loaded = :records_loaded,
                    message = :message
                WHERE audit_id = :audit_id;
            END
        END
        """
        params = {
            "audit_id": audit_id,
            "status": status,
            "records_loaded": records_loaded,
            "message": message,
            "rows_scanned": rows_scanned,
            "validation_error_count": validation_error_count,
            "memory_peak_mb": memory_peak_mb,
            "duration_seconds": duration_seconds,
            "ingestion_scope": ingestion_scope,
            "is_test_data": 1 if is_test_data else 0 if is_test_data is not None else None,
            "destination_database": destination_database,
            "destination_table": destination_table,
        }

        with self._engine.begin() as conn:
            result = conn.execute(text(sql_text), params)
            rowcount = int(result.rowcount or 0)

        return rowcount > 0

    def truncate_and_load(self, df: pd.DataFrame, table_name: str) -> None:
        schema, table = _parse_table_name(table_name)
        qualified_table = f"{_quote_identifier(schema)}.{_quote_identifier(table)}"

        with self._engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE {qualified_table}"))

        self.append_load(df, table_name)

    @staticmethod
    def _normalize_datetime_columns(df: pd.DataFrame) -> pd.DataFrame:
        """Return a copy with timezone-aware datetime columns converted to naive UTC.

        SQL Server DATETIME/DATETIME2 columns do not accept timezone-aware python
        datetimes directly in our pyodbc path; keeping tz info can lead to string
        coercion and truncation errors during executemany.
        """
        if df.empty:
            return df

        normalized = df.copy()
        for col in normalized.columns:
            series = normalized[col]
            if isinstance(series.dtype, pd.DatetimeTZDtype):
                normalized[col] = series.dt.tz_convert("UTC").dt.tz_localize(None)
        return normalized

    def _build_sqlalchemy_dtype_map(
        self,
        table_name: str,
        dataframe_columns: list[str],
    ) -> dict[str, satypes.TypeEngine]:
        """Build a SQLAlchemy dtype map from destination table metadata."""
        try:
            table_columns = self.get_table_columns(table_name)
        except Exception:
            return {}

        col_meta = {str(c["column_name"]).lower(): c for c in table_columns}
        dtype_map: dict[str, satypes.TypeEngine] = {}

        for col_name in dataframe_columns:
            meta = col_meta.get(col_name.lower())
            if not meta:
                continue

            data_type = str(meta.get("data_type") or "").lower()
            char_len = meta.get("character_maximum_length")
            precision = meta.get("numeric_precision")
            scale = meta.get("numeric_scale")

            if data_type in {"varchar", "nvarchar", "char", "nchar"}:
                length = int(char_len) if isinstance(char_len, int) and char_len > 0 else None
                dtype_map[col_name] = satypes.String(length=length)
            elif data_type in {"text", "ntext"}:
                dtype_map[col_name] = satypes.Text()
            elif data_type in {"datetime", "datetime2", "smalldatetime"}:
                dtype_map[col_name] = satypes.DateTime(timezone=False)
            elif data_type == "date":
                dtype_map[col_name] = satypes.Date()
            elif data_type == "time":
                dtype_map[col_name] = satypes.Time()
            elif data_type in {"decimal", "numeric", "money", "smallmoney"}:
                p = int(precision) if isinstance(precision, int) and precision > 0 else 18
                s = int(scale) if isinstance(scale, int) and scale >= 0 else 2
                dtype_map[col_name] = satypes.Numeric(precision=p, scale=s)
            elif data_type == "float":
                dtype_map[col_name] = satypes.Float()
            elif data_type in {"int", "smallint", "tinyint"}:
                dtype_map[col_name] = satypes.Integer()
            elif data_type == "bigint":
                dtype_map[col_name] = satypes.BigInteger()
            elif data_type == "bit":
                dtype_map[col_name] = satypes.Boolean()

        return dtype_map

    def append_load(self, df: pd.DataFrame, table_name: str) -> None:
        if df.empty:
            return

        df = self._normalize_datetime_columns(df)
        schema, table = _parse_table_name(table_name)
        dtype_map = self._build_sqlalchemy_dtype_map(f"{schema}.{table}", list(df.columns))

        # Use method=None (SQLAlchemy executemany) so that pyodbc's fast_executemany=True
        # path is exercised.  This is significantly faster than method="multi" for large
        # DataFrames because pyodbc vectorises the entire batch rather than building a
        # large VALUES(...) string per chunk.
        # chunksize of 10_000 gives a good balance of memory vs round-trips.
        try:
            df.to_sql(
                name=table,
                schema=schema,
                con=self._engine,
                if_exists="append",
                index=False,
                chunksize=10_000,
                method=None,
                dtype=dtype_map or None,
            )
        except SqlIntegrityError as exc:
            raise ValueError(
                f"PRIMARY_KEY_VIOLATION: Appending rows to '{table_name}' failed due to a "
                f"primary key or unique constraint violation. The file may have already been "
                f"loaded (reload scenario) or contains duplicate key values within the file "
                f"itself. Use load_strategy=TRUNCATE for a full reload, or remove conflicting "
                f"rows before retrying APPEND. Original error: {exc}"
            ) from exc

    def copy_stg_to_int(
        self,
        stg_table_name: str,
        int_table_name: str,
        int_database: str,
        load_strategy: str,
    ) -> int:
        """Copy all rows from a staging table into an integrated table in a different DB.

        Uses SQL Server 3-part naming (``[database].[schema].[table]``) so both DBs
        must reside on the **same SQL Server instance**.

        Parameters
        ----------
        stg_table_name:
            Fully-qualified staging table, e.g. ``staging.dest_customers``
            (resolved against *this* connection's database).
        int_table_name:
            Fully-qualified integrated table, e.g. ``staging.dest_customers``
            (resolved inside *int_database*).
        int_database:
            Name of the integrated database, e.g. ``ingest_int_dev``.
        load_strategy:
            ``TRUNCATE`` — truncate the int table before inserting.
            ``APPEND``   — append without truncation.

        Returns
        -------
        int
            Number of rows copied.
        """
        stg_schema, stg_table = _parse_table_name(stg_table_name)
        int_schema, int_table = _parse_table_name(int_table_name)

        stg_q = f"{_quote_identifier(stg_schema)}.{_quote_identifier(stg_table)}"
        int_q = (
            f"{_quote_identifier(int_database)}"
            f".{_quote_identifier(int_schema)}"
            f".{_quote_identifier(int_table)}"
        )

        # Resolve common columns between staging and integrated tables so we can build
        # an explicit INSERT column list (avoids failures when the two tables carry
        # server-managed audit columns that only one side has).
        stg_cols = [str(c["column_name"]) for c in self.get_table_columns(stg_table_name)]

        # Query int table columns via USE-DB trick in a separate EXEC.
        int_col_sql = f"""
            SELECT COLUMN_NAME
            FROM [{int_database}].INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table
            ORDER BY ORDINAL_POSITION
        """
        int_col_rows = self.query_rows(int_col_sql, {"schema": int_schema, "table": int_table})
        int_cols = [str(r["column_name"]) for r in int_col_rows]

        if not int_cols:
            # Fallback: assume same structure as stg
            int_cols = stg_cols

        stg_by_lower = {c.lower(): c for c in stg_cols}
        common_cols = [c for c in int_cols if c.lower() in stg_by_lower]

        if not common_cols:
            raise ValueError(
                f"No common columns found between staging table '{stg_table_name}' "
                f"and integrated table '{int_database}.{int_table_name}'."
            )

        cols_q = ", ".join(_quote_identifier(c) for c in common_cols)

        try:
            with self._engine.begin() as conn:
                if load_strategy == "TRUNCATE":
                    conn.execute(text(f"TRUNCATE TABLE {int_q}"))
                try:
                    conn.execute(
                        text(
                            f"INSERT INTO {int_q} ({cols_q}) "
                            f"SELECT {cols_q} FROM {stg_q}"
                        )
                    )
                    # Retrieve row count from staging table
                    count_result = conn.execute(text(f"SELECT COUNT(*) AS n FROM {stg_q}"))
                    row = count_result.mappings().first()
                    return int(row["n"]) if row else 0
                except SqlIntegrityError as exc:
                    raise ValueError(
                        f"PRIMARY_KEY_VIOLATION: Copying rows from '{stg_table_name}' to "
                        f"'{int_database}.{int_table_name}' failed due to a primary key or "
                        f"unique constraint violation. Original error: {exc}"
                    ) from exc
        except ValueError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Failed to copy staging→integrated: "
                f"stg='{stg_table_name}' int='{int_database}.{int_table_name}': {exc}"
            ) from exc

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
