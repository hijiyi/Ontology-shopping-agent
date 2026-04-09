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
        if not isinstance(user_name, str) or not user_name.strip():
            return jsonify({"ok": False, "error": "userName must be a non-empty string"}), 400
        if amount is None:
            return jsonify({"ok": False, "error": "amount is required"}), 400
        try:
            amount_val = float(amount)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "amount must be a number"}), 400

        try:
            result = service.create_order(user_name=user_name.strip(), amount=amount_val)
            return jsonify({"ok": True, "result": result})
        except Exception as e:
            logger.exception("create_order failed")
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

