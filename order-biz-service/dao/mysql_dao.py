from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pymysql
from pymysql.cursors import DictCursor


@dataclass(frozen=True)
class MySQLSettings:
    host: str
    port: int
    user: str
    password: str
    database: str
    charset: str = "utf8mb4"


class MySQLDao:
    def __init__(self, settings: MySQLSettings) -> None:
        self._settings = settings
        self._conn: pymysql.Connection | None = None

    def connect(self) -> None:
        if self._conn is not None:
            return
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

    def __enter__(self) -> "MySQLDao":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        if self._conn is None:
            self.connect()
        if self._conn is None:
            raise RuntimeError("mysql not connected")
        with self._conn.cursor() as cursor:
            cursor.execute(sql, params)
            row = cursor.fetchone()
        return dict(row) if row else None

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if self._conn is None:
            self.connect()
        if self._conn is None:
            raise RuntimeError("mysql not connected")
        with self._conn.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        return [dict(r) for r in list(rows)]

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        if self._conn is None:
            self.connect()
        if self._conn is None:
            raise RuntimeError("mysql not connected")
        with self._conn.cursor() as cursor:
            cursor.execute(sql, params)
            affected = cursor.rowcount
        return int(affected)

    def insert_and_get_id(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        if self._conn is None:
            self.connect()
        if self._conn is None:
            raise RuntimeError("mysql not connected")
        with self._conn.cursor() as cursor:
            cursor.execute(sql, params)
            new_id = cursor.lastrowid
        return int(new_id)
