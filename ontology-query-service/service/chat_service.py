from __future__ import annotations

import os
import json
import math
import time
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from service.nlq_planner import NLQPlanner
from service.query_service import QueryService
from service.llm_client import LLMClient, LLMConfig
from service.llm_planner import LLMPlanner
from dao.neo4j_dao import Neo4jDao
from service.action_executor import ActionExecutor


@dataclass(frozen=True)
class ChatResponse:
    answer: str
    plan: dict[str, Any]
    result: dict[str, Any]


class ChatService:
    def __init__(self, planner: NLQPlanner, query_service: QueryService, dao: Neo4jDao) -> None:
        self._planner = planner
        self._query_service = query_service
        self._dao = dao
        self._llm_provider = os.getenv("LLM_PROVIDER", "off")
        self._llm_model = os.getenv("LLM_MODEL", "deepseek-chat")
        self._llm_api_key = os.getenv("LLM_API_KEY", "")
        self._llm_base_url = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
        try:
            self._llm_temperature = float(os.getenv("LLM_TEMPERATURE", "0.2"))
        except ValueError:
            self._llm_temperature = 0.2

        self._llm_client = LLMClient(
            LLMConfig(
                base_url=self._llm_base_url,
                api_key=self._llm_api_key,
                model=self._llm_model,
                temperature=self._llm_temperature,
            )
        )
        self._llm_planner = LLMPlanner(self._llm_client)
        self._action_executor = ActionExecutor(dao)
        self._repo_root = Path(__file__).resolve().parents[2]
        self._kb_vec_cache: dict[str, Any] | None = None

    def plan_only(self, question: str) -> tuple[Any, bool]:
        llm_used_for_plan = False
        rule_plan = self._planner.plan(question)
        plan = None
        if getattr(rule_plan, "name", None) == "__ALL__":
            plan = rule_plan
        elif getattr(rule_plan, "queryType", None) == "execute":
            if self._execute_params_complete(rule_plan):
                plan = rule_plan
            elif self._llm_provider.lower() not in ("off", "", "none") and self._llm_api_key:
                llm_plan = self._llm_planner.plan(question)
                if llm_plan is not None:
                    llm_used_for_plan = True
                    plan = llm_plan
                else:
                    plan = rule_plan
            else:
                plan = rule_plan
        elif self._llm_provider.lower() not in ("off", "", "none") and self._llm_api_key:
            llm_plan = self._llm_planner.plan(question)
            if llm_plan is not None:
                if getattr(llm_plan, "queryType", None) == "execute" and self._should_block_execute(question):
                    llm_plan = None
                if llm_plan is not None:
                    llm_used_for_plan = True
                    plan = llm_plan
        if plan is None:
            plan = rule_plan
        return plan, llm_used_for_plan

    def _should_block_execute(self, question: str) -> bool:
        q = (question or "").strip()
        if not q:
            return True
        if any(k in q for k in ["从知识库", "知识库", "处理建议", "排查", "解释", "说明", "是什么原因", "为什么", "怎么做"]):
            if not any(k in q for k in ["下单", "创建订单", "新建订单", "支付", "付款", "推进", "变更", "更新"]):
                return True
        return False

    def _execute_params_complete(self, plan: Any) -> bool:
        if getattr(plan, "queryType", None) != "execute":
            return True
        slots = getattr(plan, "slots", None)
        if not isinstance(slots, dict):
            return False
        action_name = slots.get("actionName")
        params = slots.get("params")
        if not isinstance(action_name, str) or not action_name.strip():
            return False
        if not isinstance(params, dict):
            return False

        if action_name == "createOrder":
            return bool(params.get("userName")) and params.get("amount") is not None
        if action_name == "payOrder":
            return params.get("orderId") is not None
        if action_name == "advanceStage":
            return params.get("orderId") is not None and bool(params.get("stageName"))
        return False

    def execute_action(self, action_name: str, params: dict[str, Any], question: str, llm_used_for_plan: bool, debug: bool) -> dict[str, Any]:
        if not action_name:
            exec_result = {"ok": False, "error": "missing actionName"}
        else:
            exec_result = self._action_executor.execute(action_name, params)

        sync_result = None
        if isinstance(exec_result, dict) and exec_result.get("ok") is True:
            sync_result = self._maybe_run_etl_sync()

        plan_obj = {"queryType": "execute", "name": action_name, "intentReason": "execute action", "slots": {"actionName": action_name, "params": params}}
        llm_answer = self._answer_with_llm(
            question=question,
            plan=plan_obj,
            structured={"execution": exec_result, "sync": sync_result},
        )
        answer = llm_answer or json.dumps({"execution": exec_result, "sync": sync_result}, ensure_ascii=False)
        answer = self._strip_markdown(answer)

        resp: dict[str, Any] = {
            "answer": answer,
            "plan": plan_obj,
            "result": {"execution": exec_result, "sync": sync_result},
            "llm": {
                "provider": self._llm_provider,
                "model": self._llm_model,
                "usedForPlan": llm_used_for_plan,
                "usedForAnswer": llm_answer is not None,
            },
        }
        if debug:
            resp["debug"] = {"bizBaseUrl": os.getenv("BIZ_BASE_URL", "http://127.0.0.1:6000")}
        return resp

    def chat(self, question: str, debug: bool = False) -> dict[str, Any]:
        plan, llm_used_for_plan = self.plan_only(question)

        if getattr(plan, "queryType", None) == "execute":
            action_name = ""
            params: dict[str, Any] = {}
            if isinstance(getattr(plan, "slots", None), dict):
                action_name = str(plan.slots.get("actionName") or "").strip()
                params = plan.slots.get("params") if isinstance(plan.slots.get("params"), dict) else {}
            return self.execute_action(action_name, params, question, llm_used_for_plan, debug)

        debug_obj = None
        if plan.queryType == "instance" and isinstance(plan.slots, dict) and isinstance(plan.slots.get("task"), str):
            debug_obj = self._query_service.query_instance_by_task_with_debug(plan.slots.get("task"), plan.slots, question)
        else:
            debug_obj = self._query_service.query_with_debug(plan.queryType, plan.name)

        structured = debug_obj["result"]
        kb_hits = self._kb_search(question)
        if kb_hits:
            if isinstance(structured, dict):
                structured = dict(structured)
                structured["kb_hits"] = kb_hits
        llm_used_for_answer = False
        answer = self._answer_with_llm(question=question, plan=plan.__dict__, structured=structured)
        if answer is None:
            answer = self._answer_fallback(question=question, plan=plan.__dict__, structured=structured)
        else:
            llm_used_for_answer = True
        answer = self._strip_markdown(answer)

        resp: dict[str, Any] = {
            "answer": answer,
            "plan": plan.__dict__,
            "result": structured,
            "llm": {
                "provider": self._llm_provider,
                "model": self._llm_model,
                "usedForPlan": llm_used_for_plan,
                "usedForAnswer": llm_used_for_answer,
            },
        }
        if debug:
            resp["debug"] = {"cypher": debug_obj.get("cypher"), "params": debug_obj.get("params"), "mode": debug_obj.get("mode")}
        return resp

    def _kb_search(self, query: str) -> list[dict[str, Any]]:
        enabled = os.getenv("KB_ENABLED", "1").lower() in ("1", "true", "yes")
        if not enabled:
            return []
        q = (query or "").strip()
        if not q:
            return []
        use_vector = os.getenv("KB_VECTOR_ENABLED", "0").lower() in ("1", "true", "yes")
        try:
            topk = int(os.getenv("KB_TOPK", "3"))
        except ValueError:
            topk = 3
        if topk <= 0:
            return []
        if topk > 8:
            topk = 8

        if use_vector:
            hits = self._kb_vector_search(q, topk=topk)
            if hits:
                return hits

        try:
            rows = self._dao.run_read(
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
                {"q": q, "limit": topk},
            )
            hits: list[dict[str, Any]] = []
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
            return hits
        except Exception:
            try:
                rows = self._dao.run_read(
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
                    {"q": q, "limit": topk},
                )
                hits: list[dict[str, Any]] = []
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
                return hits
            except Exception:
                return []

    def _kb_vector_search(self, query: str, topk: int) -> list[dict[str, Any]]:
        cache = self._kb_vector_refresh_if_needed()
        if not cache:
            return []
        docs = cache.get("docs") if isinstance(cache.get("docs"), list) else []
        if not docs:
            return []
        idf = cache.get("idf") if isinstance(cache.get("idf"), dict) else {}

        q_tokens = self._kb_tokenize(query)
        q_vec, q_norm = self._kb_tfidf_vector(q_tokens, idf)
        if q_norm <= 1e-12:
            return []

        scored: list[tuple[float, dict[str, Any]]] = []
        for d in docs:
            v = d.get("vec")
            norm = d.get("norm")
            if not isinstance(v, dict) or not isinstance(norm, float) or norm <= 1e-12:
                continue
            dot = 0.0
            for k, w in q_vec.items():
                vw = v.get(k)
                if isinstance(vw, float):
                    dot += w * vw
            score = dot / (q_norm * norm)
            if score > 0:
                scored.append((score, d))

        scored.sort(key=lambda x: x[0], reverse=True)
        hits: list[dict[str, Any]] = []
        for s, d in scored[:topk]:
            hits.append(
                {
                    "docId": d.get("docId"),
                    "docTitle": d.get("docTitle"),
                    "docSource": d.get("docSource"),
                    "chunkId": d.get("chunkId"),
                    "idx": d.get("idx"),
                    "score": s,
                    "text": d.get("text"),
                }
            )
        return hits

    def _kb_vector_refresh_if_needed(self) -> dict[str, Any] | None:
        try:
            rows = self._dao.run_read("MATCH (c:Ont_Chunk) RETURN count(c) AS chunkCount, max(c.updatedAt) AS maxUpdatedAt")
        except Exception:
            return None
        r = rows[0] if rows and isinstance(rows[0], dict) else {}
        chunk_count = int(r.get("chunkCount") or 0)
        max_updated = r.get("maxUpdatedAt")
        cache = self._kb_vec_cache
        if (
            isinstance(cache, dict)
            and cache.get("chunkCount") == chunk_count
            and cache.get("maxUpdatedAt") == max_updated
            and cache.get("builtAt") is not None
        ):
            return cache

        docs = self._kb_load_chunks_for_vector()
        if not docs:
            self._kb_vec_cache = {"chunkCount": chunk_count, "maxUpdatedAt": max_updated, "builtAt": time.time(), "docs": [], "idf": {}}
            return self._kb_vec_cache

        token_sets: list[set[str]] = []
        term_counts: list[dict[str, int]] = []
        for d in docs:
            tokens = self._kb_tokenize(str(d.get("text") or ""))
            counts: dict[str, int] = {}
            for t in tokens:
                counts[t] = counts.get(t, 0) + 1
            term_counts.append(counts)
            token_sets.append(set(counts.keys()))

        df: dict[str, int] = {}
        for s in token_sets:
            for t in s:
                df[t] = df.get(t, 0) + 1
        n_docs = max(1, len(docs))
        idf: dict[str, float] = {}
        for t, c in df.items():
            idf[t] = math.log((n_docs + 1.0) / (float(c) + 1.0)) + 1.0

        for d, counts in zip(docs, term_counts):
            total = float(sum(counts.values())) if counts else 0.0
            vec: dict[str, float] = {}
            if total > 0:
                for t, c in counts.items():
                    w = (float(c) / total) * float(idf.get(t, 0.0))
                    if w > 0:
                        vec[t] = w
            norm = math.sqrt(sum(w * w for w in vec.values())) if vec else 0.0
            d["vec"] = vec
            d["norm"] = float(norm)

        self._kb_vec_cache = {"chunkCount": chunk_count, "maxUpdatedAt": max_updated, "builtAt": time.time(), "docs": docs, "idf": idf}
        return self._kb_vec_cache

    def _kb_load_chunks_for_vector(self) -> list[dict[str, Any]]:
        try:
            limit = int(os.getenv("KB_VECTOR_MAX_CHUNKS", "2000"))
        except ValueError:
            limit = 2000
        if limit <= 0:
            return []
        if limit > 10000:
            limit = 10000
        try:
            rows = self._dao.run_read(
                """
                MATCH (c:Ont_Chunk)
                OPTIONAL MATCH (d:Ont_Doc {docId: c.docId})
                RETURN
                  c.chunkId AS chunkId,
                  c.docId AS docId,
                  c.idx AS idx,
                  c.text AS text,
                  d.title AS docTitle,
                  d.source AS docSource,
                  c.updatedAt AS updatedAt
                ORDER BY c.updatedAt DESC, c.chunkId DESC
                LIMIT $limit
                """,
                {"limit": limit},
            )
            docs: list[dict[str, Any]] = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                docs.append(
                    {
                        "docId": r.get("docId"),
                        "docTitle": r.get("docTitle"),
                        "docSource": r.get("docSource"),
                        "chunkId": r.get("chunkId"),
                        "idx": r.get("idx"),
                        "text": r.get("text"),
                        "updatedAt": r.get("updatedAt"),
                    }
                )
            return docs
        except Exception:
            return []

    def _kb_tokenize(self, text: str) -> list[str]:
        t = (text or "").strip()
        if not t:
            return []

        tokens: list[str] = []
        buf: list[str] = []
        cjk: list[str] = []

        def flush_buf() -> None:
            nonlocal buf
            if buf:
                w = "".join(buf).lower()
                if w:
                    tokens.append(w)
                buf = []

        def flush_cjk() -> None:
            nonlocal cjk
            if not cjk:
                return
            tokens.extend(cjk)
            if len(cjk) >= 2:
                for i in range(len(cjk) - 1):
                    tokens.append(cjk[i] + cjk[i + 1])
            cjk = []

        for ch in t:
            o = ord(ch)
            is_ascii_word = (48 <= o <= 57) or (65 <= o <= 90) or (97 <= o <= 122) or ch in ("_", "-")
            is_cjk = 0x4E00 <= o <= 0x9FFF
            if is_ascii_word:
                flush_cjk()
                buf.append(ch)
                continue
            if is_cjk:
                flush_buf()
                cjk.append(ch)
                continue
            flush_buf()
            flush_cjk()

        flush_buf()
        flush_cjk()
        return tokens

    def _kb_tfidf_vector(self, tokens: list[str], idf: dict[str, float]) -> tuple[dict[str, float], float]:
        counts: dict[str, int] = {}
        for t in tokens:
            counts[t] = counts.get(t, 0) + 1
        total = float(sum(counts.values())) if counts else 0.0
        vec: dict[str, float] = {}
        if total > 0:
            for t, c in counts.items():
                w = (float(c) / total) * float(idf.get(t, 0.0))
                if w > 0:
                    vec[t] = w
        norm = math.sqrt(sum(w * w for w in vec.values())) if vec else 0.0
        return vec, float(norm)

    def _answer_with_llm(self, question: str, plan: dict[str, Any], structured: dict[str, Any]) -> str | None:
        if self._llm_provider.lower() in ("off", "", "none"):
            return None
        if not self._llm_api_key:
            return None

        system = (
            "你是一个 Ontology/知识图谱问答助手。你会收到用户问题、系统识别的意图与槽位、以及 Neo4j 查询得到的结构化 JSON 结果。"
            "结构化结果中可能包含 kb_hits（非结构化知识库命中片段）。请优先使用这些片段作为依据回答。"
            "请基于 JSON 结果用中文回答，按用户问题要求的粒度输出。不要编造不存在的内容。"
            "不要使用 Markdown（例如 **粗体**、代码块）。输出纯文本。"
            "输出建议结构："
            "1) 直接结论；"
            "2) 关键数据点（从 structured_result 中摘取 3-8 个要点）；"
            "3) 下一步可以问什么（给 2-3 个短提示）。"
        )
        user = json.dumps({"question": question, "plan": plan, "structured_result": structured}, ensure_ascii=False)

        return self._llm_client.chat_completions(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        )

    def _answer_fallback(self, question: str, plan: dict[str, Any], structured: dict[str, Any]) -> str:
        qt = plan.get("queryType")
        data = (structured.get("data") or {}) if isinstance(structured, dict) else {}

        if qt == "entity":
            if isinstance(data, dict) and data.get("entities") is not None:
                entities = data.get("entities") or []
                names = [e.get("name") for e in entities if isinstance(e, dict) and e.get("name")]
                lines = ["实体列表："]
                for e in entities[:50]:
                    if isinstance(e, dict) and e.get("name"):
                        table = e.get("table") or ""
                        lines.append(f"- {e.get('name')}" + (f"（{table}）" if table else ""))
                if not entities:
                    return "未找到任何实体（Ont_Entity）。请先运行一次 Ontology 结构同步（schema-only）。"
                hint = "你可以继续问：Order有哪些字段？User能做什么？User 和 Order 什么关系？"
                return "\n".join(lines) + "\n" + hint
            if not data.get("found"):
                return f"未找到实体：{data.get('entity') or plan.get('name')}"
            attrs = data.get("attributes") or []
            names = [a.get("name") for a in attrs if isinstance(a, dict) and a.get("name")]
            entity = data.get("entity") or plan.get("name") or ""
            table = data.get("table") or ""
            lines = [f"实体：{entity}" + (f"（{table}）" if table else "")]
            lines.append(f"属性：{', '.join(names) if names else '无'}")
            lines.append("你可以继续问：该实体能做什么？它和其他实体有什么关系？")
            return "\n".join(lines)

        if qt == "action":
            if isinstance(data, dict) and "actions" in data:
                actions = data.get("actions") or []
                parts = []
                for a in actions:
                    if isinstance(a, dict) and a.get("name"):
                        parts.append(f"{a.get('name')} ({a.get('apiPath') or ''})".strip())
                return f"{data.get('entity') or plan.get('name')} 可执行动作：{'; '.join(parts) if parts else '无'}"
            if isinstance(data, list):
                parts = []
                for r in data:
                    if isinstance(r, dict) and r.get("action"):
                        parts.append(f"{r.get('action')} ({r.get('apiPath') or ''})".strip())
                return f"匹配到动作：{'; '.join(parts) if parts else '无'}"
            return "未查询到动作信息"

        if qt == "relation":
            if isinstance(data, dict) and "relations" in data:
                rels = data.get("relations") or []
                parts = []
                for r in rels:
                    if isinstance(r, dict) and r.get("relType") and r.get("to"):
                        parts.append(f"{r.get('relType')} -> {r.get('to')}")
                return f"{data.get('entity') or plan.get('name')} 的关系：{'; '.join(parts) if parts else '无'}"
            if isinstance(data, list):
                parts = []
                for r in data:
                    if isinstance(r, dict) and r.get("relType"):
                        parts.append(str(r.get("relType")))
                return f"关系类型：{', '.join(sorted(set(parts))) if parts else '无'}"
            return "未查询到关系信息"

        if qt == "instance":
            if (
                isinstance(data, dict)
                and data.get("orderId") is not None
                and data.get("current_stage") is not None
                and "orders" not in data
                and "stages" not in data
            ):
                return f"最近一单订单ID：{data.get('orderId')}，当前阶段：{data.get('current_stage')}"
            if isinstance(data, dict) and "orders" in data:
                user = data.get("userName") or ""
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
                suffix = f"（展示前 {min(len(parts), 50)} 条）" if parts else ""
                return f"{user} 的订单数：{total}。{'; '.join(parts) if parts else '无'}{suffix}"
            if isinstance(data, dict) and "stages" in data:
                order_id = data.get("orderId")
                total = data.get("total")
                stages = data.get("stages") or []
                names = [str(s.get('stage_name')) for s in stages if isinstance(s, dict) and s.get("stage_name")]
                suffix = f"（展示前 {min(len(names), 50)} 条）" if names else ""
                return f"订单 {order_id} 的阶段数：{total}。阶段：{', '.join(names) if names else '无'}{suffix}"
            if isinstance(data, dict) and data.get("reason"):
                return f"暂不支持该类型问题：{data.get('reason')}"
            return "未查询到实例数据"

        return question

    def _strip_markdown(self, text: str) -> str:
        if not isinstance(text, str):
            return text
        if os.getenv("ANSWER_STRIP_MARKDOWN", "1").lower() in ("0", "false", "no"):
            return text
        return text.replace("**", "")

    def _maybe_run_etl_sync(self) -> dict[str, Any] | None:
        enabled = os.getenv("ETL_SYNC_ENABLED", "0").lower() in ("1", "true", "yes")
        if not enabled:
            return None

        etl_dir = os.getenv("ETL_DIR")
        if etl_dir:
            etl_path = Path(etl_dir)
        else:
            etl_path = self._repo_root / "ontology-etl"

        script = os.getenv("ETL_SYNC_SCRIPT")
        if script:
            script_path = Path(script)
        else:
            script_path = etl_path / "incremental_sync.py"

        if not script_path.exists():
            return {"ok": False, "error": f"etl sync script not found: {script_path}"}

        timeout_s = 60
        try:
            timeout_s = int(os.getenv("ETL_SYNC_TIMEOUT", "60"))
        except ValueError:
            timeout_s = 60

        cmd = [sys.executable, str(script_path)]
        if os.getenv("ETL_SYNC_FULL", "0").lower() in ("1", "true", "yes"):
            cmd.append("--full")

        try:
            cp = subprocess.run(
                cmd,
                cwd=str(etl_path),
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            out = (cp.stdout or "") + ("\n" + cp.stderr if cp.stderr else "")
            out = out.strip()
            if len(out) > 3000:
                out = out[:3000] + "\n...(truncated)"
            return {"ok": cp.returncode == 0, "returncode": cp.returncode, "output": out}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"etl sync timeout after {timeout_s}s"}
        except OSError as e:
            return {"ok": False, "error": f"etl sync failed: {e}"}
