from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal

from dao.neo4j_dao import Neo4jDao


QueryType = Literal["entity", "action", "relation", "instance"]


@dataclass(frozen=True)
class QueryResult:
    queryType: QueryType
    input: dict[str, Any]
    data: Any


class QueryService:
    def __init__(self, dao: Neo4jDao) -> None:
        self._dao = dao

    def query(self, query_type: QueryType, name: str) -> QueryResult:
        if query_type == "entity":
            return self.query_entity(name)
        if query_type == "action":
            return self.query_action(name)
        if query_type == "relation":
            return self.query_relation(name)
        if query_type == "instance":
            return self.query_instance(name)
        raise ValueError(f"unsupported queryType: {query_type}")

    def query_with_debug(self, query_type: QueryType, name: str) -> dict[str, Any]:
        if query_type == "entity":
            if name == "__ALL__":
                cypher = self._cypher_entity_list()
                params: dict[str, Any] = {}
                result = self.query_entity(name)
            else:
                cypher = self._cypher_entity()
                params = {"name": name}
                result = self.query_entity(name)
            return {"queryType": query_type, "name": name, "cypher": cypher.strip(), "params": params, "result": result.__dict__}

        if query_type == "relation":
            if name == "__ALL__":
                cypher = self._cypher_relation_overview()
                params: dict[str, Any] = {}
                mode = "overview"
                result = self.query_relation(name)
            else:
                cypher, params, mode = self._cypher_relation(name)
                result = self.query_relation(name)
            return {"queryType": query_type, "name": name, "mode": mode, "cypher": cypher.strip(), "params": params, "result": result.__dict__}

        if query_type == "action":
            cy1, p1 = self._cypher_action_by_entity(name)
            rows = self._dao.run_read(cy1, p1)
            if rows:
                result = self.query_action(name)
                return {"queryType": query_type, "name": name, "mode": "byEntity", "cypher": cy1.strip(), "params": p1, "result": result.__dict__}
            cy2, p2 = self._cypher_action_by_keyword(name)
            result = self.query_action(name)
            return {"queryType": query_type, "name": name, "mode": "byKeyword", "cypher": cy2.strip(), "params": p2, "result": result.__dict__}

        if query_type == "instance":
            cypher, params, mode = self._cypher_instance(name)
            result = self.query_instance(name)
            return {"queryType": query_type, "name": name, "mode": mode, "cypher": cypher.strip(), "params": params, "result": result.__dict__}

        raise ValueError(f"unsupported queryType: {query_type}")

    def query_entity(self, entity_name: str) -> QueryResult:
        if entity_name == "__ALL__":
            cypher = self._cypher_entity_list()
            rows = self._dao.run_read(cypher, {})
            data = {
                "found": True,
                "total": len(rows),
                "entities": [{"name": r.get("name"), "table": r.get("table")} for r in rows],
            }
            return QueryResult(queryType="entity", input={"mode": "list", "name": entity_name}, data=data)

        cypher = self._cypher_entity()
        rows = self._dao.run_read(cypher, {"name": entity_name})
        if not rows:
            data: dict[str, Any] = {"entity": entity_name, "found": False, "attributes": []}
        else:
            r = rows[0]
            attrs = [a for a in (r.get("attributes") or []) if a.get("name") is not None]
            data = {"entity": r.get("entity"), "table": r.get("table"), "found": True, "attributes": attrs}
        return QueryResult(queryType="entity", input={"name": entity_name}, data=data)

    def query_action(self, name_or_entity: str) -> QueryResult:
        cypher_by_entity, p1 = self._cypher_action_by_entity(name_or_entity)
        rows = self._dao.run_read(cypher_by_entity, p1)
        if rows:
            r = rows[0]
            actions = [a for a in (r.get("actions") or []) if a.get("name") is not None]
            return QueryResult(
                queryType="action",
                input={"name": name_or_entity, "mode": "byEntity"},
                data={"entity": r.get("entity"), "actions": actions},
            )

        cypher_by_action, p2 = self._cypher_action_by_keyword(name_or_entity)
        rows2 = self._dao.run_read(cypher_by_action, p2)
        data = [{"action": r.get("action"), "apiPath": r.get("apiPath"), "entities": r.get("entities") or []} for r in rows2]
        return QueryResult(queryType="action", input={"name": name_or_entity, "mode": "byKeyword"}, data=data)

    def query_relation(self, name: str) -> QueryResult:
        if name == "__ALL__":
            cypher = self._cypher_relation_overview()
            rows = self._dao.run_read(cypher, {})
            return QueryResult(queryType="relation", input={"mode": "overview"}, data=rows)

        cypher, params, mode = self._cypher_relation(name)
        if mode == "between":
            rows = self._dao.run_read(cypher, params)
            return QueryResult(queryType="relation", input={"name": name, "mode": "between"}, data=rows)

        rows2 = self._dao.run_read(cypher, params)
        if not rows2:
            data: dict[str, Any] = {"entity": name, "found": False, "relations": []}
        else:
            r = rows2[0]
            data = {"entity": r.get("entity"), "found": True, "relations": r.get("relations") or []}
        return QueryResult(queryType="relation", input={"name": name, "mode": "byEntity"}, data=data)

    def query_instance(self, question: str) -> QueryResult:
        cypher, params, mode = self._cypher_instance(question)
        if mode == "unknown":
            return QueryResult(queryType="instance", input={"question": question, "mode": mode}, data={"found": False, "reason": "unsupported question"})
        rows = self._dao.run_read(cypher, params)
        if not rows:
            if mode == "ordersByUserName":
                return QueryResult(
                    queryType="instance",
                    input={"question": question, "mode": mode, **params},
                    data={"userName": params.get("userName"), "total": 0, "orders": []},
                )
            if mode == "stagesByOrderId":
                return QueryResult(
                    queryType="instance",
                    input={"question": question, "mode": mode, **params},
                    data={"orderId": params.get("orderId"), "total": 0, "stages": []},
                )
            return QueryResult(queryType="instance", input={"question": question, "mode": mode, **params}, data=[])

        r = rows[0]
        return QueryResult(queryType="instance", input={"question": question, "mode": mode, **params}, data=r)

    def query_instance_by_task(self, task: str, slots: dict[str, Any], raw_question: str) -> QueryResult:
        cypher, params, mode = self._cypher_instance_by_task(task, slots)
        if mode == "unknown":
            return QueryResult(
                queryType="instance",
                input={"question": raw_question, "mode": mode, "task": task, "slots": slots},
                data={"found": False, "reason": "unsupported task"},
            )

        rows = self._dao.run_read(cypher, params)
        if not rows:
            if mode in ("ordersByUserName", "orderDetailByUserName"):
                return QueryResult(
                    queryType="instance",
                    input={"question": raw_question, "mode": mode, **params},
                    data={"userName": params.get("userName"), "total": 0, "orders": []},
                )
            if mode == "stagesByOrderId":
                return QueryResult(
                    queryType="instance",
                    input={"question": raw_question, "mode": mode, **params},
                    data={"orderId": params.get("orderId"), "total": 0, "stages": []},
                )
            return QueryResult(queryType="instance", input={"question": raw_question, "mode": mode, **params}, data=[])

        r = rows[0]
        return QueryResult(queryType="instance", input={"question": raw_question, "mode": mode, **params}, data=r)

    def query_instance_by_task_with_debug(self, task: str, slots: dict[str, Any], raw_question: str) -> dict[str, Any]:
        cypher, params, mode = self._cypher_instance_by_task(task, slots)
        result = self.query_instance_by_task(task, slots, raw_question)
        return {
            "queryType": "instance",
            "name": raw_question,
            "mode": mode,
            "cypher": cypher.strip(),
            "params": params,
            "result": result.__dict__,
        }

    def _cypher_entity(self) -> str:
        return """
        MATCH (e:Ont_Entity {name: $name})
        OPTIONAL MATCH (e)-[:HAS_ATTRIBUTE]->(a:Ont_Attribute)
        RETURN
          e.name AS entity,
          e.table AS table,
          collect(DISTINCT {
            key: a.key,
            name: a.name,
            data_type: a.data_type,
            column_type: a.column_type,
            is_nullable: a.is_nullable,
            column_key: a.column_key,
            comment: a.comment
          }) AS attributes
        """

    def _cypher_entity_list(self) -> str:
        return """
        MATCH (e:Ont_Entity)
        RETURN e.name AS name, e.table AS table
        ORDER BY name
        """

    def _cypher_action_by_entity(self, entity_name: str) -> tuple[str, dict[str, Any]]:
        return (
            """
            MATCH (e:Ont_Entity {name: $name})-[r]->(a:Ont_Action)
            RETURN
              e.name AS entity,
              collect(DISTINCT {name: a.name, apiPath: a.apiPath, relType: type(r)}) AS actions
            """,
            {"name": entity_name},
        )

    def _cypher_action_by_keyword(self, kw: str) -> tuple[str, dict[str, Any]]:
        return (
            """
            MATCH (a:Ont_Action)
            WHERE toLower(a.name) CONTAINS toLower($kw)
            OPTIONAL MATCH (e:Ont_Entity)-[r]->(a)
            RETURN
              a.name AS action,
              a.apiPath AS apiPath,
              collect(DISTINCT {entity: e.name, relType: type(r)}) AS entities
            ORDER BY action
            """,
            {"kw": kw},
        )

    def _cypher_relation(self, name: str) -> tuple[str, dict[str, Any], str]:
        parts = [p for p in [x.strip() for x in name.replace(",", " ").split()] if p]
        if len(parts) >= 2:
            a, b = parts[0], parts[1]
            return (
                """
                MATCH (x:Ont_Entity {name: $a})-[r]->(y:Ont_Entity {name: $b})
                RETURN type(r) AS relType, x.name AS from, y.name AS to
                UNION
                MATCH (x:Ont_Entity {name: $a})-[:HAS_RELATION]->(rr:Ont_Relation)-[:TO]->(y:Ont_Entity {name: $b})
                RETURN rr.name AS relType, x.name AS from, y.name AS to
                ORDER BY relType
                """,
                {"a": a, "b": b},
                "between",
            )

        return (
            """
            MATCH (e:Ont_Entity {name: $name})
            OPTIONAL MATCH (e)-[r]->(t:Ont_Entity)
            WITH e, collect(DISTINCT {relType: type(r), to: t.name}) AS rels1
            OPTIONAL MATCH (e)-[:HAS_RELATION]->(rr:Ont_Relation)-[:TO]->(t2:Ont_Entity)
            WITH e, rels1, collect(DISTINCT {relType: rr.name, to: t2.name, fk_column: rr.fk_column}) AS rels2
            WITH e, rels1 + rels2 AS rels
            RETURN e.name AS entity, [x IN rels WHERE x.relType IS NOT NULL] AS relations
            """,
            {"name": name},
            "byEntity",
        )

    def _cypher_relation_overview(self) -> str:
        return """
        MATCH (a:Ont_Entity)-[r]->(b:Ont_Entity)
        RETURN a.name AS from, type(r) AS relType, b.name AS to, null AS fk_column
        UNION
        MATCH (a:Ont_Entity)-[:HAS_RELATION]->(rr:Ont_Relation)-[:TO]->(b:Ont_Entity)
        RETURN a.name AS from, rr.name AS relType, b.name AS to, rr.fk_column AS fk_column
        ORDER BY from, relType, to
        """

    def _cypher_instance(self, question: str) -> tuple[str, dict[str, Any], str]:
        q = (question or "").strip()

        m_detail = re.search(r"(?:用户)?(?P<user>[A-Za-z0-9_\u4e00-\u9fa5]+?)(?:的)?\s*(?:订单详情|订单明细|订单信息)", q)
        if m_detail:
            user_name = m_detail.group("user").strip()
            return (
                """
                MATCH (u:Demo_User {name: $userName})
                OPTIONAL MATCH (u)<-[:REF_user]-(o:Demo_Order)
                OPTIONAL MATCH (o)<-[:REF_order]-(s:Demo_Stage)
                WITH u, o, collect(DISTINCT s.stage_name) AS stageNames
                WITH u, o, [x IN stageNames WHERE x IS NOT NULL] AS stages
                ORDER BY o.id
                RETURN
                  u.name AS userName,
                  count(DISTINCT o) AS total,
                  collect({
                    id: o.id,
                    amount: o.amount,
                    current_stage: o.current_stage,
                    update_time: o.update_time,
                    user_id: o.user_id,
                    stages: stages
                  })[0..50] AS orders
                """,
                {"userName": user_name},
                "orderDetailByUserName",
            )

        m1 = re.search(r"(?:用户)?(?P<user>[A-Za-z0-9_\u4e00-\u9fa5]+)\s*(?:有哪些|有多少|列出|查看).*(?:订单)", q)
        if m1:
            user_name = m1.group("user")
            return (
                """
                MATCH (u:Demo_User {name: $userName})
                OPTIONAL MATCH (u)<-[:REF_user]-(o:Demo_Order)
                WITH u, o
                ORDER BY o.id
                RETURN
                  u.name AS userName,
                  count(o) AS total,
                  collect({
                    id: o.id,
                    amount: o.amount,
                    current_stage: o.current_stage,
                    update_time: o.update_time,
                    user_id: o.user_id
                  })[0..50] AS orders
                """,
                {"userName": user_name},
                "ordersByUserName",
            )

        m2 = re.search(r"(?:用户)?(?P<user>[A-Za-z0-9_\u4e00-\u9fa5]+)的订单", q)
        if m2:
            user_name = m2.group("user")
            return (
                """
                MATCH (u:Demo_User {name: $userName})
                OPTIONAL MATCH (u)<-[:REF_user]-(o:Demo_Order)
                WITH u, o
                ORDER BY o.id
                RETURN
                  u.name AS userName,
                  count(o) AS total,
                  collect({
                    id: o.id,
                    amount: o.amount,
                    current_stage: o.current_stage,
                    update_time: o.update_time,
                    user_id: o.user_id
                  })[0..50] AS orders
                """,
                {"userName": user_name},
                "ordersByUserName",
            )

        m3 = re.search(r"订单\s*(?P<order_id>\d+)\s*(?:有哪些|有多少|列出|查看).*(?:阶段)", q)
        if m3:
            order_id = int(m3.group("order_id"))
            return (
                """
                MATCH (o:Demo_Order {id: $orderId})
                OPTIONAL MATCH (o)<-[:REF_order]-(s:Demo_Stage)
                WITH o, s
                ORDER BY s.id
                RETURN
                  o.id AS orderId,
                  count(s) AS total,
                  collect({
                    id: s.id,
                    stage_name: s.stage_name,
                    update_time: s.update_time,
                    order_id: s.order_id
                  })[0..50] AS stages
                """,
                {"orderId": order_id},
                "stagesByOrderId",
            )

        m4 = re.search(r"订单\s*(?P<order_id>\d+)\s*的阶段", q)
        if m4:
            order_id = int(m4.group("order_id"))
            return (
                """
                MATCH (o:Demo_Order {id: $orderId})
                OPTIONAL MATCH (o)<-[:REF_order]-(s:Demo_Stage)
                WITH o, s
                ORDER BY s.id
                RETURN
                  o.id AS orderId,
                  count(s) AS total,
                  collect({
                    id: s.id,
                    stage_name: s.stage_name,
                    update_time: s.update_time,
                    order_id: s.order_id
                  })[0..50] AS stages
                """,
                {"orderId": order_id},
                "stagesByOrderId",
            )

        return ("RETURN 1 AS ok", {}, "unknown")

    def _cypher_instance_by_task(self, task: str, slots: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
        t = (task or "").strip().lower()

        if t == "latest_order_stage_by_user_name":
            user_name = str(slots.get("userName") or "").strip()
            if not user_name:
                return ("RETURN 1 AS ok", {}, "unknown")
            return (
                """
                MATCH (u:Demo_User {name: $userName})
                OPTIONAL MATCH (u)<-[:REF_user]-(o:Demo_Order)
                WITH o
                ORDER BY o.update_time DESC, o.id DESC
                RETURN
                  o.id AS orderId,
                  o.current_stage AS current_stage,
                  o.update_time AS update_time
                LIMIT 1
                """,
                {"userName": user_name},
                "latestOrderStageByUserName",
            )

        if t == "orders_by_user_name":
            user_name = str(slots.get("userName") or "").strip()
            if not user_name:
                return ("RETURN 1 AS ok", {}, "unknown")
            return (
                """
                MATCH (u:Demo_User {name: $userName})
                OPTIONAL MATCH (u)<-[:REF_user]-(o:Demo_Order)
                WITH u, o
                ORDER BY o.id
                RETURN
                  u.name AS userName,
                  count(o) AS total,
                  collect({
                    id: o.id,
                    amount: o.amount,
                    current_stage: o.current_stage,
                    update_time: o.update_time,
                    user_id: o.user_id
                  })[0..50] AS orders
                """,
                {"userName": user_name},
                "ordersByUserName",
            )

        if t == "order_detail_by_user_name":
            user_name = str(slots.get("userName") or "").strip()
            if not user_name:
                return ("RETURN 1 AS ok", {}, "unknown")
            return (
                """
                MATCH (u:Demo_User {name: $userName})
                OPTIONAL MATCH (u)<-[:REF_user]-(o:Demo_Order)
                OPTIONAL MATCH (o)<-[:REF_order]-(s:Demo_Stage)
                WITH u, o, collect(DISTINCT s.stage_name) AS stageNames
                WITH u, o, [x IN stageNames WHERE x IS NOT NULL] AS stages
                ORDER BY o.id
                RETURN
                  u.name AS userName,
                  count(DISTINCT o) AS total,
                  collect({
                    id: o.id,
                    amount: o.amount,
                    current_stage: o.current_stage,
                    update_time: o.update_time,
                    user_id: o.user_id,
                    stages: stages
                  })[0..50] AS orders
                """,
                {"userName": user_name},
                "orderDetailByUserName",
            )

        if t == "stages_by_order_id":
            order_id_raw = slots.get("orderId")
            try:
                order_id = int(order_id_raw)
            except (TypeError, ValueError):
                order_id = None

            if order_id is None:
                user_name = str(slots.get("userName") or "").strip()
                order_hint = str(slots.get("orderHint") or "").strip().lower()
                if user_name and order_hint in ("latest", "recent", "newest"):
                    return (
                        """
                        MATCH (u:Demo_User {name: $userName})
                        OPTIONAL MATCH (u)<-[:REF_user]-(o:Demo_Order)
                        WITH u, o
                        ORDER BY o.update_time DESC, o.id DESC
                        WITH u, collect(o)[0] AS latest
                        OPTIONAL MATCH (latest)<-[:REF_order]-(s:Demo_Stage)
                        WITH latest, s
                        ORDER BY s.id
                        RETURN
                          latest.id AS orderId,
                          count(s) AS total,
                          collect({
                            id: s.id,
                            stage_name: s.stage_name,
                            update_time: s.update_time,
                            order_id: s.order_id
                          })[0..50] AS stages
                        """,
                        {"userName": user_name},
                        "stagesByOrderId",
                    )
                return ("RETURN 1 AS ok", {}, "unknown")
            return (
                """
                MATCH (o:Demo_Order {id: $orderId})
                OPTIONAL MATCH (o)<-[:REF_order]-(s:Demo_Stage)
                WITH o, s
                ORDER BY s.id
                RETURN
                  o.id AS orderId,
                  count(s) AS total,
                  collect({
                    id: s.id,
                    stage_name: s.stage_name,
                    update_time: s.update_time,
                    order_id: s.order_id
                  })[0..50] AS stages
                """,
                {"orderId": order_id},
                "stagesByOrderId",
            )

        return ("RETURN 1 AS ok", {}, "unknown")
