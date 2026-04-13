from __future__ import annotations

import argparse
import logging

import config
from mysql_reader import MySQLConfig, MySQLReader
from neo4j_writer import Neo4jConfig, Neo4jWriter
from relation_extractor import infer_relations


def build_logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    return logging.getLogger("ontology-etl")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema-only", action="store_true")
    parser.add_argument("--data-only", action="store_true")
    parser.add_argument("--no-actions", action="store_true")
    return parser.parse_args()


def main() -> None:
    logger = build_logger()
    args = parse_args()

    if args.schema_only and args.data_only:
        raise SystemExit("--schema-only and --data-only cannot be used together")

    mysql_conf = MySQLConfig(
        host=config.MYSQL_HOST,
        port=config.MYSQL_PORT,
        user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,
        database=config.MYSQL_DATABASE,
        charset=config.MYSQL_CHARSET,
    )
    neo4j_conf = Neo4jConfig(
        uri=config.NEO4J_URI,
        user=config.NEO4J_USER,
        password=config.NEO4J_PASSWORD,
        database=config.NEO4J_DATABASE,
    )

    with MySQLReader(mysql_conf) as reader:
        tables = reader.list_tables(table_prefix=config.MYSQL_TABLE_PREFIX)
        logger.info("Discovered tables: %s", tables)

        table_meta = reader.read_table_meta(tables)
        table_data = {} if args.schema_only else reader.read_all_table_data(tables)

    relations = infer_relations(table_meta, table_prefix=config.MYSQL_TABLE_PREFIX)
    logger.info("Inferred relations: %s", [(r.from_table, r.fk_column, r.to_table) for r in relations])

    writer = Neo4jWriter(neo4j_conf, batch_size=config.BATCH_SIZE)
    try:
        writer.validate_database_access()
        writer.ensure_constraints()

        if not args.data_only:
            writer.write_ontology_structure(table_meta, relations, table_prefix=config.MYSQL_TABLE_PREFIX)
            if not args.no_actions:
                writer.write_actions(
                    actions=[
                        {
                            "name": "createOrder",
                            "apiPath": "/api/order/create",
                            "method": "POST",
                            "uiSchema": """{"title":"创建订单","description":"选择商品与数量，系统会自动计算订单金额。","fields":[{"name":"userName","label":"用户","type":"string","required":true,"placeholder":"例如：Alice"},{"name":"items","label":"商品列表","type":"product_items","required":true,"optionsAction":"listProducts"}]}""",
                        },
                        {"name": "payOrder", "apiPath": "/api/order/pay"},
                        {"name": "advanceStage", "apiPath": "/api/order/stage/advance"},
                    ],
                    target_entity="Order",
                )
                writer.write_actions(
                    actions=[
                        {"name": "listProducts", "apiPath": "/api/product/list", "method": "GET"},
                        {"name": "listProductsAdmin", "apiPath": "/api/product/list_admin", "method": "GET"},
                        {
                            "name": "restockProduct",
                            "apiPath": "/api/product/restock",
                            "method": "POST",
                            "uiSchema": """{"title":"补库存 (仅限管理员)","description":"为现有商品增加库存数量。","fields":[{"name":"operatorName","label":"操作人 (管理员用户名)","type":"string","required":true,"placeholder":"例如: Admin"},{"name":"productId","label":"选择商品","type":"product_select","optionsAction":"listProductsAdmin","required":true},{"name":"addQty","label":"补充数量","type":"int","required":true,"placeholder":"输入新增的库存件数"}]}"""
                        },
                        {
                            "name": "updateProductPrice",
                            "apiPath": "/api/product/price/update",
                            "method": "POST",
                            "uiSchema": """{"title":"改价 (仅限管理员)","description":"更新指定商品的销售价格。","fields":[{"name":"operatorName","label":"操作人 (管理员用户名)","type":"string","required":true,"placeholder":"例如: Admin"},{"name":"productId","label":"选择商品","type":"product_select","optionsAction":"listProductsAdmin","required":true},{"name":"newPrice","label":"新价格","type":"number","required":true,"placeholder":"例如: 199.00"}]}""",
                        },
                        {
                            "name": "setProductStatus",
                            "apiPath": "/api/product/status/update",
                            "method": "POST",
                            "uiSchema": """{"title":"上下架/状态更新 (仅限管理员)","description":"设置商品状态：ACTIVE(上架)、INACTIVE(下架)、OUT_OF_STOCK(缺货)、DISCONTINUED(停售)。停售后不可恢复。","fields":[{"name":"operatorName","label":"操作人 (管理员用户名)","type":"string","required":true,"placeholder":"例如: Admin"},{"name":"productId","label":"选择商品","type":"product_select","optionsAction":"listProductsAdmin","required":true},{"name":"status","label":"新状态","type":"string","required":true,"placeholder":"ACTIVE/INACTIVE/OUT_OF_STOCK/DISCONTINUED"}]}""",
                        },
                        {
                            "name": "getProductDetail",
                            "apiPath": "/api/product/detail",
                            "method": "GET",
                            "uiSchema": """{"title":"查看商品详情","description":"查看商品的图文说明、分类与当前库存。","fields":[{"name":"productId","label":"选择商品","type":"product_select","optionsAction":"listProducts","required":true}]}"""
                        },
                    ],
                    target_entity="Product",
                )

        if not args.schema_only:
            writer.import_demo_instances(table_meta, table_data, relations, table_prefix=config.MYSQL_TABLE_PREFIX)
    finally:
        writer.close()

    logger.info("Done.")


if __name__ == "__main__":
    main()
