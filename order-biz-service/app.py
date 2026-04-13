from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from dao.mysql_dao import MySQLDao, MySQLSettings
from service.order_service import OrderService


def build_logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    return logging.getLogger("order-biz-service")


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
    logger = build_logger()

    mysql_settings = MySQLSettings(
        host=env_str("MYSQL_HOST", "127.0.0.1"),
        port=env_int("MYSQL_PORT", 3306),
        user=env_str("MYSQL_USER", "root"),
        password=env_str("MYSQL_PASSWORD", ""),
        database=env_str("MYSQL_DATABASE", ""),
        charset=env_str("MYSQL_CHARSET", "utf8mb4"),
    )
    dao = MySQLDao(mysql_settings)
    service = OrderService(dao)

    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify({"ok": True})

    @app.post("/api/order/create")
    def create_order():
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        user_name = payload.get("userName")
        amount = payload.get("amount")
        items = payload.get("items")
        if not isinstance(user_name, str) or not user_name.strip():
            return jsonify({"ok": False, "error": "userName must be a non-empty string"}), 400
        items_list: list[dict[str, Any]] | None = None
        amount_val: float | None = None
        if isinstance(items, list) and items:
            items_list = [x for x in items if isinstance(x, dict)]
            if not items_list:
                return jsonify({"ok": False, "error": "items must contain objects"}), 400
        else:
            if amount is None:
                return jsonify({"ok": False, "error": "amount is required when items is empty"}), 400
            try:
                amount_val = float(amount)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "amount must be a number"}), 400

        try:
            result = service.create_order(user_name=user_name.strip(), amount=amount_val, items=items_list)
            return jsonify({"ok": True, "result": result})
        except Exception as e:
            logger.exception("create_order failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/api/product/list")
    def list_products():
        limit = request.args.get("limit", "100")
        try:
            limit_val = int(limit)
        except ValueError:
            limit_val = 100
        try:
            rows = service.list_products(limit=limit_val)
            return jsonify({"ok": True, "result": {"products": rows}})
        except Exception as e:
            logger.exception("list_products failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/api/product/list_admin")
    def list_products_admin():
        limit = request.args.get("limit", "100")
        try:
            limit_val = int(limit)
        except ValueError:
            limit_val = 100
        try:
            rows = service.list_products_admin(limit=limit_val)
            return jsonify({"ok": True, "result": {"products": rows}})
        except Exception as e:
            logger.exception("list_products_admin failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/api/product/detail")
    def get_product_detail():
        product_id = request.args.get("productId")
        if not product_id:
            return jsonify({"ok": False, "error": "productId is required"}), 400
        try:
            pid = int(product_id)
            result = service.get_product_detail(product_id=pid)
            return jsonify({"ok": True, "result": result})
        except Exception as e:
            logger.exception("get_product_detail failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/product/restock")
    def restock_product():
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        operator_name = payload.get("operatorName")
        product_id = payload.get("productId")
        add_qty = payload.get("addQty")

        if not isinstance(operator_name, str) or not operator_name.strip():
            return jsonify({"ok": False, "error": "operatorName must be a non-empty string"}), 400
        
        try:
            pid = int(product_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "productId must be an integer"}), 400
            
        try:
            qty = int(add_qty)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "addQty must be an integer"}), 400

        try:
            result = service.restock_product(operator_name=operator_name.strip(), product_id=pid, add_qty=qty)
            return jsonify({"ok": True, "result": result})
        except Exception as e:
            logger.exception("restock_product failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/product/price/update")
    def update_product_price():
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        operator_name = payload.get("operatorName")
        product_id = payload.get("productId")
        new_price = payload.get("newPrice")

        if not isinstance(operator_name, str) or not operator_name.strip():
            return jsonify({"ok": False, "error": "operatorName must be a non-empty string"}), 400
        try:
            pid = int(product_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "productId must be an integer"}), 400
        try:
            price_val = float(new_price)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "newPrice must be a number"}), 400

        try:
            result = service.update_product_price(operator_name=operator_name.strip(), product_id=pid, new_price=price_val)
            return jsonify({"ok": True, "result": result})
        except Exception as e:
            logger.exception("update_product_price failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/product/status/update")
    def set_product_status():
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        operator_name = payload.get("operatorName")
        product_id = payload.get("productId")
        status = payload.get("status")

        if not isinstance(operator_name, str) or not operator_name.strip():
            return jsonify({"ok": False, "error": "operatorName must be a non-empty string"}), 400
        try:
            pid = int(product_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "productId must be an integer"}), 400
        if not isinstance(status, str) or not status.strip():
            return jsonify({"ok": False, "error": "status must be a non-empty string"}), 400

        try:
            result = service.set_product_status(operator_name=operator_name.strip(), product_id=pid, new_status=status.strip())
            return jsonify({"ok": True, "result": result})
        except Exception as e:
            logger.exception("set_product_status failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/order/pay")
    def pay_order():
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        order_id = payload.get("orderId")
        try:
            order_id_val = int(order_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "orderId must be an integer"}), 400

        try:
            result = service.pay_order(order_id=order_id_val)
            return jsonify({"ok": True, "result": result})
        except Exception as e:
            logger.exception("pay_order failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/order/stage/advance")
    def advance_stage():
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        order_id = payload.get("orderId")
        stage_name = payload.get("stageName")
        try:
            order_id_val = int(order_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "orderId must be an integer"}), 400
        if not isinstance(stage_name, str) or not stage_name.strip():
            return jsonify({"ok": False, "error": "stageName must be a non-empty string"}), 400

        try:
            result = service.advance_stage(order_id=order_id_val, stage_name=stage_name.strip())
            return jsonify({"ok": True, "result": result})
        except Exception as e:
            logger.exception("advance_stage failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host=env_str("BIZ_HOST", "127.0.0.1"), port=env_int("BIZ_PORT", 6000), debug=False)
