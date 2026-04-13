from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pymysql
from neo4j import GraphDatabase
from neo4j.exceptions import ClientError
from pymysql.cursors import DictCursor

import config


TableName = Literal["t_user", "t_product", "t_order", "t_order_item", "t_stage"]


def build_logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    return logging.getLogger("mysql-neo4j-incremental")


def to_jsonable_value(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, Decimal):
        return float(v)
    return v


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in row.items():
        out[k] = to_jsonable_value(v)
    return out


@dataclass
class Checkpoint:
    last_time: str
    last_id: int

    @staticmethod
    def initial() -> "Checkpoint":
        return Checkpoint(last_time="1970-01-01 00:00:00", last_id=0)

    def as_dt(self) -> datetime:
        return datetime.fromisoformat(self.last_time)


class CheckpointStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[TableName, Checkpoint]:
        if not self._path.exists():
            return {
                "t_user": Checkpoint.initial(),
                "t_product": Checkpoint.initial(),
                "t_order": Checkpoint.initial(),
                "t_order_item": Checkpoint.initial(),
                "t_stage": Checkpoint.initial(),
            }
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        result: dict[TableName, Checkpoint] = {
            "t_user": Checkpoint.initial(),
            "t_product": Checkpoint.initial(),
            "t_order": Checkpoint.initial(),
            "t_order_item": Checkpoint.initial(),
            "t_stage": Checkpoint.initial(),
        }
        for k in list(result.keys()):
            v = raw.get(k)
            if isinstance(v, dict) and "last_time" in v and "last_id" in v:
                result[k] = Checkpoint(last_time=str(v["last_time"]), last_id=int(v["last_id"]))
        return result

    def save(self, checkpoints: dict[TableName, Checkpoint]) -> None:
        raw = {k: {"last_time": v.last_time, "last_id": v.last_id} for k, v in checkpoints.items()}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


class MySQLIncrementalReader:
    def __init__(self) -> None:
        self._conn: pymysql.Connection | None = None

    def connect(self) -> None:
        if self._conn is not None:
            return
        self._conn = pymysql.connect(
            host=config.MYSQL_HOST,
            port=config.MYSQL_PORT,
            user=config.MYSQL_USER,
            password=config.MYSQL_PASSWORD,
            database=config.MYSQL_DATABASE,
            charset=config.MYSQL_CHARSET,
            cursorclass=DictCursor,
            autocommit=True,
        )

    def close(self) -> None:
        if self._conn is None:
            return
        self._conn.close()
        self._conn = None

    def __enter__(self) -> "MySQLIncrementalReader":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def fetch_incremental(
        self,
        table: TableName,
        columns: list[str],
        checkpoint: Checkpoint,
        limit: int,
    ) -> list[dict[str, Any]]:
        if self._conn is None:
            raise RuntimeError("MySQL connection is not established.")

        cols = ", ".join(f"`{c}`" for c in columns)
        sql = f"""
        SELECT {cols}
        FROM `{table}`
        WHERE (`update_time` > %s) OR (`update_time` = %s AND `id` > %s)
        ORDER BY `update_time`, `id`
        LIMIT %s
        """
        params = (checkpoint.last_time, checkpoint.last_time, checkpoint.last_id, limit)
        with self._conn.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        return [normalize_row(r) for r in list(rows)]


class Neo4jIncrementalWriter:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._driver = GraphDatabase.driver(config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PASSWORD))

    def close(self) -> None:
        self._driver.close()

    def _session(self):
        return self._driver.session(database=config.NEO4J_DATABASE)

    def validate_database_access(self) -> None:
        try:
            with self._session() as session:
                session.run("RETURN 1 AS ok").single()
        except ClientError as e:
            raise RuntimeError(
                f"无法打开 Neo4j 数据库 '{config.NEO4J_DATABASE}'。"
                f"如果你使用的是 Neo4j Community，请将 NEO4J_DATABASE 改为 'neo4j'。原始错误: {e}"
            ) from e

    def ensure_constraints(self) -> None:
        statements = [
            "CREATE CONSTRAINT demo_user_id IF NOT EXISTS FOR (n:Demo_User) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT demo_product_id IF NOT EXISTS FOR (n:Demo_Product) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT demo_order_id IF NOT EXISTS FOR (n:Demo_Order) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT demo_order_item_id IF NOT EXISTS FOR (n:Demo_OrderItem) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT demo_stage_id IF NOT EXISTS FOR (n:Demo_Stage) REQUIRE n.id IS UNIQUE",
        ]
        with self._session() as session:
            for cypher in statements:
                session.run(cypher).consume()

    def upsert_users(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        cypher = """
        UNWIND $rows AS row
        MERGE (u:Demo_User {id: row.id})
        SET u.name = row.name,
            u.role = row.role,
            u.update_time = row.update_time
        """
        with self._session() as session:
            session.execute_write(lambda tx: tx.run(cypher, rows=rows).consume())

    def upsert_orders_and_rel(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        cypher = """
        UNWIND $rows AS row
        MERGE (o:Demo_Order {id: row.id})
        SET o.user_id = row.user_id,
            o.amount = row.amount,
            o.item_count = row.item_count,
            o.current_stage = row.current_stage,
            o.update_time = row.update_time
        WITH o, row
        MATCH (u:Demo_User {id: row.user_id})
        MERGE (o)-[:REF_user]->(u)
        """
        with self._session() as session:
            session.execute_write(lambda tx: tx.run(cypher, rows=rows).consume())

    def upsert_products(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        cypher = """
        UNWIND $rows AS row
        MERGE (p:Demo_Product {id: row.id})
        SET p.sku = row.sku,
            p.name = row.name,
            p.price = row.price,
            p.stock = row.stock,
            p.status = row.status,
            p.category = row.category,
            p.description = row.description,
            p.image_url = row.image_url,
            p.update_time = row.update_time
        """
        with self._session() as session:
            session.execute_write(lambda tx: tx.run(cypher, rows=rows).consume())

    def upsert_order_items_and_rel(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        cypher = """
        UNWIND $rows AS row
        MERGE (i:Demo_OrderItem {id: row.id})
        SET i.order_id = row.order_id,
            i.product_id = row.product_id,
            i.product_name_snapshot = row.product_name_snapshot,
            i.unit_price_snapshot = row.unit_price_snapshot,
            i.qty = row.qty,
            i.line_amount = row.line_amount,
            i.update_time = row.update_time
        WITH i, row
        MERGE (o:Demo_Order {id: row.order_id})
        MERGE (p:Demo_Product {id: row.product_id})
        MERGE (i)-[:REF_order]->(o)
        MERGE (i)-[:REF_product]->(p)
        """
        with self._session() as session:
            session.execute_write(lambda tx: tx.run(cypher, rows=rows).consume())

    def upsert_stages_and_rel(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        cypher = """
        UNWIND $rows AS row
        MERGE (s:Demo_Stage {id: row.id})
        SET s.order_id = row.order_id,
            s.stage_name = row.stage_name,
            s.update_time = row.update_time
        WITH s, row
        MATCH (o:Demo_Order {id: row.order_id})
        MERGE (s)-[:REF_order]->(o)
        """
        with self._session() as session:
            session.execute_write(lambda tx: tx.run(cypher, rows=rows).consume())


def update_checkpoint_from_batch(cp: Checkpoint, rows: list[dict[str, Any]]) -> Checkpoint:
    if not rows:
        return cp

    def row_key(r: dict[str, Any]) -> tuple[str, int]:
        return (str(r.get("update_time") or ""), int(r.get("id") or 0))

    max_row = max(rows, key=row_key)
    return Checkpoint(last_time=str(max_row["update_time"]), last_id=int(max_row["id"]))


def run_incremental_sync(checkpoint_path: Path, full_sync: bool) -> None:
    logger = build_logger()
    store = CheckpointStore(checkpoint_path)
    checkpoints = store.load()
    if full_sync:
        checkpoints = {
            "t_user": Checkpoint.initial(),
            "t_product": Checkpoint.initial(),
            "t_order": Checkpoint.initial(),
            "t_order_item": Checkpoint.initial(),
            "t_stage": Checkpoint.initial(),
        }
        store.save(checkpoints)

    writer = Neo4jIncrementalWriter(logger)
    try:
        writer.validate_database_access()
        writer.ensure_constraints()

        with MySQLIncrementalReader() as reader:
            sync_plan: list[tuple[TableName, list[str], str]] = [
                ("t_user", ["id", "name", "role", "update_time"], "users"),
                ("t_product", ["id", "sku", "name", "price", "stock", "status", "category", "description", "image_url", "update_time"], "products"),
                ("t_order", ["id", "user_id", "amount", "item_count", "current_stage", "update_time"], "orders"),
                (
                    "t_order_item",
                    ["id", "order_id", "product_id", "product_name_snapshot", "unit_price_snapshot", "qty", "line_amount", "update_time"],
                    "order_items",
                ),
                ("t_stage", ["id", "order_id", "stage_name", "update_time"], "stages"),
            ]

            for table, cols, name in sync_plan:
                logger.info("Sync table=%s (checkpoint=%s/%s)", table, checkpoints[table].last_time, checkpoints[table].last_id)
                while True:
                    rows = reader.fetch_incremental(table=table, columns=cols, checkpoint=checkpoints[table], limit=config.BATCH_SIZE)
                    if not rows:
                        break

                    if table == "t_user":
                        writer.upsert_users(rows)
                    elif table == "t_product":
                        writer.upsert_products(rows)
                    elif table == "t_order":
                        writer.upsert_orders_and_rel(rows)
                    elif table == "t_order_item":
                        writer.upsert_order_items_and_rel(rows)
                    elif table == "t_stage":
                        writer.upsert_stages_and_rel(rows)

                    checkpoints[table] = update_checkpoint_from_batch(checkpoints[table], rows)
                    store.save(checkpoints)
                    logger.info("Synced %s batch_size=%s new_checkpoint=%s/%s", name, len(rows), checkpoints[table].last_time, checkpoints[table].last_id)

        logger.info("Incremental sync done. Checkpoint=%s", checkpoint_path)
    finally:
        writer.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", default=str(Path(__file__).with_name(".checkpoint.json")))
    parser.add_argument("--full", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_incremental_sync(checkpoint_path=Path(args.checkpoint), full_sync=bool(args.full))
