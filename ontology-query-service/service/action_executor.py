from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dao.neo4j_dao import Neo4jDao


@dataclass(frozen=True)
class ActionDef:
    name: str
    apiPath: str
    method: str


class ActionExecutor:
    def __init__(self, dao: Neo4jDao) -> None:
        self._dao = dao
        self._base_url = os.getenv("BIZ_BASE_URL", "http://127.0.0.1:6000").rstrip("/")

    def resolve_action(self, action_name: str) -> ActionDef | None:
        rows = self._dao.run_read(
            """
            MATCH (a:Ont_Action {name: $name})
            RETURN a.name AS name, a.apiPath AS apiPath, coalesce(a.method, 'POST') AS method
            """,
            {"name": action_name},
        )
        if not rows:
            return None
        r = rows[0]
        api_path = r.get("apiPath")
        if not isinstance(api_path, str) or not api_path.strip():
            return None
        method = r.get("method") or "POST"
        return ActionDef(name=str(r.get("name") or action_name), apiPath=api_path.strip(), method=str(method).upper())

    def resolve_action_ui(self, action_name: str) -> dict[str, Any] | None:
        rows = self._dao.run_read(
            """
            MATCH (a:Ont_Action {name: $name})
            RETURN
              a.name AS name,
              a.apiPath AS apiPath,
              coalesce(a.method, 'POST') AS method,
              a.uiSchema AS uiSchema
            """,
            {"name": action_name},
        )
        if not rows:
            return None
        r = rows[0]
        api_path = r.get("apiPath")
        if not isinstance(api_path, str) or not api_path.strip():
            return None
        ui_schema = r.get("uiSchema")
        if ui_schema is None:
            ui_schema = None
        elif not isinstance(ui_schema, str):
            ui_schema = str(ui_schema)
        return {
            "name": str(r.get("name") or action_name),
            "apiPath": api_path.strip(),
            "method": str(r.get("method") or "POST").upper(),
            "uiSchema": ui_schema,
        }

    def execute(self, action_name: str, params: dict[str, Any]) -> dict[str, Any]:
        action = self.resolve_action(action_name)
        if action is None:
            return {"ok": False, "error": f"action not found or missing apiPath: {action_name}"}

        url = f"{self._base_url}{action.apiPath}"
        method = action.method.upper()
        body = None
        headers: dict[str, str] = {}
        if method == "GET":
            if params:
                qs = urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
                url = f"{url}?{qs}"
        else:
            body = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(url=url, data=body, method=method, headers=headers)

        try:
            with urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return {"ok": True, "action": {"name": action.name, "apiPath": action.apiPath, "method": method}, "response": payload}
        except HTTPError as e:
            try:
                raw = e.read().decode("utf-8")
            except Exception:
                raw = ""
            return {"ok": False, "error": f"http error: {e.code}", "details": raw}
        except (URLError, TimeoutError) as e:
            return {"ok": False, "error": f"request failed: {e}"}
        except ValueError as e:
            return {"ok": False, "error": f"invalid json response: {e}"}
