"""Catálogo persistente do MVP, com importação única e exclusão lógica auditada."""

from __future__ import annotations

import ipaddress
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from unidecode import unidecode

from buscador_midias.catalog.http_transport import validate_transport
from buscador_midias.catalog.mechanisms import get_mechanism
from buscador_midias.catalog.models import get_model, list_models
from buscador_midias.catalog.schema import (
    archive_test,
    create_search_intervals,
    create_test_history,
    enforce_relations,
    rename_legacy_tables,
)
from buscador_midias.catalog.store_errors import CatalogError as CatalogError
from buscador_midias.sites.loader import load_sites

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY, identity TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL, url TEXT NOT NULL, country TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1, deleted_at TEXT,
    version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'manual'
);
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sites(id),
    url TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('feed','sitemap')),
    label TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1, deleted_at TEXT,
    version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '{}', origin TEXT NOT NULL DEFAULT 'manual',
    UNIQUE(source_id,url)
);
CREATE INDEX IF NOT EXISTS link_site ON links(source_id,deleted_at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, entity TEXT NOT NULL, entity_id INTEGER,
    action TEXT NOT NULL, happened_at TEXT NOT NULL, before_json TEXT, after_json TEXT
);
CREATE TABLE IF NOT EXISTS mechanisms (
    model TEXT PRIMARY KEY, parameters TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
);
"""


def clean_url(value: object, *, source: bool = False) -> str:
    """Valida URL pública HTTP(S) sem buscar conteúdo nem resolver DNS."""
    if not isinstance(value, str) or len(value) > 4096:
        raise CatalogError("Informe uma URL válida com http:// ou https://.")
    value = value.strip()
    if any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        raise CatalogError("A URL não pode conter espaços ou caracteres de controle.")
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").encode("idna").decode("ascii").lower()
        port = parts.port
    except (ValueError, UnicodeError) as error:
        raise CatalogError("URL inválida.") from error
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        raise CatalogError("Use uma URL pública HTTP(S), sem usuário ou senha.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if (
        (address is not None and not address.is_global)
        or host == "localhost"
        or host.endswith((".localhost", ".local", ".internal"))
        or "." not in host
        or host.endswith(".")
        or any(
            not label
            or label.startswith("-")
            or label.endswith("-")
            or not all(c.isalnum() or c == "-" for c in label)
            for label in host.split(".")
        )
    ):
        raise CatalogError("Informe um domínio público de notícias.")
    if source and (parts.query or parts.fragment):
        raise CatalogError("A URL do site deve ser o domínio ou seção, sem query ou fragmento.")
    netloc = host
    if port and port != (443 if parts.scheme == "https" else 80):
        netloc += f":{port}"
    path = parts.path.rstrip("/") if source else (parts.path or "/")
    return urlunsplit((parts.scheme, netloc, path, parts.query, ""))


def _text(data: dict[str, Any], field: str, maximum: int, required: bool = False) -> str:
    value = data.get(field, "")
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise CatalogError(f"Campo {field}: informe um texto de até {maximum} caracteres.")
    return value.strip()


def _active(data: dict[str, Any]) -> int:
    if type(data.get("active", True)) is not bool:
        raise CatalogError("O estado ativo deve ser verdadeiro ou falso.")
    return int(data.get("active", True))


def _stamp() -> str:
    return datetime.now(UTC).isoformat()


class CatalogStore:
    """Banco SQLite local do cadastro; não altera CSV, PostgreSQL ou a rodada 12."""

    def __init__(self, path: Path, snapshot: Path, sites: Path) -> None:
        """Define banco e arquivos usados apenas na primeira importação."""
        self.path, self.snapshot, self.sites = path, snapshot, sites
        self._lock = threading.Lock()
        self._ready = False

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Abre uma conexão por operação e fecha mesmo após falhas."""
        self.initialize()
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.create_function(
            "catalog_fold", 1, lambda value: unidecode(str(value)).casefold(), deterministic=True
        )
        return db

    def initialize(self) -> None:
        """Importa uma única vez, atomicamente; reinícios não reativam ou recriam registros."""
        with self._lock:
            if self._ready:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = self._connect()
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("BEGIN IMMEDIATE")
                rename_legacy_tables(db)
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                for definition in list_models():
                    db.execute(
                        "INSERT OR IGNORE INTO mechanisms(model,parameters,updated_at) "
                        "VALUES(?,?,?)",
                        (definition["id"], json.dumps(definition["defaults"]), _stamp()),
                    )
                columns = {r[1] for r in db.execute("PRAGMA table_info(links)")}
                if "model" not in columns:
                    db.execute("ALTER TABLE links ADD COLUMN model TEXT NOT NULL DEFAULT 'auto'")
                    db.execute(
                        "UPDATE links SET model=CASE WHEN kind='feed' THEN 'rss' "
                        "WHEN json_extract(evidence,'$.index') THEN 'sitemap_tree' "
                        "ELSE 'sitemap' END"
                    )
                if "last_test" not in columns:
                    db.execute("ALTER TABLE links ADD COLUMN last_test TEXT NOT NULL DEFAULT '{}'")
                if not db.execute("SELECT 1 FROM metadata WHERE key='seed'").fetchone():
                    if not self.snapshot.is_file():
                        raise CatalogError("Retrato de importação indisponível.", 503)
                    snapshot = json.loads(self.snapshot.read_text(encoding="utf-8"))
                    catalog = {s.key: s for s in load_sites(self.sites).sites}
                    now = _stamp()
                    endpoints = 0
                    for source in snapshot["sources"]:
                        key = source["key"]
                        site = catalog.get(key)
                        url = site.url if site else f"https://{key}"
                        cursor = db.execute(
                            "INSERT INTO sites(identity,name,url,country,language,created_at,"
                            "updated_at,origin) VALUES(?,?,?,?,?,?,?,'mapping')",
                            (
                                key,
                                source["name"],
                                url,
                                source.get("country", ""),
                                site.language if site else "",
                                now,
                                now,
                            ),
                        )
                        for item in snapshot.get("mechanisms", {}).get(key, []):
                            db.execute(
                                "INSERT OR IGNORE INTO links(source_id,url,kind,active,"
                                "created_at,updated_at,evidence,origin,model) VALUES(?,?,?,?,?,?,?,"
                                "'mapping',?)",
                                (
                                    cursor.lastrowid,
                                    item["url"],
                                    item["kind"],
                                    int(not item.get("audio", False)),
                                    now,
                                    now,
                                    json.dumps(item, ensure_ascii=False),
                                    "rss"
                                    if item["kind"] == "feed"
                                    else ("sitemap_tree" if item.get("index") else "sitemap"),
                                ),
                            )
                            endpoints += 1
                    provenance = dict(
                        imported_at=now,
                        snapshot=str(self.snapshot),
                        generated_at=snapshot.get("meta", {}).get("generated_at"),
                        sources=len(snapshot["sources"]),
                        endpoints=endpoints,
                    )
                    db.execute("INSERT INTO metadata VALUES('seed',?)", (json.dumps(provenance),))
                    self._event(db, "catalog", None, "import", None, provenance)
                enforce_relations(db)
                create_test_history(db)
                create_search_intervals(db)
                if "access_transport" not in {r[1] for r in db.execute("PRAGMA table_info(links)")}:
                    db.execute(
                        "ALTER TABLE links ADD COLUMN access_transport "
                        "TEXT NOT NULL DEFAULT 'direct'"
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
            self._ready = True

    @staticmethod
    def _event(
        db: sqlite3.Connection,
        entity: str,
        ident: int | None,
        action: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        db.execute(
            "INSERT INTO events(entity,entity_id,action,happened_at,before_json,after_json) "
            "VALUES(?,?,?,?,?,?)",
            (
                entity,
                ident,
                action,
                _stamp(),
                json.dumps(before, ensure_ascii=False) if before is not None else None,
                json.dumps(after, ensure_ascii=False) if after is not None else None,
            ),
        )

    @staticmethod
    def _record(db: sqlite3.Connection, table: str, ident: int) -> dict[str, Any]:
        row = db.execute(
            f"SELECT * FROM {table} WHERE id=? AND deleted_at IS NULL", (ident,)
        ).fetchone()
        if row is None:
            raise CatalogError("Registro não encontrado ou excluído.", 404)
        return dict(row)

    @staticmethod
    def _endpoint(
        row: dict[str, Any],
        source_active: bool,
        model_version: int,
        source_url: str,
    ) -> dict[str, Any]:
        result = dict(row)
        result["evidence"] = json.loads(result["evidence"])
        result["last_test"] = json.loads(result["last_test"])
        result["current_model_version"] = model_version
        result["test_config_current"] = bool(result["last_test"]) and (
            result["last_test"].get("model_version") == model_version
            and result["last_test"].get("source_url") == source_url
            and result["last_test"].get("access_transport", "direct")
            == result.get("access_transport", "direct")
        )
        result["active"] = bool(result["active"])
        result["effective_active"] = result["active"] and source_active
        return result

    def summary(self) -> dict[str, Any]:
        """Conta cadastros visíveis e links efetivamente ativos."""
        with self.connection() as db:
            sources = db.execute(
                "SELECT COUNT(*) total,COALESCE(SUM(active),0) active "
                "FROM sites WHERE deleted_at IS NULL"
            ).fetchone()
            links = db.execute(
                "SELECT COUNT(*) total,COALESCE(SUM(e.active AND s.active),0) active "
                "FROM links e JOIN sites s ON s.id=e.source_id "
                "WHERE e.deleted_at IS NULL AND s.deleted_at IS NULL"
            ).fetchone()
            countries = [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT country FROM sites WHERE deleted_at IS NULL AND country!='' "
                    "ORDER BY country"
                )
            ]
            seed = json.loads(
                db.execute("SELECT value FROM metadata WHERE key='seed'").fetchone()[0]
            )
            return dict(
                sources=dict(sources), endpoints=dict(links), countries=countries, seed=seed
            )

    def sources(
        self,
        q: str = "",
        status: str = "all",
        country: str = "",
        offset: int = 0,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Busca fontes com paginação, filtros e contagens por fonte."""
        with self.connection() as db:
            where = ["s.deleted_at IS NULL"]
            args: list[Any] = []
            if q:
                where.append(
                    "(instr(catalog_fold(s.name),catalog_fold(?)) "
                    "OR instr(catalog_fold(s.url),catalog_fold(?)))"
                )
                args += [q, q]
            if status in ("active", "inactive"):
                where.append("s.active=?")
                args.append(int(status == "active"))
            if country:
                where.append("s.country=?")
                args.append(country)
            predicate = " AND ".join(where)
            total = db.execute(f"SELECT COUNT(*) FROM sites s WHERE {predicate}", args).fetchone()[
                0
            ]
            rows = db.execute(
                "SELECT s.*,COALESCE(e.total,0) endpoints,COALESCE(e.rss,0) rss,"
                "COALESCE(e.sitemaps,0) sitemaps,COALESCE(e.enabled,0)*s.active enabled "
                "FROM sites s LEFT JOIN (SELECT source_id,COUNT(*) total,"
                "SUM(kind='feed') rss,SUM(kind='sitemap') sitemaps,SUM(active) enabled "
                "FROM links WHERE deleted_at IS NULL GROUP BY source_id) e "
                f"ON e.source_id=s.id WHERE {predicate} "
                "ORDER BY s.name COLLATE NOCASE,s.id LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
            return dict(items=[dict(r) for r in rows], total=total, offset=offset, limit=limit)

    def source(self, ident: int) -> dict[str, Any]:
        """Consulta os dados editáveis de uma fonte."""
        with self.connection() as db:
            return self._record(db, "sites", ident)

    def endpoints(
        self,
        source_id: int,
        q: str = "",
        status: str = "all",
        kind: str = "all",
        offset: int = 0,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Pagina RSS/sitemaps, preservando evidência e estado próprio versus herdado."""
        with self.connection() as db:
            source = self._record(db, "sites", source_id)
            where = ["source_id=?", "deleted_at IS NULL"]
            args: list[Any] = [source_id]
            if q:
                where.append(
                    "(instr(catalog_fold(url),catalog_fold(?)) "
                    "OR instr(catalog_fold(label),catalog_fold(?)))"
                )
                args += [q, q]
            if status in ("active", "inactive"):
                where.append("active=?")
                args.append(int(status == "active"))
            if kind in ("feed", "sitemap"):
                where.append("kind=?")
                args.append(kind)
            predicate = " AND ".join(where)
            total = db.execute(f"SELECT COUNT(*) FROM links WHERE {predicate}", args).fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM links WHERE {predicate} ORDER BY kind,url,id LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
            model_versions = {
                r[0]: r[1] for r in db.execute("SELECT model,version FROM mechanisms")
            }
            return dict(
                source=source,
                total=total,
                offset=offset,
                limit=limit,
                items=[
                    self._endpoint(
                        dict(r), bool(source["active"]), model_versions[r["model"]], source["url"]
                    )
                    for r in rows
                ],
            )

    def save(
        self,
        entity: str,
        data: dict[str, Any],
        ident: int | None = None,
        source_id: int | None = None,
        test_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Cria ou altera um cadastro atomicamente, com revisão e trilha de auditoria."""
        table = "sites" if entity == "source" else "links"
        allowed = {"name", "url", "country", "language", "notes", "active", "version"}
        if entity != "source":
            allowed = {
                "url",
                "kind",
                "model",
                "label",
                "notes",
                "active",
                "version",
                "search_interval_seconds",
                "access_transport",
            }
        if data.keys() - allowed:
            raise CatalogError("O cadastro contém campos desconhecidos.")
        try:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                before = self._record(db, table, ident) if ident is not None else None
                if before and (
                    type(data.get("version")) is not int or data["version"] != before["version"]
                ):
                    raise CatalogError("Este registro mudou. Atualize a tela antes de salvar.", 409)
                previous = {**before, "active": bool(before["active"])} if before else {}
                merged = {**previous, **data}
                now = _stamp()
                values: dict[str, Any] = dict(
                    url=clean_url(merged.get("url"), source=entity == "source"),
                    notes=_text(merged, "notes", 4000),
                    active=_active(merged),
                    updated_at=now,
                )
                if entity == "source":
                    parts = urlsplit(values["url"])
                    values.update(
                        name=_text(merged, "name", 200, True),
                        country=_text(merged, "country", 100),
                        language=_text(merged, "language", 60),
                        identity=(parts.hostname or "").removeprefix("www.") + parts.path,
                    )
                else:
                    source_id = before["source_id"] if before else source_id
                    parent = self._record(db, "sites", source_id or 0)
                    kind = merged.get("kind")
                    if kind not in ("feed", "sitemap"):
                        raise CatalogError("Selecione RSS/Atom ou sitemap.")
                    values.update(kind=kind, label=_text(merged, "label", 200), source_id=source_id)
                    model = merged.get("model", "auto")
                    if before and kind != before["kind"] and "model" not in data:
                        model = "auto"
                    get_model(model, kind)
                    values["model"] = model
                    interval = merged.get("search_interval_seconds", 14400)
                    if type(interval) is not int or not 1 <= interval <= 9223372036854775807:
                        raise CatalogError(
                            "O intervalo de busca deve ser um número inteiro positivo de segundos."
                        )
                    values["search_interval_seconds"] = interval
                    try:
                        values["access_transport"] = validate_transport(
                            merged.get("access_transport", "direct")
                        )
                    except ValueError as error:
                        raise CatalogError(str(error)) from error
                    # A prova pertence ao endereço e tipo que foram efetivamente testados.
                    if before and (
                        values["url"] != before["url"]
                        or kind != before["kind"]
                        or model != before["model"]
                        or values["access_transport"] != before.get("access_transport", "direct")
                    ):
                        values["evidence"] = "{}"
                        values["last_test"] = "{}"
                    if test_result is not None:
                        settings = db.execute(
                            "SELECT version FROM mechanisms WHERE model=?", (model,)
                        ).fetchone()
                        if settings is None or test_result.get("model_version") != settings[0]:
                            raise CatalogError(
                                "A configuração do mecanismo mudou. Execute um novo teste.", 409
                            )
                        if (
                            test_result["url"] != values["url"]
                            or test_result["model"] != model
                            or test_result["source_id"] != source_id
                            or test_result["requested_kind"] != kind
                            or test_result["source_url"] != parent["url"]
                            or test_result.get("access_transport", "direct")
                            != values["access_transport"]
                        ):
                            raise CatalogError(
                                "O teste pertence a outra configuração. Teste novamente."
                            )
                        values["last_test"] = json.dumps(test_result, ensure_ascii=False)
                        if test_result.get("xml_valid") and test_result.get("compatible"):
                            values["evidence"] = json.dumps(
                                self._test_evidence(test_result), ensure_ascii=False
                            )
                if before:
                    values["version"] = before["version"] + 1
                    assignments = ",".join(f"{key}=?" for key in values)
                    db.execute(
                        f"UPDATE {table} SET {assignments} WHERE id=?", [*values.values(), ident]
                    )
                else:
                    values["created_at"] = now
                    columns = ",".join(values)
                    placeholders = ",".join("?" for _ in values)
                    cursor = db.execute(
                        f"INSERT INTO {table}({columns}) VALUES({placeholders})",
                        list(values.values()),
                    )
                    ident = cursor.lastrowid
                after = self._record(db, table, ident or 0)
                if entity != "source" and test_result is not None:
                    archive_test(db, int(ident or 0), test_result)
                self._event(db, entity, ident, "update" if before else "create", before, after)
                return after
        except sqlite3.IntegrityError as error:
            raise CatalogError(
                "Este site ou link já está cadastrado, inclusive entre excluídos.", 409
            ) from error

    @staticmethod
    def _test_evidence(result: dict[str, Any]) -> dict[str, Any]:
        return dict(
            evidence="xml",
            listed=result["root_entries"],
            children=result["root_children"],
            index=result["root_index"],
            observed_at=result["observed_at"],
            texts=result["text_samples"],
            round_news=None,
            example=next(
                (s["url"] for s in result["samples"] if s.get("text") and s.get("eligible", True)),
                "",
            ),
            audio=result["outcome"] == "audio_only",
            access_method=result.get("access_method", "direct"),
            bright_data_requests=result.get("bright_data_requests", 0),
            test_id=result["test_id"],
        )

    def endpoint(self, ident: int) -> dict[str, Any]:
        """Obtém uma configuração para testar, recusando fontes excluídas."""
        with self.connection() as db:
            row = self._record(db, "links", ident)
            source = self._record(db, "sites", row["source_id"])
            version = db.execute(
                "SELECT version FROM mechanisms WHERE model=?", (row["model"],)
            ).fetchone()[0]
            return self._endpoint(row, bool(source["active"]), version, source["url"])

    def models(self) -> list[dict[str, Any]]:
        """Lista mecanismos e parâmetros persistidos, com uso no catálogo visível."""
        with self.connection() as db:
            records = {r["model"]: dict(r) for r in db.execute("SELECT * FROM mechanisms")}
            counts = dict(
                db.execute(
                    "SELECT e.model,COUNT(*) FROM links e JOIN sites s ON s.id=e.source_id "
                    "WHERE e.deleted_at IS NULL AND s.deleted_at IS NULL GROUP BY e.model"
                )
            )
            return [
                dict(
                    **definition,
                    parameters=get_mechanism(definition["id"]).validate(
                        json.loads(records[definition["id"]]["parameters"])
                    ),
                    version=records[definition["id"]]["version"],
                    updated_at=records[definition["id"]]["updated_at"],
                    endpoints=counts.get(definition["id"], 0),
                )
                for definition in list_models()
            ]

    def model_settings(self, model: object) -> dict[str, Any]:
        """Obtém uma cópia validada da configuração e sua revisão."""
        mechanism = get_mechanism(model)
        with self.connection() as db:
            row = db.execute("SELECT * FROM mechanisms WHERE model=?", (mechanism.id,)).fetchone()
            return dict(
                id=mechanism.id,
                version=row["version"],
                updated_at=row["updated_at"],
                parameters=mechanism.validate(json.loads(row["parameters"])),
            )

    def save_model(self, model: str, data: dict[str, Any]) -> dict[str, Any]:
        """Atualiza parâmetros globais atomicamente, sem apagar provas ou alterar endpoints."""
        mechanism = get_mechanism(model)
        if data.keys() - {"parameters", "version"} or not isinstance(data.get("parameters"), dict):
            raise CatalogError("Informe apenas os parâmetros do mecanismo e sua revisão.")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            before = dict(
                db.execute("SELECT * FROM mechanisms WHERE model=?", (mechanism.id,)).fetchone()
            )
            if type(data.get("version")) is not int or data["version"] != before["version"]:
                raise CatalogError("A configuração mudou. Atualize antes de salvar.", 409)
            parameters = mechanism.validate(
                {**json.loads(before["parameters"]), **data["parameters"]}
            )
            if parameters != json.loads(before["parameters"]):
                db.execute(
                    "UPDATE mechanisms SET parameters=?,version=version+1,updated_at=? "
                    "WHERE model=?",
                    (json.dumps(parameters), _stamp(), mechanism.id),
                )
                after = dict(
                    db.execute("SELECT * FROM mechanisms WHERE model=?", (mechanism.id,)).fetchone()
                )
                self._event(db, "mechanism", None, "update", before, after)
        return self.model_settings(mechanism.id)

    def record_test(self, ident: int, version: int, result: dict[str, Any]) -> int:
        """Aplica um teste só à revisão que o iniciou; nunca sobrescreve uma edição posterior."""
        row = self.save("endpoint", dict(version=version), ident, test_result=result)
        return int(row["version"])

    def delete(self, entity: str, ident: int, version: object) -> None:
        """Oculta um registro sem apagar evidências; fonte excluída oculta também seus links."""
        table = "sites" if entity == "source" else "links"
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            before = self._record(db, table, ident)
            if entity != "source":
                self._record(db, "sites", before["source_id"])
            if type(version) is not int or before["version"] != version:
                raise CatalogError("Este registro mudou. Atualize a tela antes de excluir.", 409)
            now = _stamp()
            db.execute(
                f"UPDATE {table} SET deleted_at=?,active=0,updated_at=?,"
                "version=version+1 WHERE id=?",
                (now, now, ident),
            )
            after = dict(db.execute(f"SELECT * FROM {table} WHERE id=?", (ident,)).fetchone())
            self._event(db, entity, ident, "delete", before, after)
