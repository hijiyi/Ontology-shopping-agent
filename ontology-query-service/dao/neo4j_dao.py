from __future__ import annotations

from typing import Any

from neo4j import GraphDatabase


class Neo4jDao:
    def __init__(self, uri: str, user: str, password: str, database: str) -> None:
        self._database = database
        self._driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self) -> None:
        self._driver.close()

    def _to_jsonable(self, v: Any) -> Any:
        if v is None or isinstance(v, (bool, int, float, str)):
            return v
        if isinstance(v, bytes):
            try:
                return v.decode("utf-8", errors="ignore")
            except Exception:
                return str(v)
        if isinstance(v, (list, tuple)):
            return [self._to_jsonable(x) for x in v]
        if isinstance(v, dict):
            return {k: self._to_jsonable(x) for k, x in v.items()}
        try:
            iso = getattr(v, "isoformat", None)
            if callable(iso):
                return iso()
        except Exception:
            pass
        try:
            to_native = getattr(v, "to_native", None)
            if callable(to_native):
                nv = to_native()
                return self._to_jsonable(nv)
        except Exception:
            pass
        return str(v)

    def run_read(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with self._driver.session(database=self._database) as session:
            result = session.execute_read(lambda tx: tx.run(cypher, **(params or {})).data())
        rows = list(result)
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(self._to_jsonable(r))
        return out

    def run_write(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with self._driver.session(database=self._database) as session:
            result = session.execute_write(lambda tx: tx.run(cypher, **(params or {})).data())
        rows = list(result)
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(self._to_jsonable(r))
        return out
