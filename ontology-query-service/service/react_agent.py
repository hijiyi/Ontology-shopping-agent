from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from dao.mysql_read_dao import MySQLReadDao
from dao.neo4j_dao import Neo4jDao
from service.action_executor import ActionExecutor
from service.chat_service import ChatService
from service.llm_client import LLMClient, LLMConfig
from service.query_service import QueryService


@dataclass(frozen=True)
class ToolCall:
    tool: str
    arguments: dict[str, Any]
    reason: str


class ReActAgent:
    def __init__(self, neo4j_dao: Neo4jDao, query_service: QueryService, chat_service: ChatService) -> None:
        self._neo4j_dao = neo4j_dao
        self._query_service = query_service
        self._chat_service = chat_service
        self._action_executor = ActionExecutor(neo4j_dao)
        self._mysql = MySQLReadDao.from_env()
        self._sql_allowed_tables = os.getenv("SQL_ALLOWED_TABLES", "t_user,t_order,t_stage")

        self._llm_provider = os.getenv("LLM_PROVIDER", "off")
        self._llm_model = os.getenv("LLM_MODEL", "deepseek-chat")
        self._llm_api_key = os.getenv("LLM_API_KEY", "")
        self._llm_base_url = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
        try:
            self._llm_temperature = float(os.getenv("LLM_TEMPERATURE", "0.2"))
        except ValueError:
            self._llm_temperature = 0.2

        self._llm = LLMClient(
            LLMConfig(
                base_url=self._llm_base_url,
                api_key=self._llm_api_key,
                model=self._llm_model,
                temperature=self._llm_temperature,
            )
        )

    def tools_schema(self) -> dict[str, Any]:
        allowed_tables = [t.strip() for t in self._sql_allowed_tables.split(",") if t.strip()]
        return {
            "tools": [
                {
                    "name": "sql_query",
                    "description": f"Run a read-only SELECT query on MySQL. Allowed tables: {', '.join(allowed_tables)}. Always include a LIMIT <= 100.",
                    "parameters": {
                        "type": "object",
                        "required": ["sql"],
                        "properties": {"sql": {"type": "string"}},
                    },
                },
                {
                    "name": "neo4j_query",
                    "description": "Query ontology graph using safe templates. queryType must be one of entity|action|relation|instance and name is the main argument.",
                    "parameters": {
                        "type": "object",
                        "required": ["queryType", "name"],
                        "properties": {
                            "queryType": {"type": "string", "enum": ["entity", "action", "relation", "instance"]},
                            "name": {"type": "string"},
                        },
                    },
                },
                {
                    "name": "execute_action",
                    "description": "Execute a business action resolved from Ont_Action(apiPath). actionName must exist in Neo4j. params is JSON object.",
                    "parameters": {
                        "type": "object",
                        "required": ["actionName", "params"],
                        "properties": {
                            "actionName": {"type": "string"},
                            "params": {"type": "object"},
                        },
                    },
                },
                {
                    "name": "sync_neo4j",
                    "description": "Run incremental sync script (ontology-etl/incremental_sync.py) to sync MySQL changes into Neo4j Demo graph. Respects ETL_SYNC_* env.",
                    "parameters": {"type": "object", "properties": {}},
                },
            ]
        }

    def run(
        self,
        message: str,
        max_steps: int = 5,
        debug: bool = False,
        allow_execute: bool = False,
        tool_allowlist: list[str] | None = None,
    ) -> dict[str, Any]:
        question = (message or "").strip()
        if not question:
            return {"ok": False, "error": "message is empty"}

        fast = self._try_fast_path(question, debug=debug)
        if fast is not None:
            return fast

        if self._looks_like_execute(question) and not allow_execute:
            return {"ok": False, "error": "该执行意图需要走卡片确认，请先关闭 ReAct，再发送同样的指令（会返回表单卡片）"}

        if self._llm_provider.lower() in ("off", "", "none") or not self._llm_api_key:
            return {"ok": False, "error": "LLM is not enabled. Set LLM_PROVIDER and LLM_API_KEY in .env."}

        steps: list[dict[str, Any]] = []
        tool_schema = self.tools_schema()
        allowed_tables = [t.strip() for t in self._sql_allowed_tables.split(",") if t.strip()]
        schema_hint = {
            "mysql": {
                "allowedTables": allowed_tables,
                "tables": {
                    "t_user": ["id", "name", "update_time"],
                    "t_order": ["id", "user_id", "amount", "current_stage", "update_time"],
                    "t_stage": ["id", "order_id", "stage_name", "update_time"],
                },
                "businessMapping": {
                    "userName": "t_user.name",
                    "orderTime": "t_order.update_time",
                    "orderAmount": "t_order.amount",
                },
            },
            "neo4j": {
                "ontologyEntities": ["User", "Order", "Stage"],
                "demoLabels": ["Demo_User", "Demo_Order", "Demo_Stage"],
            },
        }

        system = (
            "You are a ReAct agent for an order system. You can call tools to query MySQL, query Neo4j, execute business actions, and sync Neo4j.\n"
            "You must output ONLY JSON.\n"
            "At each step, choose either:\n"
            "1) {\"type\":\"tool\",\"tool\":\"...\",\"arguments\":{...},\"reason\":\"short\"}\n"
            "2) {\"type\":\"final\",\"answer\":\"...\"}\n"
            "Rules:\n"
            "- Do not output SQL unless calling sql_query.\n"
            "- You must NOT invent table names. Use only the allowed MySQL tables from schema_hint.\n"
            "- For 'Alice recent 3 orders', use t_user.name to find user_id, then query t_order by user_id ordered by update_time.\n"
            "- For 'sum of last 3 orders amount', use SQL like:\n"
            "  SELECT SUM(x.amount) AS total_amount\n"
            "  FROM (\n"
            "    SELECT amount FROM t_order\n"
            "    WHERE user_id = (SELECT id FROM t_user WHERE name='Alice' LIMIT 1)\n"
            "    ORDER BY update_time DESC\n"
            "    LIMIT 3\n"
            "  ) x;\n"
            "- Prefer MySQL for latest transactional facts (latest order id, aggregates, time filters).\n"
            "- Prefer Neo4j for ontology semantics (entities/actions/relations) and Demo graph relation navigation.\n"
            "- If you execute an action that changes MySQL, call sync_neo4j afterwards.\n"
            "- Do not call the same tool with the same arguments twice.\n"
            "- After you get enough information to answer the user, you must return type=final.\n"
            "- Keep reason short (no chain-of-thought).\n"
        )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps({"question": question, "tools": tool_schema, "schema_hint": schema_hint}, ensure_ascii=False)},
        ]

        for _ in range(max_steps):
            obj = self._llm.extract_json(messages)
            if not isinstance(obj, dict):
                return {"ok": False, "error": "LLM did not return valid JSON", "steps": steps}

            step_type = obj.get("type")
            if step_type == "final":
                ans = obj.get("answer")
                if not isinstance(ans, str):
                    ans = json.dumps(obj, ensure_ascii=False)
                if os.getenv("ANSWER_STRIP_MARKDOWN", "1").lower() not in ("0", "false", "no"):
                    ans = ans.replace("**", "")
                return {
                    "ok": True,
                    "answer": ans,
                    "llm": {"provider": self._llm_provider, "model": self._llm_model},
                    "steps": steps if debug else [],
                }

            if step_type != "tool":
                return {"ok": False, "error": f"invalid step type: {step_type}", "steps": steps}

            tool = obj.get("tool")
            args = obj.get("arguments") if isinstance(obj.get("arguments"), dict) else {}
            reason = obj.get("reason") if isinstance(obj.get("reason"), str) else ""
            if not isinstance(tool, str) or not tool:
                return {"ok": False, "error": "missing tool", "steps": steps}
            if tool_allowlist is not None and tool not in tool_allowlist:
                return {"ok": False, "error": f"tool not allowed in this mode: {tool}", "steps": steps}

            observation = self._run_tool(tool, args)
            steps.append({"tool": tool, "arguments": args, "reason": reason, "observation": observation})
            messages.append({"role": "user", "content": json.dumps({"tool_result": {"tool": tool, "observation": observation}}, ensure_ascii=False)})

        return {"ok": False, "error": "max steps reached", "steps": steps}

    def _polish_answer(self, question: str, structured: dict[str, Any], draft_answer: str) -> str:
        if self._llm_provider.lower() in ("off", "", "none"):
            return draft_answer
        if not self._llm_api_key:
            return draft_answer

        system = (
            "你是一个业务助手。你会收到用户问题、结构化结果，以及一个草稿答案。"
            "请用中文把答案改写得更自然、更清晰，不要编造结构化结果之外的信息。"
            "不要使用 Markdown。输出纯文本。"
        )
        user = json.dumps({"question": question, "structured_result": structured, "draft_answer": draft_answer}, ensure_ascii=False)
        text = self._llm.chat_completions(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        )
        if not text:
            return draft_answer
        if os.getenv("ANSWER_STRIP_MARKDOWN", "1").lower() not in ("0", "false", "no"):
            text = text.replace("**", "")
        return text

    def _looks_like_execute(self, question: str) -> bool:
        t = (question or "").strip()
        if not t:
            return False
        lowered = t.lower()
        if any(k in t for k in ["下单", "创建订单", "新建订单", "支付", "付款", "推进", "变更", "更新"]):
            return True
        if any(k in lowered for k in ["createorder", "payorder", "advancestage"]):
            return True
        return False

    def _try_fast_path(self, question: str, debug: bool) -> dict[str, Any] | None:
        # 1) Entity list like "有哪些实体"
        if any(k in question for k in ["有哪些实体", "实体列表", "全部实体"]):
            try:
                result = self._query_service.query("entity", "__ALL__")
                data = result.data if isinstance(result.__dict__.get("data"), (list, dict)) else result.__dict__.get("data")
                names: list[str] = []
                if isinstance(data, dict) and isinstance(data.get("entities"), list):
                    for e in data.get("entities"):
                        if isinstance(e, dict) and e.get("name"):
                            names.append(str(e.get("name")))
                draft = "实体列表：" + (", ".join(names) if names else "无")
                return {"ok": True, "answer": draft, "llm": {"provider": "fast-path", "model": ""}, "steps": [] if not debug else [{"tool": "neo4j_query", "arguments": {"queryType": "entity", "name": "__ALL__"}, "reason": "fast-path: entity list", "observation": {"ok": True, "result": result.__dict__}}]}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        # 1.5) Entity fields like "Order有哪些字段"
        if any(k in question for k in ["有哪些字段", "有哪些属性", "字段有哪些", "属性有哪些", "表结构", "schema"]):
            entity_name = None
            if any(k in question for k in ["Order", "订单"]):
                entity_name = "Order"
            elif any(k in question for k in ["User", "用户"]):
                entity_name = "User"
            elif any(k in question for k in ["Stage", "阶段"]):
                entity_name = "Stage"

            if entity_name:
                try:
                    result = self._query_service.query("entity", entity_name)
                    data = result.__dict__.get("data")
                    attrs = []
                    if isinstance(data, dict) and isinstance(data.get("attributes"), list):
                        for a in data.get("attributes"):
                            if isinstance(a, dict) and a.get("name"):
                                attrs.append(str(a.get("name")))
                    answer = f"{entity_name} 字段：" + (", ".join(attrs) if attrs else "无")
                    return {
                        "ok": True,
                        "answer": answer,
                        "llm": {"provider": "fast-path", "model": ""},
                        "steps": []
                        if not debug
                        else [
                            {
                                "tool": "neo4j_query",
                                "arguments": {"queryType": "entity", "name": entity_name},
                                "reason": "fast-path: entity fields",
                                "observation": {"ok": True, "result": result.__dict__},
                            }
                        ],
                    }
                except Exception as e:
                    return {"ok": False, "error": str(e)}

        # 2) Orders by user like "Alice有哪些订单"
        m_orders = re.search(r"^(?:查询|查看|列出)?\s*(?:用户)?(?P<user>[A-Za-z0-9_\u4e00-\u9fa5]+)\s*(?:有哪些|有多少|列出|查看).*(?:订单)", question)
        if m_orders:
            user_name = m_orders.group("user")
            if user_name in ("订单", "的订单", "该订单", "该用户", "这个用户"):
                return None
            try:
                result = self._query_service.query("instance", question)
                data = result.__dict__.get("data")
                if isinstance(data, dict) and "orders" in data:
                    total = data.get("total")
                    orders = data.get("orders") or []
                    parts = []
                    for o in orders:
                        if not isinstance(o, dict) or o.get("id") is None:
                            continue
                        seg = f"#{o.get('id')}"
                        if o.get("amount") is not None:
                            seg += f" amount={o.get('amount')}"
                        if o.get("current_stage"):
                            seg += f" current_stage={o.get('current_stage')}"
                        parts.append(seg)
                    answer = f"{user_name} 的订单数：{total}。{'; '.join(parts) if parts else '无'}"
                else:
                    answer = f"{user_name} 的订单数：0。无"
                return {
                    "ok": True,
                    "answer": answer,
                    "llm": {"provider": "fast-path", "model": ""},
                    "steps": [] if not debug else [{"tool": "neo4j_query", "arguments": {"queryType": "instance", "name": question}, "reason": "fast-path: orders by user", "observation": {"ok": True, "result": result.__dict__}}],
                }
            except Exception as e:
                return {"ok": False, "error": str(e)}

        # 2.5) Order detail by user like "Alice的订单详情"
        m_detail = re.search(r"^(?:用户)?(?P<user>[A-Za-z0-9_\u4e00-\u9fa5]+?)(?:的)?\s*(?:订单详情|订单明细|订单信息)", question)
        if m_detail:
            user_name = m_detail.group("user").strip()
            if user_name in ("订单", "的订单", "该订单", "该用户", "这个用户"):
                return None
            try:
                result = self._query_service.query("instance", question)
                data = result.__dict__.get("data")
                if isinstance(data, dict) and "orders" in data:
                    total = data.get("total")
                    orders = data.get("orders") or []
                    parts = []
                    for o in orders:
                        if not isinstance(o, dict) or o.get("id") is None:
                            continue
                        seg = f"#{o.get('id')}"
                        if o.get("amount") is not None:
                            seg += f" amount={o.get('amount')}"
                        if o.get("current_stage"):
                            seg += f" current_stage={o.get('current_stage')}"
                        if isinstance(o.get("stages"), list) and o.get("stages"):
                            seg += f" stages={','.join([str(x) for x in o.get('stages') if x is not None])}"
                        parts.append(seg)
                    answer = f"{user_name} 的订单详情（{total}）：{'; '.join(parts) if parts else '无'}"
                else:
                    answer = f"{user_name} 的订单详情：无"
                return {
                    "ok": True,
                    "answer": answer,
                    "llm": {"provider": "fast-path", "model": ""},
                    "steps": [] if not debug else [{"tool": "neo4j_query", "arguments": {"queryType": "instance", "name": question}, "reason": "fast-path: order detail by user", "observation": {"ok": True, "result": result.__dict__}}],
                }
            except Exception as e:
                return {"ok": False, "error": str(e)}

        # 3) Sum of last N orders amount
        m = re.search(
            r"(?:查询|统计|计算|求)?\s*(?P<user>[A-Za-z0-9_\u4e00-\u9fa5]+)\s*最近\s*(?P<n>\d+)\s*(?:单|笔|条).*(?:金额|amount).*(?:总和|合计|总计|sum|total)",
            question,
            flags=re.IGNORECASE,
        )
        if not m:
            m_pay = re.search(
                r"(?P<user>[A-Za-z0-9_\u4e00-\u9fa5]+)\s*最近\s*(?:一单|1单).*(?:支付|付款)",
                question,
                flags=re.IGNORECASE,
            )
            if not m_pay:
                return None

            user_name = str(m_pay.group("user") or "").strip()
            if not user_name or "'" in user_name or '"' in user_name:
                return {"ok": False, "error": "invalid user name"}

            steps: list[dict[str, Any]] = []
            sql_last = (
                "SELECT id, amount, update_time "
                "FROM t_order "
                f"WHERE user_id = (SELECT id FROM t_user WHERE name='{user_name}' LIMIT 1) "
                "ORDER BY update_time DESC "
                "LIMIT 1"
            )
            obs_last = self._run_tool("sql_query", {"sql": sql_last})
            steps.append({"tool": "sql_query", "arguments": {"sql": sql_last}, "reason": "fast-path: find latest order id", "observation": obs_last})

            if not obs_last.get("ok"):
                return {"ok": False, "error": obs_last.get("error") or "sql_query failed", "steps": steps if debug else []}

            rows = obs_last.get("rows") if isinstance(obs_last.get("rows"), list) else []
            if not rows or not isinstance(rows[0], dict) or rows[0].get("id") is None:
                return {"ok": True, "answer": f"{user_name} 没有可支付的订单", "llm": {"provider": "fast-path", "model": ""}, "steps": steps if debug else []}

            order_id = int(rows[0].get("id"))

            obs_pay = self._run_tool("execute_action", {"actionName": "payOrder", "params": {"orderId": order_id}})
            steps.append({"tool": "execute_action", "arguments": {"actionName": "payOrder", "params": {"orderId": order_id}}, "reason": "fast-path: pay latest order", "observation": obs_pay})

            if not obs_pay.get("ok"):
                return {"ok": False, "error": obs_pay.get("error") or "payOrder failed", "steps": steps if debug else []}

            obs_sync = self._run_tool("sync_neo4j", {})
            steps.append({"tool": "sync_neo4j", "arguments": {}, "reason": "fast-path: sync after payment", "observation": obs_sync})

            draft = f"已支付 {user_name} 最近一单订单（订单ID: {order_id}）。数据已同步到 Neo4j。"
            answer = self._polish_answer(
                question=question,
                structured={"userName": user_name, "orderId": order_id, "pay": obs_pay, "sync": obs_sync},
                draft_answer=draft,
            )
            return {"ok": True, "answer": answer, "llm": {"provider": "fast-path", "model": self._llm_model if self._llm_api_key else ""}, "steps": steps if debug else []}

        user_name = str(m.group("user") or "").strip()
        if not user_name or "'" in user_name or '"' in user_name:
            return {"ok": False, "error": "invalid user name"}

        try:
            n = int(m.group("n"))
        except Exception:
            n = 3
        if n <= 0:
            n = 3
        if n > 100:
            n = 100

        sql = (
            "SELECT SUM(x.amount) AS total_amount "
            "FROM ( "
            f"SELECT amount FROM t_order "
            f"WHERE user_id = (SELECT id FROM t_user WHERE name='{user_name}' LIMIT 1) "
            "ORDER BY update_time DESC "
            f"LIMIT {n} "
            ") x "
            "LIMIT 1"
        )

        steps: list[dict[str, Any]] = []
        observation = self._run_tool("sql_query", {"sql": sql})
        steps.append({"tool": "sql_query", "arguments": {"sql": sql}, "reason": "fast-path: sum last N orders amount", "observation": observation})

        if not observation.get("ok"):
            return {"ok": False, "error": observation.get("error") or "sql_query failed", "steps": steps if debug else []}

        rows = observation.get("rows") if isinstance(observation.get("rows"), list) else []
        total = None
        if rows and isinstance(rows[0], dict):
            total = rows[0].get("total_amount")
        if total is None:
            draft = f"{user_name} 最近 {n} 单订单金额总和：0（未找到订单或金额为空）"
        else:
            draft = f"{user_name} 最近 {n} 单订单金额总和：{total}"

        answer = self._polish_answer(
            question=question,
            structured={"userName": user_name, "n": n, "total_amount": total},
            draft_answer=draft,
        )
        return {"ok": True, "answer": answer, "llm": {"provider": "fast-path", "model": self._llm_model if self._llm_api_key else ""}, "steps": steps if debug else []}

    def _run_tool(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool == "sql_query":
            sql = args.get("sql")
            if not isinstance(sql, str) or not sql.strip():
                return {"ok": False, "error": "sql is required"}
            try:
                res = self._mysql.select_safe(sql)
                return {"ok": True, **res}
            except Exception as e:
                allowed_tables = [t.strip() for t in self._sql_allowed_tables.split(",") if t.strip()]
                return {"ok": False, "error": str(e), "allowedTables": allowed_tables}

        if tool == "neo4j_query":
            qt = args.get("queryType")
            name = args.get("name")
            if qt not in ("entity", "action", "relation", "instance"):
                return {"ok": False, "error": "invalid queryType"}
            if not isinstance(name, str) or not name.strip():
                return {"ok": False, "error": "name is required"}
            try:
                result = self._query_service.query(qt, name.strip())
                return {"ok": True, "result": result.__dict__}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        if tool == "execute_action":
            action_name = args.get("actionName")
            params = args.get("params")
            if not isinstance(action_name, str) or not action_name.strip():
                return {"ok": False, "error": "actionName is required"}
            if not isinstance(params, dict):
                return {"ok": False, "error": "params must be an object"}
            res = self._action_executor.execute(action_name.strip(), params)
            return res

        if tool == "sync_neo4j":
            try:
                res = self._chat_service._maybe_run_etl_sync()
                return res or {"ok": False, "error": "ETL_SYNC_ENABLED is off"}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        return {"ok": False, "error": f"unknown tool: {tool}"}
