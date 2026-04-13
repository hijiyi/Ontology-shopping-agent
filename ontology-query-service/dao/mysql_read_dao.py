from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import pymysql
from pymysql.cursors import DictCursor


@dataclass(frozen=True)
class MySQLReadSettings:
    host: str
    port: int
    user: str
    password: str
    database: str
    charset: str = "utf8mb4"


class MySQLReadDao:
    def __init__(self, settings: MySQLReadSettings) -> None:
        self._settings = settings
        self._conn: pymysql.Connection | None = None

    @staticmethod
    def from_env() -> "MySQLReadDao":
        def env_str(key: str, default: str = "") -> str:
            v = os.getenv(key)
            return v if v is not None and v != "" else default

        def env_int(key: str, default: int) -> int:
            v = os.getenv(key)
            if v is None or v == "":
                return default
            try:
                return int(v)
            except ValueError:
                return default

        settings = MySQLReadSettings(
            host=env_str("MYSQL_HOST", "127.0.0.1"),
            port=env_int("MYSQL_PORT", 3306),
            user=env_str("MYSQL_USER", "root"),
            password=env_str("MYSQL_PASSWORD", ""),
            database=env_str("MYSQL_DATABASE", ""),
            charset=env_str("MYSQL_CHARSET", "utf8mb4"),
        )
        return MySQLReadDao(settings)

    def connect(self) -> None:
        if self._conn is not None:
            return
        if not self._settings.database:
            raise RuntimeError("MYSQL_DATABASE is empty")
        self._conn = pymysql.connect(
            host=self._settings.host,
            port=self._settings.port,
            user=self._settings.user,
            password=self._settings.password,
            database=self._settings.database,
            charset=self._settings.charset,
            cursorclass=DictCursor,
            autocommit=True,
        )

    def close(self) -> None:
        if self._conn is None:
            return
        self._conn.close()
        self._conn = None

    def select_safe(self, sql: str, max_limit: int = 100) -> dict[str, Any]:
        normalized = self._normalize_sql(sql)
        self._ensure_select_only(normalized)
        self._ensure_allowed_tables(normalized)
        normalized = self._ensure_limit(normalized, max_limit=max_limit)

        self.connect()
        if self._conn is None:
            raise RuntimeError("mysql not connected")

        with self._conn.cursor() as cursor:
            cursor.execute(normalized)
            rows = cursor.fetchall()

        rows_list = list(rows)
        for row in rows_list:
            if not isinstance(row, dict):
                continue
            for k, v in list(row.items()):
                if isinstance(v, Decimal):
                    row[k] = float(v)
                elif isinstance(v, datetime):
                    row[k] = v.isoformat(sep=" ")
                elif isinstance(v, date):
                    row[k] = v.isoformat()
                elif isinstance(v, time):
                    row[k] = v.isoformat()
        return {"sql": normalized, "rowCount": len(rows_list), "rows": rows_list}

    def _normalize_sql(self, sql: str) -> str:
        s = (sql or "").strip()
        s = re.sub(r"\s+", " ", s)
        if ";" in s:
            s = s.split(";", 1)[0].strip()
        return s

    def _ensure_select_only(self, sql: str) -> None:
        lowered = sql.lower().strip()
        if not lowered.startswith("select"):
            raise ValueError("only SELECT is allowed")
        forbidden = ["insert", "update", "delete", "drop", "alter", "create", "truncate", "replace", "grant", "revoke"]
        for kw in forbidden:
            if re.search(rf"\b{kw}\b", lowered):
                raise ValueError(f"forbidden keyword in sql: {kw}")

    def _ensure_allowed_tables(self, sql: str) -> None:
        allowed_raw = os.getenv("SQL_ALLOWED_TABLES", "t_user,t_order,t_stage,t_product,t_order_item")
        allowed = {t.strip() for t in allowed_raw.split(",") if t.strip()}
        lowered = sql.lower()

        tables = set()
        for m in re.finditer(r"\b(from|join)\s+`?([a-zA-Z0-9_]+)`?", lowered):
            tables.add(m.group(2))
        for t in tables:
            if t not in allowed:
                raise ValueError(f"table not allowed: {t}. allowed: {', '.join(sorted(allowed))}")

    def _ensure_limit(self, sql: str, max_limit: int) -> str:
        lowered = sql.lower()
        m = re.search(r"\blimit\s+(\d+)\b", lowered)
        if not m:
            return f"{sql} LIMIT {max_limit}"
        limit_val = int(m.group(1))
        if limit_val <= max_limit:
            return sql
        return re.sub(r"\blimit\s+\d+\b", f"LIMIT {max_limit}", sql, flags=re.IGNORECASE)
