from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from dao.neo4j_dao import Neo4jDao


QueryType = Literal["entity", "action", "relation", "instance", "execute"]


@dataclass(frozen=True)
class NLQPlan:
    queryType: QueryType
    name: str
    intentReason: str
    slots: dict[str, Any]


class NLQPlanner:
    def __init__(self, dao: Neo4jDao) -> None:
        self._dao = dao

    def plan(self, question: str) -> NLQPlan:
        q = (question or "").strip()
        if not q:
            raise ValueError("question is empty")

        exec_plan = self._maybe_execute_plan(q)
        if exec_plan is not None:
            return exec_plan

        if re.search(r"订单\s*\d+.*(?:阶段|商品明细|商品列表|订单详情|订单明细|明细|详情)", q):
            slots = self._extract_instance_slots(q)
            return NLQPlan(
                queryType="instance",
                name=q,
                intentReason="matched order-instance question pattern (by orderId)",
                slots=slots,
            )

        if any(k in q for k in ["有哪些实体", "实体有哪些", "实体列表", "所有实体", "全部实体"]):
            return NLQPlan(
                queryType="entity",
                name="__ALL__",
                intentReason="matched entity list question pattern",
                slots={"mode": "list_entities"},
            )

        if any(k in q for k in ["这些实体之间有什么关系", "实体之间有什么关系", "实体之间的关系", "实体关系概览", "本体关系概览"]):
            return NLQPlan(
                queryType="relation",
                name="__ALL__",
                intentReason="matched relation overview question pattern",
                slots={"mode": "list_entity_relations"},
            )

        known_entities = self._load_entity_names()
        known_actions = self._load_action_names()
        entity_alias = self._entity_alias_map()

        entities = self._extract_entities(q, known_entities, entity_alias)
        intent = self._infer_intent(q, entities)

        if intent == "instance":
            slots = self._extract_instance_slots(q)
            return NLQPlan(
                queryType="instance",
                name=q,
                intentReason="matched instance-data question pattern",
                slots=slots,
            )

        if intent == "entity":
            entity = entities[0] if entities else self._guess_entity_from_text(q, known_entities, entity_alias)
            return NLQPlan(
                queryType="entity",
                name=entity,
                intentReason="matched entity/attribute question pattern",
                slots={"entity": entity, "entities": entities},
            )

        if intent == "action":
            entity = entities[0] if entities else ""
            if entity:
                return NLQPlan(
                    queryType="action",
                    name=entity,
                    intentReason="matched action question pattern; treat as action-by-entity",
                    slots={"entity": entity, "entities": entities},
                )
            kw = self._extract_action_keyword(q, known_actions)
            return NLQPlan(
                queryType="action",
                name=kw,
                intentReason="matched action question pattern; treat as action-by-keyword",
                slots={"keyword": kw, "entities": entities},
            )

        if intent == "relation":
            if len(entities) >= 2:
                a, b = entities[0], entities[1]
                return NLQPlan(
                    queryType="relation",
                    name=f"{a} {b}",
                    intentReason="matched relation question pattern and found two entities",
                    slots={"from": a, "to": b, "entities": entities},
                )
            if len(entities) == 1:
                e = entities[0]
                return NLQPlan(
                    queryType="relation",
                    name=e,
                    intentReason="matched relation question pattern and found one entity",
                    slots={"entity": e, "entities": entities},
                )
            return NLQPlan(
                queryType="relation",
                name=q,
                intentReason="matched relation question pattern but cannot extract entity reliably",
                slots={"entities": entities},
            )

        raise ValueError("cannot infer intent")

    def _maybe_execute_plan(self, text: str) -> NLQPlan | None:
        t = text.strip()
        if not t:
            return None

        if re.search(r"订单\s*\d+", t) and any(k in t for k in ["商品明细", "订单详情", "订单明细", "商品列表"]):
            return None

        if any(k in t for k in ["改价", "修改价格", "更新价格"]):
            m_pid = re.search(r"(?:商品|product)\s*#?\s*(?P<pid>\d+)", t, flags=re.IGNORECASE)
            m_price = re.search(r"(?:新价格|改价|价格)\s*[:=]?\s*(?P<p>\d+(?:\.\d+)?)", t)
            m_op = re.search(r"(?:管理员|operator|操作人|用户名)\s*[:=]\s*(?P<op>[A-Za-z0-9_\u4e00-\u9fa5]+)", t, flags=re.IGNORECASE)
            params3: dict[str, Any] = {}
            if m_op:
                params3["operatorName"] = m_op.group("op")
            if m_pid:
                params3["productId"] = int(m_pid.group("pid"))
            if m_price:
                try:
                    params3["newPrice"] = float(m_price.group("p"))
                except ValueError:
                    pass
            return NLQPlan(
                queryType="execute",
                name="updateProductPrice",
                intentReason="matched update product price execute intent",
                slots={"actionName": "updateProductPrice", "params": params3},
            )

        if any(k in t for k in ["上下架", "上架", "下架", "停售", "缺货", "更新状态", "设置状态"]):
            m_pid = re.search(r"(?:商品|product)\s*#?\s*(?P<pid>\d+)", t, flags=re.IGNORECASE)
            m_op = re.search(r"(?:管理员|operator|操作人|用户名)\s*[:=]\s*(?P<op>[A-Za-z0-9_\u4e00-\u9fa5]+)", t, flags=re.IGNORECASE)
            status_val = None
            if any(k in t for k in ["上架", "启用", "可售"]):
                status_val = "ACTIVE"
            elif ("下架" in t and "上下架" not in t) or any(k in t for k in ["停用"]):
                status_val = "INACTIVE"
            elif "缺货" in t:
                status_val = "OUT_OF_STOCK"
            elif "停售" in t:
                status_val = "DISCONTINUED"

            params4: dict[str, Any] = {}
            if m_op:
                params4["operatorName"] = m_op.group("op")
            if m_pid:
                params4["productId"] = int(m_pid.group("pid"))
            if status_val:
                params4["status"] = status_val
            return NLQPlan(
                queryType="execute",
                name="setProductStatus",
                intentReason="matched set product status execute intent",
                slots={"actionName": "setProductStatus", "params": params4},
            )

        want_product_detail = any(k in t for k in ["查看商品详情", "查看商品信息"])
        if not want_product_detail:
            if "详情" in t and re.search(r"(?:商品|product)\s*#?\s*\d+", t, flags=re.IGNORECASE):
                want_product_detail = True

        if want_product_detail:
            m = re.search(r"(?:商品|product)\s*#?\s*(?P<pid>\d+)", t, flags=re.IGNORECASE)
            params: dict[str, Any] = {}
            if m:
                params["productId"] = int(m.group("pid"))
            return NLQPlan(
                queryType="execute",
                name="getProductDetail",
                intentReason="matched get product detail execute intent",
                slots={"actionName": "getProductDetail", "params": params},
            )

        if any(k in t for k in ["补库存", "补货", "增加库存", "入库"]):
            m_pid = re.search(r"(?:商品|product)\s*#?\s*(?P<pid>\d+)", t, flags=re.IGNORECASE)
            m_qty = re.search(r"(?:补库存|补货|增加库存|入库)\s*(?P<qty>\d+)", t)
            m_op = re.search(r"(?:管理员|operator|操作人|用户名)\s*[:=]?\s*(?P<op>[A-Za-z0-9_\u4e00-\u9fa5]+)", t, flags=re.IGNORECASE)
            params2: dict[str, Any] = {}
            if m_op:
                params2["operatorName"] = m_op.group("op")
            if m_pid:
                params2["productId"] = int(m_pid.group("pid"))
            if m_qty:
                params2["addQty"] = int(m_qty.group("qty"))
            return NLQPlan(
                queryType="execute",
                name="restockProduct",
                intentReason="matched restock product execute intent",
                slots={"actionName": "restockProduct", "params": params2},
            )

        m1 = re.search(r"(?:给|为)?(?P<user>[A-Za-z0-9_\u4e00-\u9fa5]+)\s*(?:下单|创建订单|新建订单)\s*(?P<amount>\d+(?:\.\d+)?)", t)
        if m1:
            return NLQPlan(
                queryType="execute",
                name="createOrder",
                intentReason="matched create order execute pattern",
                slots={
                    "actionName": "createOrder",
                    "params": {"userName": m1.group("user"), "amount": float(m1.group("amount"))},
                },
            )

        if any(k in t for k in ["下单", "创建订单", "新建订单"]):
            user = None
            amount = None

            mu = re.search(r"(?:给|为)\s*(?P<user>[A-Za-z0-9_\u4e00-\u9fa5]+)", t)
            if mu:
                user = mu.group("user")
            else:
                mu2 = re.search(r"(?:用户|userName)\s*[:=]\s*(?P<user>[A-Za-z0-9_\u4e00-\u9fa5]+)", t, flags=re.IGNORECASE)
                if mu2:
                    user = mu2.group("user")

            if user in ("创建订单", "下单", "新建订单", "订单"):
                user = None

            ma = re.search(r"(?:金额|amount)\s*[:=]?\s*(?P<amount>\d+(?:\.\d+)?)", t, flags=re.IGNORECASE)
            if ma:
                amount = float(ma.group("amount"))
            else:
                ma2 = re.search(r"(?P<amount>\d+(?:\.\d+)?)", t)
                if ma2:
                    amount = float(ma2.group("amount"))

            params: dict[str, Any] = {}
            if user:
                params["userName"] = user
            if amount is not None:
                params["amount"] = amount
            return NLQPlan(
                queryType="execute",
                name="createOrder",
                intentReason="matched create order execute intent",
                slots={"actionName": "createOrder", "params": params},
            )

        m2 = re.search(r"(?:支付|付款)(?:订单)?\s*(?P<order_id>\d+)", t)
        if m2:
            return NLQPlan(
                queryType="execute",
                name="payOrder",
                intentReason="matched pay order execute pattern",
                slots={"actionName": "payOrder", "params": {"orderId": int(m2.group("order_id"))}},
            )

        if any(k in t for k in ["支付", "付款"]):
            return NLQPlan(
                queryType="execute",
                name="payOrder",
                intentReason="matched pay order execute intent (missing orderId)",
                slots={"actionName": "payOrder", "params": {}},
            )

        m3 = re.search(r"(?:推进|更新|变更).*(?:订单)?\s*(?P<order_id>\d+).*(?:到|为)\s*(?P<stage>[A-Za-z0-9_]+)", t)
        if m3:
            return NLQPlan(
                queryType="execute",
                name="advanceStage",
                intentReason="matched advance stage execute pattern",
                slots={"actionName": "advanceStage", "params": {"orderId": int(m3.group("order_id")), "stageName": m3.group("stage")}},
            )

        if any(k in t for k in ["推进", "变更阶段", "更新阶段"]):
            return NLQPlan(
                queryType="execute",
                name="advanceStage",
                intentReason="matched advance stage execute intent (missing orderId/stageName)",
                slots={"actionName": "advanceStage", "params": {}},
            )

        return None

    def _load_entity_names(self) -> list[str]:
        rows = self._dao.run_read("MATCH (e:Ont_Entity) RETURN DISTINCT e.name AS name ORDER BY name")
        names = [r.get("name") for r in rows if r.get("name")]
        return [str(x) for x in names]

    def _load_action_names(self) -> list[str]:
        rows = self._dao.run_read("MATCH (a:Ont_Action) RETURN DISTINCT a.name AS name ORDER BY name")
        names = [r.get("name") for r in rows if r.get("name")]
        return [str(x) for x in names]

    def _entity_alias_map(self) -> dict[str, str]:
        return {
            "用户": "User",
            "订单": "Order",
            "阶段": "Stage",
            "用户表": "User",
            "订单表": "Order",
            "阶段表": "Stage",
        }

    def _extract_entities(self, text: str, known_entities: list[str], alias: dict[str, str]) -> list[str]:
        found: list[str] = []

        lowered = text.lower()
        for e in known_entities:
            if e and e.lower() in lowered:
                found.append(e)

        for k, v in alias.items():
            if k in text and v not in found:
                found.append(v)

        order = {name: i for i, name in enumerate(known_entities)}
        return sorted(set(found), key=lambda x: order.get(x, 10**9))

    def _infer_intent(self, text: str, entities: list[str]) -> QueryType:
        t = text.lower()

        if self._looks_like_instance_question(text):
            return "instance"

        if any(k in t for k in ["字段", "属性", "有哪些字段", "有哪些属性", "包含哪些字段", "表结构", "schema"]):
            return "entity"
        if any(k in t for k in ["动作", "action", "接口", "api", "调用", "能做什么", "可以做什么"]):
            return "action"
        if any(k in t for k in ["关系", "关联", "引用", "外键", "属于", "谁的", "什么关系", "如何关联"]):
            return "relation"

        if len(entities) >= 2:
            return "relation"
        if len(entities) == 1:
            return "entity"
        return "action"

    def _looks_like_instance_question(self, text: str) -> bool:
        t = text.strip()
        if not t:
            return False
        if any(k in t for k in ["有哪些订单", "有多少订单", "列出订单", "查看订单", "订单列表", "的订单"]):
            return True
        if any(k in t for k in ["有哪些阶段", "有多少阶段", "列出阶段", "查看阶段", "阶段列表", "的阶段"]):
            return True
        if re.search(r"订单\s*\d+.*(有哪些|有多少|列出|查看).*(阶段)", t):
            return True
        return False

    def _extract_instance_slots(self, text: str) -> dict[str, Any]:
        t = text.strip()
        m_latest_stage = re.search(
            r"(?:用户)?(?P<user>[A-Za-z0-9_\u4e00-\u9fa5]+)\s*(?:最近|最新)\s*(?:一单|1单)?(?:订单)?\s*(?:阶段|状态).*(?:到哪了|是什么|是多少|如何|当前)?",
            t,
        )
        if m_latest_stage:
            return {"task": "latest_order_stage_by_user_name", "userName": m_latest_stage.group("user"), "orderHint": "latest"}

        m1 = re.search(r"(?:用户)?(?P<user>[A-Za-z0-9_\u4e00-\u9fa5]+)\s*(?:有哪些|有多少|列出|查看).*(?:订单)", t)
        if m1:
            return {"task": "orders_by_user_name", "userName": m1.group("user")}

        m2 = re.search(r"(?:用户)?(?P<user>[A-Za-z0-9_\u4e00-\u9fa5]+)的订单", t)
        if m2:
            return {"task": "orders_by_user_name", "userName": m2.group("user")}

        m3 = re.search(r"订单\s*(?P<order_id>\d+)\s*(?:有哪些|有多少|列出|查看).*(?:阶段)", t)
        if m3:
            return {"task": "stages_by_order_id", "orderId": int(m3.group("order_id"))}

        m4 = re.search(r"订单\s*(?P<order_id>\d+)\s*的阶段", t)
        if m4:
            return {"task": "stages_by_order_id", "orderId": int(m4.group("order_id"))}

        m5 = re.search(r"订单\s*(?P<order_id>\d+).*(?:商品明细|商品列表|订单详情|订单明细|明细|详情)", t)
        if m5:
            return {"orderId": int(m5.group("order_id"))}

        return {"task": "unknown", "raw": t}

    def _guess_entity_from_text(self, text: str, known_entities: list[str], alias: dict[str, str]) -> str:
        extracted = self._extract_entities(text, known_entities, alias)
        if extracted:
            return extracted[0]
        return "Order"

    def _extract_action_keyword(self, text: str, known_actions: list[str]) -> str:
        lowered = text.lower()
        for a in known_actions:
            if a and a.lower() in lowered:
                return a

        m = re.search(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text)
        if m:
            return m.group(0)
        return text.strip()
