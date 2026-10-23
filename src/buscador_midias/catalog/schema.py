"""Modelo relacional local e migração transacional do cadastro destinado à base de Mídias."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

LINK_COLUMNS = (
    "id,source_id,url,kind,label,notes,active,deleted_at,version,created_at,updated_at,"
    "evidence,origin,model,last_test"
)
LINK_DEFINITION = """
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE RESTRICT,
    url TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('feed','sitemap')),
    label TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1, deleted_at TEXT,
    version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '{}', origin TEXT NOT NULL DEFAULT 'manual',
    model TEXT NOT NULL REFERENCES mechanisms(model) ON DELETE RESTRICT,
    last_test TEXT NOT NULL DEFAULT '{}',
    UNIQUE(source_id,url)
"""


def rename_legacy_tables(db: sqlite3.Connection) -> None:
    """Renomeia tabelas legadas mantendo IDs; recusa estados ambíguos sem sobrescrever dados."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for old, new in [
        ("sources", "sites"),
        ("endpoints", "links"),
        ("mechanism_settings", "mechanisms"),
    ]:
        if old in tables:
            if new in tables:
                raise RuntimeError(f"Tabelas {old} e {new} coexistem; migração interrompida.")
            db.execute(f"ALTER TABLE {old} RENAME TO {new}")


def enforce_relations(db: sqlite3.Connection) -> None:
    """Estabelece vínculo obrigatório link→site e link→mecanismo sem perder provas ou revisões."""
    foreign_keys = {(r[2], r[3], r[4]) for r in db.execute("PRAGMA foreign_key_list(links)")}
    required = {("mechanisms", "model", "model"), ("sites", "source_id", "id")}
    if not required.issubset(foreign_keys):
        columns = {r[1] for r in db.execute("PRAGMA table_info(links)")}
        if columns != set(LINK_COLUMNS.split(",")):
            raise RuntimeError("Colunas inesperadas em links; migração interrompida sem perda.")
        db.execute(f"CREATE TABLE links_relational ({LINK_DEFINITION})")
        db.execute(f"INSERT INTO links_relational({LINK_COLUMNS}) SELECT {LINK_COLUMNS} FROM links")
        db.execute("DROP TABLE links")
        db.execute("ALTER TABLE links_relational RENAME TO links")
    db.execute("CREATE INDEX IF NOT EXISTS link_site ON links(source_id,deleted_at)")
    db.execute("CREATE INDEX IF NOT EXISTS link_mechanism ON links(model,deleted_at)")
    if list(db.execute("PRAGMA foreign_key_check")):
        raise RuntimeError("Migração interrompida: vínculos inválidos no catálogo.")
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)"
    )
    db.execute(
        "INSERT OR IGNORE INTO schema_migrations(version,applied_at) VALUES(1,?)",
        (datetime.now(UTC).isoformat(),),
    )


def create_test_history(db: sqlite3.Connection) -> None:
    """Mantém testes salvos relacionados ao link, com instantâneo imutável da configuração."""
    db.execute("""CREATE TABLE IF NOT EXISTS link_test_results (
        id INTEGER PRIMARY KEY,
        link_id INTEGER NOT NULL REFERENCES links(id) ON DELETE RESTRICT,
        model TEXT NOT NULL REFERENCES mechanisms(model) ON DELETE RESTRICT,
        test_id TEXT NOT NULL, model_version INTEGER, observed_at TEXT,
        parameters TEXT NOT NULL, result TEXT NOT NULL,
        UNIQUE(link_id,test_id)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS link_test_history ON link_test_results(link_id,id)")
    if db.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone():
        return
    # Recupera somente os resultados já salvos no cadastro e na auditoria local.
    for row in db.execute("SELECT id,last_test FROM links WHERE last_test<>'{}'"):
        archive_test(db, row[0], json.loads(row[1]))
    for row in db.execute(
        "SELECT entity_id,before_json,after_json FROM events WHERE entity='endpoint' ORDER BY id"
    ):
        if not db.execute("SELECT 1 FROM links WHERE id=?", (row[0],)).fetchone():
            continue
        for snapshot in row[1:]:
            data = json.loads(snapshot) if snapshot else {}
            if data.get("id") != row[0]:
                continue  # Uma cópia preserva origem, sem atribuir teste a outro link.
            test = data.get("last_test", {})
            archive_test(db, row[0], json.loads(test) if isinstance(test, str) else test)
    db.execute(
        "INSERT INTO schema_migrations(version,applied_at) VALUES(2,?)",
        (datetime.now(UTC).isoformat(),),
    )


def archive_test(db: sqlite3.Connection, link_id: int, result: dict[str, object]) -> None:
    """Anexa um resultado salvo uma única vez, mantendo o conteúdo original."""
    if (
        not result.get("test_id")
        or not db.execute(
            "SELECT 1 FROM mechanisms WHERE model=?", (result.get("model"),)
        ).fetchone()
    ):
        return
    db.execute(
        """INSERT OR IGNORE INTO link_test_results
        (link_id,model,test_id,model_version,observed_at,parameters,result)
        VALUES(?,?,?,?,?,?,?)""",
        (
            link_id,
            result["model"],
            result["test_id"],
            result.get("model_version"),
            result.get("observed_at"),
            json.dumps(result.get("parameters", {})),
            json.dumps(result, ensure_ascii=False),
        ),
    )
