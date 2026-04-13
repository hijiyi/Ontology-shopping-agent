from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from service.llm_client import LLMClient


QueryType = Literal["entity", "action", "relation", "instance", "execute"]


@dataclass(frozen=True)
class LLMPlan:
    queryType: QueryType
    name: str
    intentReason: str
    slots: dict[str, Any]


class LLMPlanner:
    def __init__(self, client: LLMClient) -> None:
        self._client = client

    def plan(self, question: str) -> LLMPlan | None:
        q = (question or "").strip()
        if not q:
            return None

        system = (
            "你是一个意图识别与参数抽取器。你只输出 JSON，不要输出任何额外文字。\n"
            "目标：把用户的自然语言问题映射为一个可执行的 Neo4j 查询计划。\n"
            "允许的 queryType 只能是：entity | action | relation | instance | execute\n"
            "输出 JSON 格式：\n"
            "{\n"
            '  "queryType": "entity|action|relation|instance|execute",\n'
            '  "name": "用于后端查询的主参数",\n'
            '  "intentReason": "一句话说明为什么这么判定",\n'
            '  "slots": { ... }\n'
            "}\n"
            "instance 类问题必须输出 slots.task，允许的 task：\n"
            "- orders_by_user_name（slots.userName）\n"
            "- order_detail_by_user_name（slots.userName）\n"
            "- stages_by_order_id（slots.orderId，整数）\n"
            "- latest_order_stage_by_user_name（slots.userName，用于“某人最近一单订单阶段/状态”）\n"
            "execute 类问题用于执行真实业务操作，必须输出 slots.actionName 和 slots.params：\n"
            "- createOrder：params 需要 userName, amount\n"
            "- payOrder：params 需要 orderId\n"
            "- advanceStage：params 需要 orderId, stageName\n"
            "- getProductDetail：params 需要 productId\n"
            "- restockProduct：params 需要 operatorName, productId, addQty\n"
            "- updateProductPrice：params 需要 operatorName, productId, newPrice\n"
            "- setProductStatus：params 需要 operatorName, productId, status\n"
            "补充：如果用户表达了执行意图但缺少必填参数，你仍然要输出 execute，并在 params 里尽量填你能推断的内容。\n"
            "例如：\n"
            "- “支付 Apple 最近一单”：actionName=payOrder，params 至少给出 userName='Apple'，并可额外给 orderHint='latest'\n"
            "- “推进 Apple 最新订单到 PAID”：actionName=advanceStage，params 可给 userName='Apple', stageName='PAID', orderHint='latest'\n"
            "后端会用这些信息先查询补齐 orderId，然后再弹出确认卡片。\n"
            "规则：\n"
            "- 如果问“某人有哪些订单/某人的订单”，选 instance + orders_by_user_name\n"
            "- 如果问“某人订单详情/订单明细”，选 instance + order_detail_by_user_name\n"
            "- 如果问“订单123有哪些阶段/订单123的阶段”，选 instance + stages_by_order_id\n"
            "- 如果问“某人最近一单订单阶段/状态到哪了”，选 instance + latest_order_stage_by_user_name\n"
            "- 如果问“Order 有哪些字段/属性/表结构”，选 entity，name=Order\n"
            "- 如果问“Order 有哪些动作/API”，选 action，name=Order\n"
            "- 如果问“Order 和 User 什么关系”，选 relation，name='Order User'\n"
            "- 如果问“给Alice创建订单100/下单100/创建订单 amount=100 user=Alice”，选 execute，slots.actionName='createOrder'\n"
            "- 如果问“支付订单101/把订单101支付”，选 execute，slots.actionName='payOrder'\n"
            "- 如果问“推进订单101到PAID/推进阶段为SHIPPED”，选 execute，slots.actionName='advanceStage'\n"
            "- 如果问“查看商品详情/商品123详情”，选 execute，slots.actionName='getProductDetail'\n"
            "- 如果问“管理员补库存/给商品123补库存10”，选 execute，slots.actionName='restockProduct'\n"
            "- 如果问“管理员改价/给商品123改价199”，选 execute，slots.actionName='updateProductPrice'\n"
            "- 如果问“管理员上下架/给商品123下架/停售”，选 execute，slots.actionName='setProductStatus'\n"
            "必须严禁输出 Cypher。"
        )

        user = f"用户问题：{q}"
        obj = self._client.extract_json(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        )
        if not isinstance(obj, dict):
            return None
        qt = obj.get("queryType")
        name = obj.get("name")
        if qt not in ("entity", "action", "relation", "instance", "execute"):
            return None
        if not isinstance(name, str) or not name.strip():
            return None
        slots = obj.get("slots") if isinstance(obj.get("slots"), dict) else {}
        reason = obj.get("intentReason")
        if not isinstance(reason, str) or not reason.strip():
            reason = "planned by llm"
        return LLMPlan(queryType=qt, name=name.strip(), intentReason=reason.strip(), slots=slots)
