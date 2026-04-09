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
                        {"name": "createOrder", "apiPath": "/api/order/create"},
                        {"name": "payOrder", "apiPath": "/api/order/pay"},
                        {"name": "advanceStage", "apiPath": "/api/order/stage/advance"},
                    ],
                    target_entity="Order",
                )

        if not args.schema_only:
            writer.import_demo_instances(table_meta, table_data, relations, table_prefix=config.MYSQL_TABLE_PREFIX)
    finally:
        writer.close()

    logger.info("Done.")


if __name__ == "__main__":
    main()
