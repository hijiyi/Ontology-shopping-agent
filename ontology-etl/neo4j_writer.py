from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from neo4j import GraphDatabase
from neo4j.exceptions import ClientError

from mysql_reader import TableMeta
from relation_extractor import InferredRelation, entity_name_from_table, safe_rel_type


@dataclass(frozen=True)
class Neo4jConfig:
    uri: str
    user: str
    password: str
    database: str


def chunked(items: Iterable[dict[str, Any]], chunk_size: int) -> Iterator[list[dict[str, Any]]]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    batch: list[dict[str, Any]] = []
    for item in items:
        batch.append(item)
        if len(batch) >= chunk_size:
            yield batch
            batch = []
    if batch:
        yield batch


class Neo4jWriter:
    def __init__(self, config: Neo4jConfig, batch_size: int = 1000) -> None:
        self._config = config
        self._batch_size = batch_size
        self._driver = GraphDatabase.driver(self._config.uri, auth=(self._config.user, self._config.password))

    def close(self) -> None:
        self._driver.close()

    def _open_session(self):
        return self._driver.session(database=self._config.database)

    def validate_database_access(self) -> None:
        try:
            with self._open_session() as session:
                session.run("RETURN 1 AS ok").single()
        except ClientError as e:
            raise RuntimeError(
                f"无法打开 Neo4j 数据库 '{self._config.database}'。"
                f"如果你使用的是 Neo4j Community，请将 config.py 中的 NEO4J_DATABASE 改为 'neo4j'。原始错误: {e}"
            ) from e

    def ensure_constraints(self) -> None:
        statements = [
            "CREATE CONSTRAINT ont_entity_name IF NOT EXISTS FOR (n:Ont_Entity) REQUIRE n.name IS UNIQUE",
            "CREATE CONSTRAINT ont_attribute_key IF NOT EXISTS FOR (n:Ont_Attribute) REQUIRE n.key IS UNIQUE",
            "CREATE CONSTRAINT ont_relation_key IF NOT EXISTS FOR (n:Ont_Relation) REQUIRE n.key IS UNIQUE",
            "CREATE CONSTRAINT ont_action_name IF NOT EXISTS FOR (n:Ont_Action) REQUIRE n.name IS UNIQUE",
        ]
        with self._open_session() as session:
            for cypher in statements:
                session.run(cypher).consume()

    def ensure_demo_constraints(self, entity_names: list[str]) -> None:
        with self._open_session() as session:
            for en in entity_names:
                label = f"Demo_{en}"
                cypher = f"CREATE CONSTRAINT demo_{en.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE"
                session.run(cypher).consume()

    def write_ontology_structure(
        self,
        table_meta: dict[str, TableMeta],
        relations: list[InferredRelation],
        table_prefix: str = "t_",
    ) -> None:
        entities = []
        attributes = []
        for t, meta in table_meta.items():
            entity_name = entity_name_from_table(t, table_prefix=table_prefix)
            entities.append({"name": entity_name, "table": t})
            for col in meta.columns:
                key = f"{t}.{col.name}"
                attributes.append(
                    {
                        "key": key,
                        "name": col.name,
                        "table": t,
                        "data_type": col.data_type,
                        "column_type": col.column_type,
                        "is_nullable": col.is_nullable,
                        "column_key": col.column_key,
                        "comment": col.column_comment,
                    }
                )

        with self._open_session() as session:
            session.execute_write(
                lambda tx: tx.run(
                    """
                    UNWIND $rows AS row
                    MERGE (e:Ont_Entity {name: row.name})
                    ON CREATE SET e.table = row.table,
                                  e.source = 'mysql'
                    SET e.lastSeenAt = datetime()
                    """,
                    rows=entities,
                ).consume()
            )

            for batch in chunked(attributes, self._batch_size):
                session.execute_write(
                    lambda tx: tx.run(
                        """
                        UNWIND $rows AS row
                        MERGE (a:Ont_Attribute {key: row.key})
                        ON CREATE SET a.name = row.name,
                                      a.table = row.table,
                                      a.data_type = row.data_type,
                                      a.column_type = row.column_type,
                                      a.is_nullable = row.is_nullable,
                                      a.column_key = row.column_key,
                                      a.comment = row.comment,
                                      a.source = 'mysql'
                        SET a.lastSeenAt = datetime()
                        WITH a, row
                        MATCH (e:Ont_Entity {table: row.table})
                        MERGE (e)-[:HAS_ATTRIBUTE]->(a)
                        """,
                        rows=batch,
                    ).consume()
                )

            relation_rows = []
            for r in relations:
                from_entity = entity_name_from_table(r.from_table, table_prefix=table_prefix)
                to_entity = entity_name_from_table(r.to_table, table_prefix=table_prefix)
                rel_type = safe_rel_type(r.rel_type)
                key = f"{r.from_table}.{r.fk_column}->{r.to_table}"
                relation_rows.append(
                    {
                        "key": key,
                        "name": rel_type,
                        "from_table": r.from_table,
                        "to_table": r.to_table,
                        "fk_column": r.fk_column,
                        "from_entity": from_entity,
                        "to_entity": to_entity,
                    }
                )

            for batch in chunked(relation_rows, self._batch_size):
                session.execute_write(
                    lambda tx: tx.run(
                        """
                        UNWIND $rows AS row
                        MERGE (r:Ont_Relation {key: row.key})
                        ON CREATE SET r.name = row.name,
                                      r.from_table = row.from_table,
                                      r.to_table = row.to_table,
                                      r.fk_column = row.fk_column,
                                      r.source = 'mysql'
                        SET r.lastSeenAt = datetime()
                        WITH r, row
                        MATCH (fromE:Ont_Entity {name: row.from_entity})
                        MATCH (toE:Ont_Entity {name: row.to_entity})
                        MERGE (fromE)-[:HAS_RELATION]->(r)
                        MERGE (r)-[:TO]->(toE)
                        """,
                        rows=batch,
                    ).consume()
                )

            for r in relations:
                from_entity = entity_name_from_table(r.from_table, table_prefix=table_prefix)
                to_entity = entity_name_from_table(r.to_table, table_prefix=table_prefix)
                rel_type = safe_rel_type(r.rel_type)
                cypher = f"""
                MATCH (a:Ont_Entity {{name: $fromName}})
                MATCH (b:Ont_Entity {{name: $toName}})
                MERGE (a)-[:{rel_type}]->(b)
                """
                session.execute_write(lambda tx: tx.run(cypher, fromName=from_entity, toName=to_entity).consume())

    def write_actions(self, actions: list[dict[str, Any]], target_entity: str = "Order") -> None:
        with self._open_session() as session:
            session.execute_write(
                lambda tx: tx.run(
                    """
                    UNWIND $rows AS row
                    MERGE (a:Ont_Action {name: row.name})
                    ON CREATE SET a.apiPath = row.apiPath,
                                  a.source = 'system'
                    SET a.lastSeenAt = datetime()
                    """,
                    rows=actions,
                ).consume()
            )
            session.execute_write(
                lambda tx: tx.run(
                    """
                    MATCH (e:Ont_Entity {name: $entity})
                    MATCH (a:Ont_Action)
                    WHERE a.name IN $actionNames
                    MERGE (e)-[:HAS_ACTION]->(a)
                    """,
                    entity=target_entity,
                    actionNames=[a["name"] for a in actions],
                ).consume()
            )

    def import_demo_instances(
        self,
        table_meta: dict[str, TableMeta],
        table_data: dict[str, list[dict[str, Any]]],
        relations: list[InferredRelation],
        table_prefix: str = "t_",
    ) -> None:
        entity_names = [entity_name_from_table(t, table_prefix=table_prefix) for t in table_meta.keys()]
        self.ensure_demo_constraints(entity_names)

        with self._open_session() as session:
            for table_name, meta in table_meta.items():
                entity_name = entity_name_from_table(table_name, table_prefix=table_prefix)
                label = f"Demo_{entity_name}"

                pk = meta.primary_key_column() or "id"
                rows = table_data.get(table_name, [])
                if not rows:
                    continue

                cypher = f"""
                UNWIND $rows AS row
                WITH row
                WHERE row.{pk} IS NOT NULL
                MERGE (n:{label} {{id: row.{pk}}})
                SET n += row
                """
                for batch in chunked(rows, self._batch_size):
                    session.execute_write(lambda tx: tx.run(cypher, rows=batch).consume())

            for r in relations:
                from_entity = entity_name_from_table(r.from_table, table_prefix=table_prefix)
                to_entity = entity_name_from_table(r.to_table, table_prefix=table_prefix)
                from_label = f"Demo_{from_entity}"
                to_label = f"Demo_{to_entity}"
                rel_type = safe_rel_type(r.rel_type)

                from_pk = (table_meta.get(r.from_table).primary_key_column() if table_meta.get(r.from_table) else None) or "id"
                to_pk = (table_meta.get(r.to_table).primary_key_column() if table_meta.get(r.to_table) else None) or "id"

                forward = f"""
                UNWIND $rows AS row
                WITH row
                WHERE row.{from_pk} IS NOT NULL AND row.{r.fk_column} IS NOT NULL
                MATCH (a:{from_label} {{id: row.{from_pk}}})
                MATCH (b:{to_label} {{id: row.{r.fk_column}}})
                MERGE (a)-[:{rel_type}]->(b)
                """
                reverse = f"""
                UNWIND $rows AS row
                WITH row
                WHERE row.{from_pk} IS NOT NULL AND row.{r.fk_column} IS NOT NULL
                MATCH (a:{from_label} {{id: row.{from_pk}}})
                MATCH (b:{to_label} {{id: row.{r.fk_column}}})
                MERGE (b)-[:{rel_type}]->(a)
                """

                rows = table_data.get(r.from_table, [])
                for batch in chunked(rows, self._batch_size):
                    session.execute_write(lambda tx: tx.run(forward, rows=batch).consume())
                    session.execute_write(lambda tx: tx.run(reverse, rows=batch).consume())
