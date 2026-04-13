from __future__ import annotations

from typing import Any

from dao.mysql_dao import MySQLDao


class OrderService:
    def __init__(self, dao: MySQLDao) -> None:
        self._dao = dao

    def list_products(self, limit: int = 100) -> list[dict[str, Any]]:
        limit_val = int(limit) if isinstance(limit, int) else 100
        if limit_val <= 0:
            limit_val = 100
        if limit_val > 200:
            limit_val = 200
        return self._dao.fetch_all(
            "SELECT id, sku, name, price, stock, status, category, description, image_url, update_time FROM t_product WHERE status = 'ACTIVE' ORDER BY id DESC LIMIT %s",
            (limit_val,),
        )

    def list_products_admin(self, limit: int = 100) -> list[dict[str, Any]]:
        limit_val = int(limit) if isinstance(limit, int) else 100
        if limit_val <= 0:
            limit_val = 100
        if limit_val > 200:
            limit_val = 200
        return self._dao.fetch_all(
            "SELECT id, sku, name, price, stock, status, category, description, image_url, update_time FROM t_product ORDER BY id DESC LIMIT %s",
            (limit_val,),
        )

    def get_product_detail(self, product_id: int) -> dict[str, Any]:
        product = self._dao.fetch_one(
            "SELECT id, sku, name, price, stock, status, category, description, image_url, update_time FROM t_product WHERE id=%s",
            (product_id,),
        )
        if not product:
            raise ValueError(f"product not found: {product_id}")
        return product

    def restock_product(self, operator_name: str, product_id: int, add_qty: int) -> dict[str, Any]:
        if add_qty <= 0:
            raise ValueError("addQty must be greater than 0")
        
        user = self._dao.fetch_one("SELECT id, name, role FROM t_user WHERE name=%s", (operator_name,))
        if not user or user.get("role") != "ADMIN":
            raise ValueError(f"permission denied: user '{operator_name}' is not an ADMIN")
            
        product = self._dao.fetch_one("SELECT id, name, stock, status FROM t_product WHERE id=%s", (product_id,))
        if not product:
            raise ValueError(f"product not found: {product_id}")
            
        if product.get("status") == "DISCONTINUED":
            raise ValueError(f"cannot restock a DISCONTINUED product: {product_id}")
            
        self._dao.execute(
            "UPDATE t_product SET stock = stock + %s, update_time=NOW() WHERE id=%s",
            (add_qty, product_id)
        )
        
        new_product = self._dao.fetch_one("SELECT id, stock FROM t_product WHERE id=%s", (product_id,))
        return {
            "productId": product_id,
            "operatorName": operator_name,
            "addQty": add_qty,
            "newStock": new_product.get("stock") if new_product else 0
        }

    def update_product_price(self, operator_name: str, product_id: int, new_price: float) -> dict[str, Any]:
        try:
            price_val = float(new_price)
        except (TypeError, ValueError):
            raise ValueError("newPrice must be a number")
        if price_val <= 0:
            raise ValueError("newPrice must be greater than 0")

        user = self._dao.fetch_one("SELECT id, name, role FROM t_user WHERE name=%s", (operator_name,))
        if not user or user.get("role") != "ADMIN":
            raise ValueError(f"permission denied: user '{operator_name}' is not an ADMIN")

        product = self._dao.fetch_one("SELECT id, price, status FROM t_product WHERE id=%s", (product_id,))
        if not product:
            raise ValueError(f"product not found: {product_id}")
        if product.get("status") == "DISCONTINUED":
            raise ValueError(f"cannot update price for a DISCONTINUED product: {product_id}")

        old_price = float(product.get("price") or 0.0)
        self._dao.execute(
            "UPDATE t_product SET price=%s, update_time=NOW() WHERE id=%s",
            (price_val, product_id),
        )
        return {"productId": product_id, "operatorName": operator_name, "oldPrice": old_price, "newPrice": price_val}

    def set_product_status(self, operator_name: str, product_id: int, new_status: str) -> dict[str, Any]:
        status_val = str(new_status or "").strip().upper()
        if not status_val:
            raise ValueError("status must be a non-empty string")

        allowed = {"ACTIVE", "INACTIVE", "OUT_OF_STOCK", "DISCONTINUED"}
        if status_val not in allowed:
            raise ValueError(f"invalid status: {status_val}")

        user = self._dao.fetch_one("SELECT id, name, role FROM t_user WHERE name=%s", (operator_name,))
        if not user or user.get("role") != "ADMIN":
            raise ValueError(f"permission denied: user '{operator_name}' is not an ADMIN")

        product = self._dao.fetch_one("SELECT id, status FROM t_product WHERE id=%s", (product_id,))
        if not product:
            raise ValueError(f"product not found: {product_id}")

        old_status = str(product.get("status") or "").upper()
        if old_status == "DISCONTINUED" and status_val != "DISCONTINUED":
            raise ValueError(f"cannot change status of a DISCONTINUED product: {product_id}")

        self._dao.execute(
            "UPDATE t_product SET status=%s, update_time=NOW() WHERE id=%s",
            (status_val, product_id),
        )
        return {"productId": product_id, "operatorName": operator_name, "oldStatus": old_status, "newStatus": status_val}

    def create_order(self, user_name: str, amount: float | None = None, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        user = self._dao.fetch_one("SELECT id, name FROM t_user WHERE name=%s", (user_name,))
        if user is None:
            user_id = self._dao.insert_and_get_id(
                "INSERT INTO t_user(name, update_time) VALUES (%s, NOW())",
                (user_name,),
            )
        else:
            user_id = int(user["id"])
            self._dao.execute("UPDATE t_user SET update_time=NOW() WHERE id=%s", (user_id,))

        total_amount = float(amount) if amount is not None else 0.0
        total_item_count = 0
        order_id = self._dao.insert_and_get_id(
            "INSERT INTO t_order(user_id, amount, item_count, current_stage, update_time) VALUES (%s, %s, %s, %s, NOW())",
            (user_id, total_amount, total_item_count, "CREATED"),
        )

        order_items: list[dict[str, Any]] = []
        if items:
            total_amount = 0.0
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                product_id = raw.get("productId")
                qty = raw.get("qty")
                try:
                    product_id_val = int(product_id)
                except (TypeError, ValueError):
                    raise ValueError("productId must be an integer")
                try:
                    qty_val = int(qty)
                except (TypeError, ValueError):
                    raise ValueError("qty must be an integer")
                if qty_val <= 0:
                    raise ValueError("qty must be greater than 0")

                product = self._dao.fetch_one(
                    "SELECT id, sku, name, price, stock, status FROM t_product WHERE id=%s",
                    (product_id_val,),
                )
                if product is None:
                    raise ValueError(f"product not found: {product_id_val}")
                if str(product.get("status") or "ACTIVE") != "ACTIVE":
                    raise ValueError(f"product is not active: {product_id_val}")

                stock_val = int(product.get("stock") or 0)
                if stock_val < qty_val:
                    raise ValueError(f"insufficient stock for product {product_id_val}")

                unit_price = float(product.get("price") or 0.0)
                line_amount = unit_price * float(qty_val)

                item_id = self._dao.insert_and_get_id(
                    """
                    INSERT INTO t_order_item(
                      order_id, product_id, product_name_snapshot, unit_price_snapshot, qty, line_amount, update_time
                    ) VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    """,
                    (order_id, product_id_val, str(product.get("name") or ""), unit_price, qty_val, line_amount),
                )
                self._dao.execute(
                    "UPDATE t_product SET stock = stock - %s, update_time=NOW() WHERE id=%s",
                    (qty_val, product_id_val),
                )

                total_amount += line_amount
                total_item_count += qty_val
                order_items.append(
                    {
                        "orderItemId": item_id,
                        "productId": product_id_val,
                        "sku": product.get("sku"),
                        "name": product.get("name"),
                        "unitPrice": unit_price,
                        "qty": qty_val,
                        "lineAmount": line_amount,
                    }
                )

            self._dao.execute(
                "UPDATE t_order SET amount=%s, item_count=%s, update_time=NOW() WHERE id=%s",
                (total_amount, total_item_count, order_id),
            )

        self._dao.insert_and_get_id(
            "INSERT INTO t_stage(order_id, stage_name, update_time) VALUES (%s, %s, NOW())",
            (order_id, "CREATED"),
        )

        return {
            "orderId": order_id,
            "userId": user_id,
            "userName": user_name,
            "amount": total_amount,
            "itemCount": total_item_count,
            "items": order_items,
        }

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
