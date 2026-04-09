from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pymysql
from pymysql.cursors import DictCursor


@dataclass(frozen=True)
class MySQLConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    charset: str = "utf8mb4"


@dataclass(frozen=True)
class ColumnMeta:
    name: str
    data_type: str
    column_type: str
    is_nullable: bool
    column_key: str
    column_comment: str
    ordinal_position: int


@dataclass(frozen=True)
class TableMeta:
    name: str
    columns: list[ColumnMeta]

    def primary_key_column(self) -> str | None:
        for c in self.columns:
            if c.column_key == "PRI":
                return c.name
        return None


class MySQLReader:
    def __init__(self, config: MySQLConfig) -> None:
        self._config = config
        self._conn: pymysql.Connection | None = None

    def connect(self) -> None:
        if self._conn is not None:
            return
        self._conn = pymysql.connect(
            host=self._config.host,
            port=self._config.port,
            user=self._config.user,
            password=self._config.password,
            database=self._config.database,
            charset=self._config.charset,
            cursorclass=DictCursor,
            autocommit=True,
        )

    def close(self) -> None:
        if self._conn is None:
            return
        self._conn.close()
        self._conn = None

    def __enter__(self) -> "MySQLReader":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if self._conn is None:
            raise RuntimeError("MySQL connection is not established. Call connect() first.")
        with self._conn.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        return list(rows)

    @staticmethod
    def _get_ci(row: dict[str, Any], key: str) -> Any:
        if key in row:
            return row[key]
        lower_key = key.lower()
        if lower_key in row:
            return row[lower_key]
        upper_key = key.upper()
        if upper_key in row:
            return row[upper_key]
        for k, v in row.items():
            if isinstance(k, str) and k.lower() == lower_key:
                return v
        raise KeyError(key)

    def list_tables(self, table_prefix: str | None = None) -> list[str]:
        sql = """
        SELECT table_name AS table_name
        FROM information_schema.tables
        WHERE table_schema = %s
          AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """
        rows = self._fetch_all(sql, (self._config.database,))
        tables = [self._get_ci(r, "table_name") for r in rows]
        if table_prefix:
            tables = [t for t in tables if t.startswith(table_prefix)]
        return tables

    def read_table_meta(self, tables: list[str]) -> dict[str, TableMeta]:
        if not tables:
            return {}
        placeholders = ",".join(["%s"] * len(tables))
        sql = f"""
        SELECT
            table_name AS table_name,
            column_name AS column_name,
            data_type AS data_type,
            column_type AS column_type,
            is_nullable AS is_nullable,
            column_key AS column_key,
            IFNULL(column_comment, '') AS column_comment,
            ordinal_position AS ordinal_position
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name IN ({placeholders})
        ORDER BY table_name, ordinal_position
        """
        rows = self._fetch_all(sql, (self._config.database, *tables))

        meta: dict[str, list[ColumnMeta]] = {}
        for r in rows:
            table_name = self._get_ci(r, "table_name")
            meta.setdefault(table_name, []).append(
                ColumnMeta(
                    name=self._get_ci(r, "column_name"),
                    data_type=self._get_ci(r, "data_type"),
                    column_type=self._get_ci(r, "column_type"),
                    is_nullable=(self._get_ci(r, "is_nullable") == "YES"),
                    column_key=(self._get_ci(r, "column_key") or ""),
                    column_comment=(self._get_ci(r, "column_comment") or ""),
                    ordinal_position=int(self._get_ci(r, "ordinal_position")),
                )
            )

        result: dict[str, TableMeta] = {}
        for table_name, columns in meta.items():
            result[table_name] = TableMeta(name=table_name, columns=columns)
        return result

    def read_all_table_data(self, tables: list[str]) -> dict[str, list[dict[str, Any]]]:
        data: dict[str, list[dict[str, Any]]] = {}
        for t in tables:
            rows = self._fetch_all(f"SELECT * FROM `{t}`")
            for row in rows:
                for k, v in list(row.items()):
                    if isinstance(v, Decimal):
                        row[k] = float(v)
            data[t] = rows
        return data
