"""Importação atômica e conferida do catálogo SQLite para o PostgreSQL de Mídias."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb
from unidecode import unidecode

from buscador_midias.catalog.postgres import DEFAULT_SCHEMA, connect_postgres, validate_schema
from buscador_midias.catalog.postgres_schema import (
    DATE_COLUMNS,
    DDL,
    IDENTITY_TABLES,
    JSON_COLUMNS,
    TABLES,
)


def canonical(value: Any) -> Any:
    """Representação estável entre SQLite e tipos nativos PostgreSQL."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, bytes):
        return {"binary_sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
    return value


def converted(table: str, columns: list[str], row: Any) -> list[Any]:
    """Converte somente colunas declaradas, mantendo o resultado zlib em BYTEA."""
    result = []
    for column, value in zip(columns, row, strict=True):
        if value is not None:
            if column in JSON_COLUMNS and not (
                table == "catalog_validation_attempts" and column == "result"
            ):
                value = json.loads(value)
            elif column in DATE_COLUMNS:
                value = datetime.fromisoformat(value).astimezone(UTC)
        result.append(value)
    return result


def add_hash(digest: Any, values: list[Any]) -> None:
    """Inclui limites de linha e ordenação de chaves JSON na impressão digital."""
    digest.update(
        json.dumps(
            [canonical(v) for v in values],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    )
    digest.update(b"\n")


def create_tables(db: Any, schema: str) -> None:
    """Cria o esquema isolado com FK, tipos nativos e índices em uma transação."""
    db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    db.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
    for statement in DDL.split(";"):
        if statement.strip():
            db.execute(statement)
    # Sem extensão global: normalização latina suficiente para o catálogo em português.
    letters = [
        chr(i)
        for i in range(128, 592)
        if len(unidecode(chr(i))) == 1 and unidecode(chr(i)).isascii()
    ]
    db.execute(
        sql.SQL(
            "CREATE FUNCTION catalog_fold(value TEXT) RETURNS TEXT "
            "LANGUAGE SQL IMMUTABLE STRICT AS {} "
        ).format(
            sql.Literal(
                "SELECT lower(translate(value,"
                + sql.Literal("".join(letters)).as_string(db)
                + ","
                + sql.Literal("".join(unidecode(c) for c in letters)).as_string(db)
                + "))"
            )
        )
    )


def migrate_catalog(
    path: Path, schema: str = DEFAULT_SCHEMA, *, check_only: bool = False
) -> dict[str, Any]:
    """Copia tudo ou nada; uma segunda execução confere, nunca sobrescreve o destino."""
    validate_schema(schema)
    if not path.is_file():
        raise ValueError("Catálogo SQLite não encontrado.")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as source:
        source.execute("PRAGMA query_only=ON")
        source.execute("BEGIN")  # Um único retrato consistente para cópia e hashes.
        existing = {
            r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if existing - set(TABLES) - {"sqlite_sequence"}:
            raise ValueError(
                "Tabelas locais desconhecidas; atualize o migrador antes de continuar."
            )
        if not {"sites", "links", "mechanisms", "schema_migrations"}.issubset(existing):
            raise ValueError("Catálogo relacional incompleto.")
        if source.execute("PRAGMA quick_check").fetchone()[0] != "ok" or list(
            source.execute("PRAGMA foreign_key_check")
        ):
            raise ValueError("Catálogo local apresenta falha de integridade.")
        report: dict[str, Any] = {"schema": schema, "tables": {}}
        with connect_postgres() as connection:
            db: Any = connection
            db.execute("SELECT pg_advisory_xact_lock(%s)", (674931205,))
            exists = bool(
                db.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)).fetchone()
            )
            if (
                exists
                and not db.execute(
                    "SELECT to_regclass(%s)", (schema + ".catalog_imports",)
                ).fetchone()[0]
            ):
                raise ValueError(
                    "Esquema já existe e não pertence a uma importação do gerenciador."
                )
            if not exists:
                if check_only:
                    raise ValueError("Esquema de destino ainda não existe.")
                create_tables(db, schema)
            else:
                db.execute(
                    sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                        sql.Identifier(schema)
                    )
                )
            for table in TABLES:
                if table not in existing:
                    report["tables"][table] = dict(count=0, sha256=hashlib.sha256().hexdigest())
                    if db.execute(
                        sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                    ).fetchone()[0]:
                        raise ValueError(f"Destino contém linhas ausentes da origem em {table}.")
                    continue
                columns = [r[1] for r in source.execute(f'PRAGMA table_info("{table}")')]
                pk = [
                    r[1]
                    for r in sorted(
                        source.execute(f'PRAGMA table_info("{table}")'), key=lambda r: r[5]
                    )
                    if r[5]
                ]
                names = sql.SQL(",").join(map(sql.Identifier, columns))
                order = ",".join('"' + c + '"' for c in pk)
                digest, count = hashlib.sha256(), 0
                rows = source.execute(f'SELECT * FROM "{table}" ORDER BY {order}')
                if not exists:
                    with db.cursor().copy(
                        sql.SQL("COPY {} ({}) FROM STDIN").format(sql.Identifier(table), names)
                    ) as copy:
                        for row in rows:
                            values = converted(table, columns, row)
                            add_hash(digest, values)
                            copy.write_row(
                                [
                                    Jsonb(v)
                                    if v is not None
                                    and c in JSON_COLUMNS
                                    and not (
                                        table == "catalog_validation_attempts" and c == "result"
                                    )
                                    else v
                                    for c, v in zip(columns, values, strict=True)
                                ]
                            )
                            count += 1
                else:
                    for row in rows:
                        add_hash(digest, converted(table, columns, row))
                        count += 1
                target_digest, target_count = hashlib.sha256(), 0
                with db.cursor(name="verify_catalog") as cursor:
                    cursor.execute(
                        sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
                            names, sql.Identifier(table), sql.SQL(order)
                        )
                    )
                    for row in cursor:
                        add_hash(target_digest, list(row))
                        target_count += 1
                if count != target_count or digest.digest() != target_digest.digest():
                    raise ValueError(f"Conferência divergente em {table}; transação desfeita.")
                report["tables"][table] = dict(count=count, sha256=digest.hexdigest())
            if not exists:
                for table in IDENTITY_TABLES:
                    db.execute(
                        sql.SQL(
                            "SELECT setval(pg_get_serial_sequence(%s,'id'), "
                            "COALESCE(max(id),1), max(id) IS NOT NULL) FROM {}"
                        ).format(sql.Identifier(table)),
                        (schema + "." + table,),
                    )
                db.execute(
                    "INSERT INTO catalog_imports(source_path,manifest) VALUES(%s,%s)",
                    (str(path.resolve()), Jsonb(report)),
                )
            report["action"] = "verified" if exists else "imported"
        return report


def main() -> int:
    """Entrada Python independente para transportar depois ao powerDD."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sqlite", type=Path)
    parser.add_argument("--schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = migrate_catalog(args.sqlite, args.schema, check_only=args.check_only)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
