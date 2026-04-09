from __future__ import annotations

import os
import logging
import atexit
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

from dao.neo4j_dao import Neo4jDao
from dao.mysql_read_dao import MySQLReadDao
from service.query_service import QueryService
from service.nlq_planner import NLQPlanner
from service.chat_service import ChatService
from service.action_executor import ActionExecutor
from service.react_agent import ReActAgent


def build_logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    return logging.getLogger("ontology-query-service")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ.setdefault(key, value)


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


def create_app() -> Flask:
    load_dotenv(Path(__file__).with_name(".env"))
    app = Flask(__name__, static_folder=None)
    logger = build_logger()

    dao = Neo4jDao(
        uri=env_str("NEO4J_URI", "bolt://127.0.0.1:7687"),
        user=env_str("NEO4J_USER", "neo4j"),
        password=env_str("NEO4J_PASSWORD", ""),
        database=env_str("NEO4J_DATABASE", "neo4j"),
    )
    mysql = MySQLReadDao.from_env()
    service = QueryService(dao)
    planner = NLQPlanner(dao)
    chat_service = ChatService(planner, service, dao)
    action_executor = ActionExecutor(dao)
    react_agent = ReActAgent(dao, service, chat_service)

    atexit.register(dao.close)

    web_dir = Path(__file__).with_name("web")
    kb_chunk_index_ready = False

    def ensure_kb_indexes() -> None:
        nonlocal kb_chunk_index_ready
        if kb_chunk_index_ready:
            return
        try:
            dao.run_write("CREATE CONSTRAINT kb_doc_id IF NOT EXISTS FOR (d:Ont_Doc) REQUIRE d.docId IS UNIQUE")
        except Exception:
            pass
        try:
            dao.run_write("CREATE CONSTRAINT kb_chunk_id IF NOT EXISTS FOR (c:Ont_Chunk) REQUIRE c.chunkId IS UNIQUE")
        except Exception:
            pass
        try:
            dao.run_write("CREATE FULLTEXT INDEX kb_chunk_text IF NOT EXISTS FOR (c:Ont_Chunk) ON EACH [c.text]")
        except Exception:
            pass
        kb_chunk_index_ready = True

    def chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
        t = (text or "").strip()
        if not t:
            return []
        if chunk_size <= 0:
            chunk_size = 600
        if chunk_size > 4000:
            chunk_size = 4000
        if overlap < 0:
            overlap = 0
        if overlap >= chunk_size:
            overlap = max(0, chunk_size // 5)

        out: list[str] = []
        i = 0
        n = len(t)
        while i < n:
            j = min(n, i + chunk_size)
            out.append(t[i:j])
            if j >= n:
                break
            i = max(0, j - overlap)
        return out

    def should_block_execute_for_question(q: str) -> bool:
        t = (q or "").strip()
        if not t:
            return True
        if any(k in t for k in ["从知识库", "知识库", "处理建议", "排查", "解释", "说明", "是什么原因", "为什么", "怎么做"]):
            if not any(k in t for k in ["下单", "创建订单", "新建订单", "支付", "付款", "推进", "变更", "更新"]):
                return True
        return False

    def normalize_extracted_user_name(name: str) -> str:
        n = (name or "").strip()
        if not n:
            return ""
        stop = {"将", "把", "给", "为", "让", "请", "帮", "帮我", "我", "你", "他", "她", "它"}
        if n in stop:
            return ""
        if n.endswith("单") and len(n) <= 2:
            return ""
        return n

    @app.get("/")
    def index():
        return send_from_directory(web_dir, "index.html")

    @app.post("/query")
    def query():
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        query_type = payload.get("queryType")
        name = payload.get("name")

        if query_type not in ("entity", "action", "relation", "instance"):
            return jsonify({"ok": False, "error": "queryType must be one of: entity|action|relation|instance"}), 400
        if not isinstance(name, str) or not name.strip():
            return jsonify({"ok": False, "error": "name must be a non-empty string"}), 400

        try:
            result = service.query(query_type, name.strip())
            return jsonify({"ok": True, "result": result.__dict__})
        except Exception as e:
            logger.exception("query failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/ask")
    def ask():
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        question = payload.get("question") or payload.get("text") or payload.get("name")
        include_debug = bool(payload.get("debug"))

        if not isinstance(question, str) or not question.strip():
            return jsonify({"ok": False, "error": "question must be a non-empty string"}), 400

        try:
            plan = planner.plan(question.strip())
            if include_debug:
                debug = service.query_with_debug(plan.queryType, plan.name)
                return jsonify(
                    {
                        "ok": True,
                        "question": question.strip(),
                        "plan": plan.__dict__,
                        "debug": debug,
                    }
                )

            result = service.query(plan.queryType, plan.name)
            return jsonify({"ok": True, "question": question.strip(), "plan": plan.__dict__, "result": result.__dict__})
        except Exception as e:
            logger.exception("ask failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/chat")
    def chat():
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        message = payload.get("message") or payload.get("question") or payload.get("text")
        debug = bool(payload.get("debug"))

        if not isinstance(message, str) or not message.strip():
            return jsonify({"ok": False, "error": "message must be a non-empty string"}), 400

        try:
            resp = chat_service.chat(message.strip(), debug=debug)
            return jsonify({"ok": True, **resp})
        except Exception as e:
            logger.exception("chat failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/ui/plan")
    def ui_plan():
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        message = payload.get("message") or payload.get("question") or payload.get("text")
        debug = bool(payload.get("debug"))

        if not isinstance(message, str) or not message.strip():
            return jsonify({"ok": False, "error": "message must be a non-empty string"}), 400

        try:
            plan, llm_used_for_plan = chat_service.plan_only(message.strip())
            plan_dict = plan.__dict__ if hasattr(plan, "__dict__") else dict(plan)

            if plan_dict.get("queryType") != "execute":
                resp = chat_service.chat(message.strip(), debug=debug)
                return jsonify({"ok": True, "kind": "chat", **resp})

            slots = plan_dict.get("slots") if isinstance(plan_dict.get("slots"), dict) else {}
            action_name = str(slots.get("actionName") or "").strip()
            extracted_params = slots.get("params") if isinstance(slots.get("params"), dict) else {}

            info = action_executor.resolve_action_ui(action_name) if action_name else None
            if info is None:
                return jsonify({"ok": True, "kind": "chat", **chat_service.chat(message.strip(), debug=debug)})

            ui_schema_raw = info.get("uiSchema")
            form = None
            if isinstance(ui_schema_raw, str) and ui_schema_raw.strip():
                import json as _json

                try:
                    form = _json.loads(ui_schema_raw)
                except ValueError:
                    form = {"title": action_name, "description": "uiSchema 不是合法 JSON", "fields": []}
            else:
                form = {"title": action_name, "fields": []}

            fields = form.get("fields") if isinstance(form, dict) else None
            if not isinstance(fields, list):
                fields = []
                if isinstance(form, dict):
                    form["fields"] = fields

            for f in fields:
                if not isinstance(f, dict):
                    continue
                n = f.get("name")
                if isinstance(n, str) and n in extracted_params and "value" not in f:
                    f["value"] = extracted_params.get(n)

            return jsonify(
                {
                    "ok": True,
                    "kind": "action_form",
                    "llm": {
                        "provider": os.getenv("LLM_PROVIDER", "off"),
                        "model": os.getenv("LLM_MODEL", ""),
                        "usedForPlan": llm_used_for_plan,
                    },
                    "plan": plan_dict,
                    "action": {"name": info.get("name"), "apiPath": info.get("apiPath"), "method": info.get("method")},
                    "form": form,
                    "prefill": extracted_params,
                }
            )
        except Exception as e:
            logger.exception("ui_plan failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/ui/execute")
    def ui_execute():
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        action_name = payload.get("actionName")
        params = payload.get("params")
        debug = bool(payload.get("debug"))
        message = payload.get("message") or payload.get("question") or payload.get("text") or ""

        if not isinstance(action_name, str) or not action_name.strip():
            return jsonify({"ok": False, "error": "actionName must be a non-empty string"}), 400
        if not isinstance(params, dict):
            return jsonify({"ok": False, "error": "params must be an object"}), 400

        try:
            resp = chat_service.execute_action(action_name.strip(), params, str(message), False, debug)
            if isinstance(resp, dict) and resp.get("ok") is True and os.getenv("EXEC_AGENT_INTERPRET", "0").lower() in ("1", "true", "yes"):
                try:
                    oid = None
                    if isinstance(params, dict) and params.get("orderId"):
                        oid = params.get("orderId")
                    ex = resp.get("response") or resp.get("execution") or {}
                    if oid is None and isinstance(ex, dict):
                        inner = ex.get("result") if isinstance(ex.get("result"), dict) else ex.get("response")
                        if isinstance(inner, dict) and inner.get("result") and isinstance(inner.get("result"), dict):
                            oid = inner.get("result").get("orderId")
                        elif isinstance(inner, dict) and inner.get("orderId") is not None:
                            oid = inner.get("orderId")
                    if oid is not None:
                        oid_int = int(oid)
                        obs1 = mysql.select_safe(f"SELECT current_stage, update_time FROM t_order WHERE id = {oid_int} LIMIT 1")
                        obs2 = mysql.select_safe(f"SELECT stage_name, update_time FROM t_stage WHERE order_id = {oid_int} ORDER BY id ASC LIMIT 20")
                        cur = None
                        if isinstance(obs1.get("rows"), list) and obs1["rows"]:
                            cur = obs1["rows"][0].get("current_stage")
                        stages = []
                        if isinstance(obs2.get("rows"), list):
                            for r in obs2["rows"]:
                                nm = r.get("stage_name")
                                tm = r.get("update_time")
                                if nm:
                                    stages.append({"stage": nm, "time": tm})
                        seq = " -> ".join([s["stage"] for s in stages if s.get("stage")])
                        report = f"执行完成。订单 {oid_int} 当前阶段：{cur or '未知'}。阶段历史：{seq or '无'}。"
                        resp["report"] = report
                        resp["stageHistory"] = stages
                except Exception:
                    pass
            return jsonify({"ok": True, "kind": "execution_result", **resp})
        except Exception as e:
            logger.exception("ui_execute failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/smart")
    def smart():
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        message = payload.get("message") or payload.get("question") or payload.get("text")
        debug = bool(payload.get("debug"))
        agent_on = bool(payload.get("react")) or bool(payload.get("agent"))
        max_steps = payload.get("maxSteps")
        try:
            max_steps_val = int(max_steps) if max_steps is not None else int(os.getenv("REACT_MAX_STEPS", "5"))
        except ValueError:
            max_steps_val = 5

        if not isinstance(message, str) or not message.strip():
            return jsonify({"ok": False, "error": "message must be a non-empty string"}), 400

        text = message.strip()
        try:
            plan, llm_used_for_plan = chat_service.plan_only(text)
            plan_dict = plan.__dict__ if hasattr(plan, "__dict__") else dict(plan)

            if plan_dict.get("queryType") == "execute":
                if should_block_execute_for_question(text):
                    resp = chat_service.chat(text, debug=debug)
                    return jsonify({"ok": True, "kind": "chat", **resp})

                slots = plan_dict.get("slots") if isinstance(plan_dict.get("slots"), dict) else {}
                action_name = str(slots.get("actionName") or "").strip()
                extracted_params = slots.get("params") if isinstance(slots.get("params"), dict) else {}

                info = action_executor.resolve_action_ui(action_name) if action_name else None
                if info is None:
                    resp = chat_service.chat(text, debug=debug)
                    return jsonify({"ok": True, "kind": "chat", **resp})

                ui_schema_raw = info.get("uiSchema")
                form = None
                if isinstance(ui_schema_raw, str) and ui_schema_raw.strip():
                    import json as _json

                    try:
                        form = _json.loads(ui_schema_raw)
                    except ValueError:
                        form = {"title": action_name, "description": "uiSchema 不是合法 JSON", "fields": []}
                else:
                    form = {"title": action_name, "fields": []}

                prefill_meta: dict[str, Any] = {}

                # Agent-driven prefill via ReAct (optional)
                try:
                    use_agent_prefill_react = bool(agent_on) and os.getenv("EXEC_AGENT_PREFILL_MODE", "").lower() in ("react", "llm")
                except Exception:
                    use_agent_prefill_react = False
                if use_agent_prefill_react:
                    try:
                        oid_try = extracted_params.get("orderId")
                        need_oid = oid_try in (None, "", 0)
                        if action_name in ("payOrder", "advanceStage") and need_oid:
                            prompt = (
                                "为下面的执行动作补全缺失参数，不要执行动作，只用工具查询需要的字段并给出结论。\n"
                                f"动作: {action_name}\n"
                                f"已知参数: {extracted_params}\n"
                                f"用户输入: {text}\n"
                                "如果缺少 orderId：\n"
                                "- 若给了 userName 且包含“最近/最新”，用以下 SQL 模式查询该用户最近一单的 id：\n"
                                "  SELECT id FROM t_order WHERE user_id = (SELECT id FROM t_user WHERE name = 'Alice' LIMIT 1) ORDER BY update_time DESC, id DESC LIMIT 1\n"
                                "- 若未给 userName 但包含“最近/最新”，用以下 SQL 模式查询系统全局最近一单的 id：\n"
                                "  SELECT o.id FROM t_order o ORDER BY o.update_time DESC, o.id DESC LIMIT 1\n"
                                "限制：必须只调用 sql_query（只读、表白名单 t_user|t_order|t_stage、强制 LIMIT），或必要时使用 neo4j_query 查询结构信息；不要调用 execute_action。"
                            )
                            try:
                                max_steps_prefill = int(os.getenv("EXEC_AGENT_PREFILL_STEPS", "6"))
                            except Exception:
                                max_steps_prefill = 6
                            agent_resp = react_agent.run(
                                prompt,
                                max_steps=max_steps_prefill,
                                debug=True,
                                allow_execute=True,
                                tool_allowlist=["sql_query", "neo4j_query"],
                            )
                            prefill_meta["reactTried"] = True
                            if isinstance(agent_resp, dict):
                                prefill_meta["agentOk"] = agent_resp.get("ok")
                                if agent_resp.get("ok") is False and agent_resp.get("error"):
                                    prefill_meta["agentError"] = str(agent_resp.get("error"))[:300]
                            steps_list = agent_resp.get("steps") if isinstance(agent_resp, dict) else None
                            if debug and isinstance(steps_list, list):
                                prefill_meta["agentSteps"] = steps_list
                            if isinstance(steps_list, list):
                                prefill_meta["agentStepsCount"] = len(steps_list)
                            found_id = None
                            if isinstance(steps_list, list):
                                for st in reversed(steps_list):
                                    if not isinstance(st, dict):
                                        continue
                                    if st.get("tool") != "sql_query":
                                        continue
                                    obs = st.get("observation")
                                    if not isinstance(obs, dict):
                                        continue
                                    rows = obs.get("rows")
                                    if isinstance(rows, list) and rows:
                                        row0 = rows[0] if isinstance(rows[0], dict) else None
                                        if isinstance(row0, dict):
                                            # common id keys
                                            cand = row0.get("id")
                                            if cand is None:
                                                # try orderId, order_id
                                                cand = row0.get("orderId") or row0.get("order_id")
                                            if cand is not None:
                                                try:
                                                    found_id = int(cand)
                                                except Exception:
                                                    pass
                                    if found_id is not None:
                                        break
                            if found_id is not None:
                                extracted_params = dict(extracted_params)
                                extracted_params["orderId"] = found_id
                                prefill_meta["source"] = "agent_react"
                                if isinstance(form, dict):
                                    hint = f"通过 Agent 校验：定位最近一单订单 id={found_id}"
                                    desc_old = (form.get("description") or "").strip()
                                    form["description"] = (desc_old + "\n" + hint).strip() if desc_old else hint
                    except Exception:
                        pass

                # Optional: Agent-like prefill/checks before showing the card
                if (bool(agent_on) and os.getenv("EXEC_AGENT_PREFILL", "0").lower() in ("1", "true", "yes")):
                    try:
                        if action_name in ("payOrder", "advanceStage"):
                            oid_pref = extracted_params.get("orderId")
                            if oid_pref not in (None, "", 0):
                                try:
                                    oid_int = int(oid_pref)
                                    obs_chk = mysql.select_safe(f"SELECT current_stage FROM t_order WHERE id = {oid_int} LIMIT 1")
                                    rows_chk = obs_chk.get("rows") if isinstance(obs_chk.get("rows"), list) else []
                                    if rows_chk and isinstance(rows_chk[0], dict):
                                        cur = rows_chk[0].get("current_stage")
                                        hint = f"检查：订单 {oid_int} 当前阶段为 {cur or '未知'}"
                                        if "source" not in prefill_meta:
                                            prefill_meta["source"] = "deterministic"
                                        if isinstance(form, dict):
                                            desc_old = (form.get("description") or "").strip()
                                            form["description"] = (desc_old + "\n" + hint).strip() if desc_old else hint
                                except Exception:
                                    pass
                    except Exception:
                        pass

                order_id_raw = extracted_params.get("orderId")
                order_id_missing = order_id_raw is None or order_id_raw == "" or order_id_raw == 0

                if action_name == "payOrder" and order_id_missing:
                    hinted_user = extracted_params.get("userName") if isinstance(extracted_params.get("userName"), str) else ""
                    hinted_order_hint = extracted_params.get("orderHint") if isinstance(extracted_params.get("orderHint"), str) else ""
                    user_name = ""
                    patterns = [
                        r"(?P<user>[A-Za-z0-9_\u4e00-\u9fa5]+)\s*最近\s*(?:一单|1单)(?:订单)?\s*(?:支付|付款)",
                        r"(?:支付|付款)\s*(?P<user>[A-Za-z0-9_\u4e00-\u9fa5]+)\s*最近\s*(?:一单|1单)(?:订单)?",
                        r"(?:支付|付款)\s*最近\s*(?:一单|1单)(?:订单)?\s*(?P<user>[A-Za-z0-9_\u4e00-\u9fa5]+)",
                    ]
                    for p in patterns:
                        m = re.search(p, text, flags=re.IGNORECASE)
                        if m:
                            user_name = str(m.group("user") or "").strip()
                            break

                    if not user_name and hinted_user:
                        if hinted_order_hint.lower() in ("latest", "recent", "newest") or any(k in text for k in ["最近", "最新"]):
                            user_name = hinted_user.strip()

                    user_name = normalize_extracted_user_name(user_name)

                    if user_name and "'" not in user_name and '"' not in user_name:
                            sql_last = (
                                "SELECT id, amount, update_time "
                                "FROM t_order "
                                f"WHERE user_id = (SELECT id FROM t_user WHERE name='{user_name}' LIMIT 1) "
                                "ORDER BY update_time DESC "
                                "LIMIT 1"
                            )
                            obs = mysql.select_safe(sql_last, max_limit=100)
                            rows = obs.get("rows") if isinstance(obs.get("rows"), list) else []
                            if rows and isinstance(rows[0], dict) and rows[0].get("id") is not None:
                                extracted_params = dict(extracted_params)
                                extracted_params["orderId"] = int(rows[0].get("id"))
                                if "source" not in prefill_meta:
                                    prefill_meta["source"] = "deterministic"
                                if isinstance(form, dict):
                                    amount = rows[0].get("amount")
                                    upd = rows[0].get("update_time")
                                    hint = f"已自动定位 {user_name} 最近一单订单：id={extracted_params['orderId']}"
                                    if amount is not None:
                                        hint += f"，amount={amount}"
                                    if upd is not None:
                                        hint += f"，time={upd}"
                                    desc = (form.get("description") or "").strip()
                                    form["description"] = (desc + "\n" + hint).strip() if desc else hint
                            else:
                                if isinstance(form, dict):
                                    hint = f"未能定位 {user_name} 的最近一单订单，请手动填写订单ID"
                                    desc = (form.get("description") or "").strip()
                                    form["description"] = (desc + "\n" + hint).strip() if desc else hint
                                    desc = (form.get("description") or "").strip()
                                    form["description"] = (desc + "\n" + hint).strip() if desc else hint
                    elif hinted_order_hint.lower() in ("latest", "recent", "newest") or any(k in text for k in ["最近", "最新"]):
                        sql_last = (
                            "SELECT o.id AS id, o.amount AS amount, o.update_time AS update_time, u.name AS user_name "
                            "FROM t_order o "
                            "LEFT JOIN t_user u ON o.user_id = u.id "
                            "ORDER BY o.update_time DESC, o.id DESC "
                            "LIMIT 1"
                        )
                        obs = mysql.select_safe(sql_last, max_limit=100)
                        rows = obs.get("rows") if isinstance(obs.get("rows"), list) else []
                        if rows and isinstance(rows[0], dict) and rows[0].get("id") is not None:
                            extracted_params = dict(extracted_params)
                            extracted_params["orderId"] = int(rows[0].get("id"))
                            if "source" not in prefill_meta:
                                prefill_meta["source"] = "deterministic"
                            if isinstance(form, dict):
                                amount = rows[0].get("amount")
                                upd = rows[0].get("update_time")
                                uname = rows[0].get("user_name")
                                hint = f"已自动定位系统最近一单订单：id={extracted_params['orderId']}"
                                if uname:
                                    hint += f"，user={uname}"
                                if amount is not None:
                                    hint += f"，amount={amount}"
                                if upd is not None:
                                    hint += f"，time={upd}"
                                desc = (form.get("description") or "").strip()
                                form["description"] = (desc + "\n" + hint).strip() if desc else hint
                        else:
                            if isinstance(form, dict):
                                hint = "未找到任何订单，请先创建订单后再支付"
                                desc = (form.get("description") or "").strip()
                                form["description"] = (desc + "\n" + hint).strip() if desc else hint

                order_id_raw2 = extracted_params.get("orderId")
                order_id_missing2 = order_id_raw2 is None or order_id_raw2 == "" or order_id_raw2 == 0

                if action_name == "advanceStage" and order_id_missing2:
                    hinted_user = extracted_params.get("userName") if isinstance(extracted_params.get("userName"), str) else ""
                    hinted_order_hint = extracted_params.get("orderHint") if isinstance(extracted_params.get("orderHint"), str) else ""
                    user_name = ""
                    patterns = [
                        r"(?P<user>[A-Za-z0-9_\u4e00-\u9fa5]+)\s*(?:最近|最新)\s*(?:一单|1单)?(?:订单)?",
                        r"(?:最近|最新)\s*(?:一单|1单)?(?:订单)?\s*(?P<user>[A-Za-z0-9_\u4e00-\u9fa5]+)",
                    ]
                    for p in patterns:
                        m = re.search(p, text, flags=re.IGNORECASE)
                        if m:
                            user_name = str(m.group("user") or "").strip()
                            break

                    if not user_name and hinted_user:
                        if hinted_order_hint.lower() in ("latest", "recent", "newest") or any(k in text for k in ["最近", "最新"]):
                            user_name = hinted_user.strip()

                    user_name = normalize_extracted_user_name(user_name)

                    if user_name and "'" not in user_name and '"' not in user_name:
                        sql_last = (
                            "SELECT id, amount, update_time "
                            "FROM t_order "
                            f"WHERE user_id = (SELECT id FROM t_user WHERE name='{user_name}' LIMIT 1) "
                            "ORDER BY update_time DESC "
                            "LIMIT 1"
                        )
                        obs = mysql.select_safe(sql_last, max_limit=100)
                        rows = obs.get("rows") if isinstance(obs.get("rows"), list) else []
                        if rows and isinstance(rows[0], dict) and rows[0].get("id") is not None:
                            extracted_params = dict(extracted_params)
                            extracted_params["orderId"] = int(rows[0].get("id"))
                            if "source" not in prefill_meta:
                                prefill_meta["source"] = "deterministic"
                            if isinstance(form, dict):
                                amount = rows[0].get("amount")
                                upd = rows[0].get("update_time")
                                hint = f"已自动定位 {user_name} 最近一单订单：id={extracted_params['orderId']}"
                                if amount is not None:
                                    hint += f"，amount={amount}"
                                if upd is not None:
                                    hint += f"，time={upd}"
                                desc = (form.get("description") or "").strip()
                                form["description"] = (desc + "\n" + hint).strip() if desc else hint
                    elif hinted_order_hint.lower() in ("latest", "recent", "newest") or any(k in text for k in ["最近", "最新"]):
                        sql_last = (
                            "SELECT o.id AS id, o.amount AS amount, o.update_time AS update_time, u.name AS user_name "
                            "FROM t_order o "
                            "LEFT JOIN t_user u ON o.user_id = u.id "
                            "ORDER BY o.update_time DESC, o.id DESC "
                            "LIMIT 1"
                        )
                        obs = mysql.select_safe(sql_last, max_limit=100)
                        rows = obs.get("rows") if isinstance(obs.get("rows"), list) else []
                        if rows and isinstance(rows[0], dict) and rows[0].get("id") is not None:
                            extracted_params = dict(extracted_params)
                            extracted_params["orderId"] = int(rows[0].get("id"))
                            if "source" not in prefill_meta:
                                prefill_meta["source"] = "deterministic"
                            if isinstance(form, dict):
                                amount = rows[0].get("amount")
                                upd = rows[0].get("update_time")
                                uname = rows[0].get("user_name")
                                hint = f"已自动定位系统最近一单订单：id={extracted_params['orderId']}"
                                if uname:
                                    hint += f"，user={uname}"
                                if amount is not None:
                                    hint += f"，amount={amount}"
                                if upd is not None:
                                    hint += f"，time={upd}"
                                desc = (form.get("description") or "").strip()
                                form["description"] = (desc + "\n" + hint).strip() if desc else hint
                        else:
                            if isinstance(form, dict):
                                hint = "未找到任何订单，请先创建订单后再推进阶段"
                                desc = (form.get("description") or "").strip()
                                form["description"] = (desc + "\n" + hint).strip() if desc else hint

                fields = form.get("fields") if isinstance(form, dict) else None
                if not isinstance(fields, list):
                    fields = []
                    if isinstance(form, dict):
                        form["fields"] = fields

                for f in fields:
                    if not isinstance(f, dict):
                        continue
                    n = f.get("name")
                    if isinstance(n, str) and n in extracted_params and "value" not in f:
                        f["value"] = extracted_params.get(n)

                return jsonify(
                    {
                        "ok": True,
                        "kind": "action_form",
                        "llm": {
                            "provider": os.getenv("LLM_PROVIDER", "off"),
                            "model": os.getenv("LLM_MODEL", ""),
                            "usedForPlan": llm_used_for_plan,
                        },
                        "plan": plan_dict,
                        "action": {"name": info.get("name"), "apiPath": info.get("apiPath"), "method": info.get("method")},
                        "form": form,
                        "prefill": extracted_params,
                        **({"prefillMeta": prefill_meta} if prefill_meta else {}),
                    }
                )

            if agent_on:
                resp = react_agent.run(text, max_steps=max_steps_val, debug=debug)
                return jsonify(resp)

            resp = chat_service.chat(text, debug=debug)
            return jsonify({"ok": True, "kind": "chat", **resp})
        except Exception as e:
            logger.exception("smart failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/kb/upsert")
    def kb_upsert():
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        text = payload.get("text")
        title = payload.get("title") or ""
        source = payload.get("source") or ""
        doc_id = payload.get("docId") or payload.get("id")
        try:
            chunk_size = int(payload.get("chunkSize") or 600)
        except ValueError:
            chunk_size = 600
        try:
            overlap = int(payload.get("overlap") or 80)
        except ValueError:
            overlap = 80

        if not isinstance(text, str) or not text.strip():
            return jsonify({"ok": False, "error": "text must be a non-empty string"}), 400
        if not isinstance(title, str):
            title = str(title)
        if not isinstance(source, str):
            source = str(source)
        if not isinstance(doc_id, str) or not doc_id.strip():
            doc_id = uuid.uuid4().hex
        doc_id = doc_id.strip()

        ensure_kb_indexes()
        chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"

        try:
            dao.run_write(
                """
                MERGE (d:Ont_Doc {docId: $docId})
                SET d.title = $title,
                    d.source = $source,
                    d.updatedAt = $now
                """,
                {"docId": doc_id, "title": title, "source": source, "now": now},
            )
            dao.run_write(
                """
                MATCH (d:Ont_Doc {docId: $docId})
                OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Ont_Chunk)
                DETACH DELETE c
                """,
                {"docId": doc_id},
            )
            dao.run_write(
                """
                MATCH (d:Ont_Doc {docId: $docId})
                UNWIND $chunks AS ch
                MERGE (c:Ont_Chunk {chunkId: ch.chunkId})
                SET c.docId = $docId,
                    c.idx = ch.idx,
                    c.text = ch.text,
                    c.updatedAt = $now
                MERGE (d)-[:HAS_CHUNK]->(c)
                """,
                {
                    "docId": doc_id,
                    "now": now,
                    "chunks": [{"chunkId": f"{doc_id}:{i}", "idx": i, "text": s} for i, s in enumerate(chunks)],
                },
            )
            return jsonify({"ok": True, "docId": doc_id, "chunkCount": len(chunks)})
        except Exception as e:
            logger.exception("kb_upsert failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/kb/search")
    def kb_search():
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        q = payload.get("query") or payload.get("q") or payload.get("text")
        try:
            limit = int(payload.get("limit") or 5)
        except ValueError:
            limit = 5
        if limit <= 0:
            limit = 5
        if limit > 20:
            limit = 20
        if not isinstance(q, str) or not q.strip():
            return jsonify({"ok": False, "error": "query must be a non-empty string"}), 400

        ensure_kb_indexes()
        query_text = q.strip()
        try:
            rows = dao.run_read(
                """
                CALL db.index.fulltext.queryNodes('kb_chunk_text', $q) YIELD node, score
                OPTIONAL MATCH (d:Ont_Doc {docId: node.docId})
                RETURN
                  node.chunkId AS chunkId,
                  node.docId AS docId,
                  node.idx AS idx,
                  node.text AS text,
                  d.title AS docTitle,
                  d.source AS docSource,
                  score AS score
                ORDER BY score DESC
                LIMIT $limit
                """,
                {"q": query_text, "limit": limit},
            )
            hits = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                hits.append(
                    {
                        "docId": r.get("docId"),
                        "docTitle": r.get("docTitle"),
                        "docSource": r.get("docSource"),
                        "chunkId": r.get("chunkId"),
                        "idx": r.get("idx"),
                        "score": r.get("score"),
                        "text": r.get("text"),
                    }
                )
            return jsonify({"ok": True, "query": query_text, "hits": hits})
        except Exception:
            try:
                rows = dao.run_read(
                    """
                    MATCH (c:Ont_Chunk)
                    WHERE toLower(c.text) CONTAINS toLower($q)
                    OPTIONAL MATCH (d:Ont_Doc {docId: c.docId})
                    RETURN
                      c.chunkId AS chunkId,
                      c.docId AS docId,
                      c.idx AS idx,
                      c.text AS text,
                      d.title AS docTitle,
                      d.source AS docSource
                    LIMIT $limit
                    """,
                    {"q": query_text, "limit": limit},
                )
                hits = []
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    hits.append(
                        {
                            "docId": r.get("docId"),
                            "docTitle": r.get("docTitle"),
                            "docSource": r.get("docSource"),
                            "chunkId": r.get("chunkId"),
                            "idx": r.get("idx"),
                            "score": None,
                            "text": r.get("text"),
                        }
                    )
                return jsonify({"ok": True, "query": query_text, "hits": hits})
            except Exception as e:
                logger.exception("kb_search failed")
                return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/kb/stats")
    def kb_stats():
        ensure_kb_indexes()
        try:
            rows = dao.run_read(
                """
                MATCH (d:Ont_Doc)
                OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c:Ont_Chunk)
                RETURN count(DISTINCT d) AS docCount, count(DISTINCT c) AS chunkCount
                """,
                {},
            )
            r = rows[0] if rows else {}
            return jsonify({"ok": True, "docCount": r.get("docCount") or 0, "chunkCount": r.get("chunkCount") or 0})
        except Exception as e:
            logger.exception("kb_stats failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/tools")
    def tools():
        return jsonify({"ok": True, **react_agent.tools_schema()})

    @app.post("/react")
    def react():
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        message = payload.get("message") or payload.get("question") or payload.get("text")
        debug = bool(payload.get("debug"))
        max_steps = payload.get("maxSteps")
        try:
            max_steps_val = int(max_steps) if max_steps is not None else int(os.getenv("REACT_MAX_STEPS", "5"))
        except ValueError:
            max_steps_val = 5

        if not isinstance(message, str) or not message.strip():
            return jsonify({"ok": False, "error": "message must be a non-empty string"}), 400

        try:
            resp = react_agent.run(message.strip(), max_steps=max_steps_val, debug=debug)
            return jsonify(resp)
        except Exception as e:
            logger.exception("react failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/health")
    def health():
        return jsonify({"ok": True})

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host=env_str("FLASK_HOST", "127.0.0.1"), port=env_int("FLASK_PORT", 5000), debug=False)
