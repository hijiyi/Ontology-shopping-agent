from __future__ import annotations

from typing import Any

from dao.mysql_dao import MySQLDao


class OrderService:
    def __init__(self, dao: MySQLDao) -> None:
        self._dao = dao

    def create_order(self, user_name: str, amount: float) -> dict[str, Any]:
        user = self._dao.fetch_one("SELECT id, name FROM t_user WHERE name=%s", (user_name,))
        if user is None:
            user_id = self._dao.insert_and_get_id(
                "INSERT INTO t_user(name, update_time) VALUES (%s, NOW())",
                (user_name,),
            )
        else:
            user_id = int(user["id"])
            self._dao.execute("UPDATE t_user SET update_time=NOW() WHERE id=%s", (user_id,))

        order_id = self._dao.insert_and_get_id(
            "INSERT INTO t_order(user_id, amount, current_stage, update_time) VALUES (%s, %s, %s, NOW())",
            (user_id, amount, "CREATED"),
        )

        self._dao.insert_and_get_id(
            "INSERT INTO t_stage(order_id, stage_name, update_time) VALUES (%s, %s, NOW())",
            (order_id, "CREATED"),
        )

        return {"orderId": order_id, "userId": user_id, "userName": user_name, "amount": amount}

    def pay_order(self, order_id: int) -> dict[str, Any]:
        order = self._dao.fetch_one("SELECT id, user_id, amount FROM t_order WHERE id=%s", (order_id,))
        if order is None:
            raise ValueError(f"order not found: {order_id}")

        self._dao.execute("UPDATE t_order SET current_stage=%s, update_time=NOW() WHERE id=%s", ("PAID", order_id))
        stage_id = self._dao.insert_and_get_id(
            "INSERT INTO t_stage(order_id, stage_name, update_time) VALUES (%s, %s, NOW())",
            (order_id, "PAID"),
        )
        return {"orderId": order_id, "stageId": stage_id, "stage": "PAID"}

    def advance_stage(self, order_id: int, stage_name: str) -> dict[str, Any]:
        order = self._dao.fetch_one("SELECT id FROM t_order WHERE id=%s", (order_id,))
        if order is None:
            raise ValueError(f"order not found: {order_id}")

        self._dao.execute("UPDATE t_order SET current_stage=%s, update_time=NOW() WHERE id=%s", (stage_name, order_id))
        stage_id = self._dao.insert_and_get_id(
            "INSERT INTO t_stage(order_id, stage_name, update_time) VALUES (%s, %s, NOW())",
            (order_id, stage_name),
        )
        return {"orderId": order_id, "stageId": stage_id, "stage": stage_name}
