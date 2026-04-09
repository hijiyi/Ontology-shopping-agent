from __future__ import annotations

from dataclasses import dataclass

from mysql_reader import TableMeta


@dataclass(frozen=True)
class InferredRelation:
    from_table: str
    fk_column: str
    to_table: str

    @property
    def base_name(self) -> str:
        return self.fk_column[: -len("_id")]

    @property
    def rel_type(self) -> str:
        return f"REF_{self.base_name}"


def infer_relations(table_meta: dict[str, TableMeta], table_prefix: str = "t_") -> list[InferredRelation]:
    tables = sorted(table_meta.keys())
    table_set = set(tables)

    def normalize(s: str) -> str:
        return s.lower().strip()

    def guess_to_table(base: str) -> str | None:
        base = normalize(base)
        candidates = [
            f"{table_prefix}{base}",
            base,
            f"{table_prefix}{base}s",
            f"{base}s",
        ]
        for c in candidates:
            if c in table_set:
                return c
        for t in tables:
            if t == f"{table_prefix}{base}":
                return t
        for t in tables:
            if t.endswith(f"_{base}") and t.startswith(table_prefix):
                return t
        return None

    relations: list[InferredRelation] = []
    for from_table, meta in table_meta.items():
        for col in meta.columns:
            col_name = normalize(col.name)
            if not col_name.endswith("_id"):
                continue
            base = col_name[: -len("_id")]
            to_table = guess_to_table(base)
            if not to_table:
                continue
            if to_table == from_table:
                continue
            relations.append(InferredRelation(from_table=from_table, fk_column=col.name, to_table=to_table))

    unique: dict[tuple[str, str, str], InferredRelation] = {}
    for r in relations:
        unique[(r.from_table, r.fk_column, r.to_table)] = r
    return list(unique.values())


def entity_name_from_table(table_name: str, table_prefix: str = "t_") -> str:
    raw = table_name
    if raw.startswith(table_prefix):
        raw = raw[len(table_prefix) :]
    parts = [p for p in raw.split("_") if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) or table_name


def safe_rel_type(rel_type: str) -> str:
    allowed = []
    for ch in rel_type:
        if ch.isalnum() or ch == "_":
            allowed.append(ch)
        else:
            allowed.append("_")
    s = "".join(allowed)
    if not s:
        return "REF_UNKNOWN"
    if s[0].isdigit():
        return f"R_{s}"
    return s
