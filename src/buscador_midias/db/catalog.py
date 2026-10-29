"""Repositório do catálogo (sites, links, mecanismos) sobre as tabelas ``bm_*``.

Mantém o contrato que a API e a tela do gerenciador já usam (``model``, ``source_id``, versões
otimistas, erros 404/409), mas com o desenho novo:

- o que a máquina escreve (prova do último teste, evidência) vive em ``bm_link_state`` e não mexe
  na ``version`` do link;
- quem desativou um link fica em colunas do próprio link, e a auditoria guarda só o diff.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb

from buscador_midias.catalog.http_transport import validate_transport
from buscador_midias.catalog.mechanisms import get_mechanism
from buscador_midias.catalog.models import get_model, list_models
from buscador_midias.catalog.read_order import READ_ORDERS, UNKNOWN
from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.catalog.urls import clean_url
from buscador_midias.db.connection import Repository, connect
from buscador_midias.db.schema import install

WRITE_LOCK = 674931205

_SITE_FIELDS = {"name", "url", "country", "language", "notes", "active", "version"}
_LINK_FIELDS = {
    "url",
    "kind",
    "model",
    "label",
    "notes",
    "active",
    "version",
    "search_interval_seconds",
    "access_transport",
    "read_order",
}
_SITE_COLUMNS = (
    "id,identity,name,url,country,language,notes,active,deleted_at,version,origin,"
    "created_at,updated_at"
)
_LINK_COLUMNS = (
    "l.id,l.site_id AS source_id,l.url,l.kind,l.mechanism AS model,l.label,l.notes,l.active,"
    "l.deleted_at,l.deactivated_by,l.deactivated_reason,l.deactivated_at,l.access_transport,"
    "l.read_order,l.search_interval_seconds,l.route,l.version,l.origin,l.created_at,l.updated_at"
)
_LINK_SELECT = (
    f"SELECT {_LINK_COLUMNS},COALESCE(s.last_test,'{{}}'::jsonb) AS last_test,"
    "COALESCE(s.evidence,'{}'::jsonb) AS evidence FROM bm_links l "
    "LEFT JOIN bm_link_state s ON s.link_id=l.id"
)


def _text(data: Mapping[str, Any], field: str, maximum: int, required: bool = False) -> str:
    value = data.get(field, "")
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise CatalogError(f"Campo {field}: informe um texto de até {maximum} caracteres.")
    return value.strip()


def _active(data: Mapping[str, Any]) -> int:
    if type(data.get("active", True)) is not bool:
        raise CatalogError("O estado ativo deve ser verdadeiro ou falso.")
    return int(data.get("active", True))


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _plain(row: Mapping[str, Any]) -> dict[str, Any]:
    """Datas em ISO 8601, como a API sempre devolveu."""
    return {key: _iso(value) for key, value in row.items()}


def _diff(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> dict[str, Any]:
    """Só os campos que mudaram, como ``{campo: [antes, depois]}``."""
    previous = before or {}
    return {
        key: [_iso(previous.get(key)), _iso(value)]
        for key, value in after.items()
        if key not in ("updated_at", "created_at") and previous.get(key) != value
    }


class CatalogRepository(Repository):
    """Cadastro editável: uma transação por operação e escrita serializada por advisory lock."""

    backend = "postgres"

    def install(self) -> None:
        """Cria as tabelas, as funções e as configurações padrão dos mecanismos que faltam."""
        with connect(self.schema, env=self.env, create=True) as db:
            install(db)
            for definition in list_models():
                db.execute(
                    "INSERT INTO bm_mechanisms(model,parameters) VALUES(%s,%s::jsonb) "
                    "ON CONFLICT (model) DO NOTHING",
                    (definition["id"], Jsonb(definition["defaults"])),
                )

    # --- consultas -----------------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """Conta cadastros visíveis e links efetivamente ativos."""
        with self.connection() as db:
            sources = db.execute(
                "SELECT COUNT(*) AS total,COALESCE(SUM(active),0)::int AS active "
                "FROM bm_sites WHERE deleted_at IS NULL"
            ).fetchone()
            links = db.execute(
                "SELECT COUNT(*) AS total,"
                "COALESCE(SUM(CASE WHEN l.active=1 AND s.active=1 THEN 1 ELSE 0 END),0)::int "
                "AS active FROM bm_links l JOIN bm_sites s ON s.id=l.site_id "
                "WHERE l.deleted_at IS NULL AND s.deleted_at IS NULL"
            ).fetchone()
            countries = [
                row["country"]
                for row in db.execute(
                    "SELECT DISTINCT country FROM bm_sites "
                    "WHERE deleted_at IS NULL AND country<>'' ORDER BY country"
                )
            ]
            total = db.execute("SELECT COUNT(*) AS n FROM bm_sites").fetchone()
            assert sources and links and total
            return dict(
                sources=dict(sources),
                endpoints=dict(links),
                countries=countries,
                seed=dict(sources=total["n"]),
            )

    def sources(
        self, q: str = "", status: str = "all", country: str = "", offset: int = 0, limit: int = 25
    ) -> dict[str, Any]:
        """Busca fontes com paginação, filtros e contagens por fonte."""
        where = ["s.deleted_at IS NULL"]
        args: list[Any] = []
        if q:
            where.append(
                "(strpos(bm_fold(s.name),bm_fold(%s))>0 OR strpos(bm_fold(s.url),bm_fold(%s))>0)"
            )
            args += [q, q]
        if status in ("active", "inactive"):
            where.append("s.active=%s")
            args.append(int(status == "active"))
        if country:
            where.append("s.country=%s")
            args.append(country)
        predicate = " AND ".join(where)
        with self.connection() as db:
            count = db.execute(f"SELECT COUNT(*) AS n FROM bm_sites s WHERE {predicate}", args)
            total = count.fetchone()
            assert total
            rows = db.execute(
                "SELECT s.id,s.identity,s.name,s.url,s.country,s.language,s.notes,s.active,"
                "s.deleted_at,s.version,s.origin,s.created_at,s.updated_at,"
                "COALESCE(e.total,0) AS endpoints,COALESCE(e.rss,0) AS rss,"
                "COALESCE(e.sitemaps,0) AS sitemaps,COALESCE(e.enabled,0)*s.active AS enabled "
                "FROM bm_sites s LEFT JOIN (SELECT site_id,COUNT(*)::int AS total,"
                "SUM((kind='feed')::int)::int AS rss,SUM((kind='sitemap')::int)::int AS sitemaps,"
                "SUM(active)::int AS enabled FROM bm_links WHERE deleted_at IS NULL "
                f"GROUP BY site_id) e ON e.site_id=s.id WHERE {predicate} "
                "ORDER BY lower(s.name),s.id LIMIT %s OFFSET %s",
                [*args, limit, offset],
            ).fetchall()
            return dict(
                items=[_plain(r) for r in rows], total=total["n"], offset=offset, limit=limit
            )

    def source(self, ident: int) -> dict[str, Any]:
        """Dados editáveis de uma fonte visível."""
        with self.connection() as db:
            return self._site(db, ident)

    def endpoints(
        self,
        source_id: int,
        q: str = "",
        status: str = "all",
        kind: str = "all",
        offset: int = 0,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Pagina os links de uma fonte, com prova própria e estado efetivo."""
        with self.connection() as db:
            site = self._site(db, source_id)
            where = ["l.site_id=%s", "l.deleted_at IS NULL"]
            args: list[Any] = [source_id]
            if q:
                where.append(
                    "(strpos(bm_fold(l.url),bm_fold(%s))>0 "
                    "OR strpos(bm_fold(l.label),bm_fold(%s))>0)"
                )
                args += [q, q]
            if status in ("active", "inactive"):
                where.append("l.active=%s")
                args.append(int(status == "active"))
            if kind in ("feed", "sitemap"):
                where.append("l.kind=%s")
                args.append(kind)
            predicate = " AND ".join(where)
            count = db.execute(f"SELECT COUNT(*) AS n FROM bm_links l WHERE {predicate}", args)
            total = count.fetchone()
            assert total
            rows = db.execute(
                f"{_LINK_SELECT} WHERE {predicate} ORDER BY l.kind,l.url,l.id LIMIT %s OFFSET %s",
                [*args, limit, offset],
            ).fetchall()
            versions = self._model_versions(db)
            return dict(
                source=site,
                total=total["n"],
                offset=offset,
                limit=limit,
                items=[
                    self._endpoint(r, bool(site["active"]), versions[r["model"]], site["url"])
                    for r in rows
                ],
            )

    def endpoint(self, ident: int) -> dict[str, Any]:
        """Um link para testar ou exibir, recusando excluídos."""
        with self.connection() as db:
            row = self._link(db, ident)
            site = self._site(db, row["source_id"])
            return self._endpoint(
                row, bool(site["active"]), self._model_versions(db)[row["model"]], site["url"]
            )

    # --- escrita -------------------------------------------------------------------------------

    def save(
        self,
        entity: str,
        data: dict[str, Any],
        ident: int | None = None,
        source_id: int | None = None,
        test_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Cria ou altera um cadastro atomicamente, com revisão otimista e auditoria."""
        is_site = entity == "source"
        if data.keys() - (_SITE_FIELDS if is_site else _LINK_FIELDS):
            raise CatalogError("O cadastro contém campos desconhecidos.")
        try:
            with self.connection() as db:
                db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
                before = (
                    (self._site(db, ident) if is_site else self._link(db, ident)) if ident else None
                )
                if before and (
                    type(data.get("version")) is not int or data["version"] != before["version"]
                ):
                    raise CatalogError("Este registro mudou. Atualize a tela antes de salvar.", 409)
                if is_site:
                    saved = self._write_site(db, data, ident, before)
                else:
                    saved = self._write_link(db, data, ident, source_id, before, test_result)
                self._event(
                    db, entity, saved["id"], "update" if before else "create", before, saved
                )
                return saved
        except pg_errors.UniqueViolation as error:
            raise CatalogError(
                "Este site ou link já está cadastrado, inclusive entre excluídos.", 409
            ) from error

    def delete(self, entity: str, ident: int, version: object) -> None:
        """Oculta um registro sem apagar histórico; fonte excluída oculta também seus links."""
        is_site = entity == "source"
        with self.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
            before = self._site(db, ident) if is_site else self._link(db, ident)
            if not is_site:
                self._site(db, before["source_id"])
            if type(version) is not int or before["version"] != version:
                raise CatalogError("Este registro mudou. Atualize a tela antes de excluir.", 409)
            if is_site:
                db.execute(
                    "UPDATE bm_sites SET deleted_at=now(),active=0,updated_at=now(),"
                    "version=version+1 WHERE id=%s",
                    (ident,),
                )
                after = self._site(db, ident, visible=False)
            else:
                db.execute(
                    "UPDATE bm_links SET deleted_at=now(),active=0,deactivated_by='user',"
                    "deactivated_reason='delete',deactivated_at=now(),updated_at=now(),"
                    "version=version+1 WHERE id=%s",
                    (ident,),
                )
                after = self._link(db, ident, visible=False)
            self._event(db, entity, ident, "delete", before, after, actor="user")

    def record_test(self, ident: int, version: int, result: dict[str, Any]) -> int:
        """Aplica um teste só à revisão que o iniciou; nunca sobrescreve uma edição posterior."""
        with self.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
            link = self._link(db, ident)
            if link["version"] != version:
                raise CatalogError("Este registro mudou. Atualize a tela antes de salvar.", 409)
            site = self._site(db, link["source_id"])
            self._apply_test(db, link, site, result)
            return int(link["version"])

    # --- mecanismos ----------------------------------------------------------------------------

    def models(self) -> list[dict[str, Any]]:
        """Lista mecanismos com parâmetros persistidos e uso no catálogo visível."""
        with self.connection() as db:
            records = {r["model"]: r for r in db.execute("SELECT * FROM bm_mechanisms")}
            counts = {
                r["mechanism"]: r["n"]
                for r in db.execute(
                    "SELECT l.mechanism,COUNT(*) AS n FROM bm_links l JOIN bm_sites s "
                    "ON s.id=l.site_id WHERE l.deleted_at IS NULL AND s.deleted_at IS NULL "
                    "GROUP BY l.mechanism"
                )
            }
            return [
                dict(
                    **definition,
                    parameters=get_mechanism(definition["id"]).validate(
                        records[definition["id"]]["parameters"]
                    ),
                    version=records[definition["id"]]["version"],
                    updated_at=_iso(records[definition["id"]]["updated_at"]),
                    endpoints=counts.get(definition["id"], 0),
                )
                for definition in list_models()
            ]

    def model_settings(self, model: object) -> dict[str, Any]:
        """Cópia validada da configuração de um mecanismo e sua revisão."""
        mechanism = get_mechanism(model)
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM bm_mechanisms WHERE model=%s", (mechanism.id,)
            ).fetchone()
            assert row
            return dict(
                id=mechanism.id,
                version=row["version"],
                updated_at=_iso(row["updated_at"]),
                parameters=mechanism.validate(row["parameters"]),
            )

    def save_model(self, model: str, data: dict[str, Any]) -> dict[str, Any]:
        """Atualiza parâmetros globais com revisão otimista, sem tocar nos links."""
        mechanism = get_mechanism(model)
        if data.keys() - {"parameters", "version"} or not isinstance(data.get("parameters"), dict):
            raise CatalogError("Informe apenas os parâmetros do mecanismo e sua revisão.")
        with self.connection() as db:
            db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
            before = db.execute(
                "SELECT * FROM bm_mechanisms WHERE model=%s", (mechanism.id,)
            ).fetchone()
            assert before
            if type(data.get("version")) is not int or data["version"] != before["version"]:
                raise CatalogError("A configuração mudou. Atualize antes de salvar.", 409)
            parameters = mechanism.validate({**before["parameters"], **data["parameters"]})
            if parameters != before["parameters"]:
                db.execute(
                    "UPDATE bm_mechanisms SET parameters=%s,version=version+1,updated_at=now() "
                    "WHERE model=%s",
                    (Jsonb(parameters), mechanism.id),
                )
                db.execute(
                    "INSERT INTO bm_events(entity,action,actor,changes) "
                    "VALUES('mechanism','update','user',%s)",
                    (Jsonb({"model": mechanism.id, **_diff(before, {"parameters": parameters})}),),
                )
        return self.model_settings(mechanism.id)

    # --- internos ------------------------------------------------------------------------------

    @staticmethod
    def _site(
        db: psycopg.Connection[dict[str, Any]], ident: int, visible: bool = True
    ) -> dict[str, Any]:
        suffix = " AND deleted_at IS NULL" if visible else ""
        row = db.execute(
            f"SELECT {_SITE_COLUMNS} FROM bm_sites WHERE id=%s{suffix}", (ident,)
        ).fetchone()
        if row is None:
            raise CatalogError("Registro não encontrado ou excluído.", 404)
        return _plain(row)

    @staticmethod
    def _link(
        db: psycopg.Connection[dict[str, Any]], ident: int, visible: bool = True
    ) -> dict[str, Any]:
        suffix = " AND l.deleted_at IS NULL" if visible else ""
        row = db.execute(f"{_LINK_SELECT} WHERE l.id=%s{suffix}", (ident,)).fetchone()
        if row is None:
            raise CatalogError("Registro não encontrado ou excluído.", 404)
        return _plain(row)

    @staticmethod
    def _stored(row: dict[str, Any] | None) -> dict[str, Any]:
        """Valores já gravados, com ``active`` como booleano para passar pela mesma validação."""
        return {**row, "active": bool(row["active"])} if row else {}

    @staticmethod
    def _model_versions(db: psycopg.Connection[dict[str, Any]]) -> dict[str, int]:
        return {
            r["model"]: r["version"] for r in db.execute("SELECT model,version FROM bm_mechanisms")
        }

    @staticmethod
    def _endpoint(
        row: dict[str, Any], site_active: bool, model_version: int, site_url: str
    ) -> dict[str, Any]:
        result = dict(row)
        last = result["last_test"]
        result["current_model_version"] = model_version
        result["test_config_current"] = bool(last) and (
            last.get("model_version") == model_version
            and last.get("source_url") == site_url
            and last.get("access_transport", "direct") == result["access_transport"]
        )
        result["active"] = bool(result["active"])
        result["effective_active"] = result["active"] and site_active
        return result

    def _write_site(
        self,
        db: psycopg.Connection[dict[str, Any]],
        data: dict[str, Any],
        ident: int | None,
        before: dict[str, Any] | None,
    ) -> dict[str, Any]:
        merged = {**self._stored(before), **data}
        url = clean_url(merged.get("url"), source=True)
        parts = urlsplit(url)
        values: dict[str, Any] = dict(
            url=url,
            notes=_text(merged, "notes", 4000),
            active=_active(merged),
            name=_text(merged, "name", 200, True),
            country=_text(merged, "country", 100),
            language=_text(merged, "language", 60),
            identity=(parts.hostname or "").removeprefix("www.") + parts.path,
        )
        if before:
            assignments = ",".join(f"{key}=%s" for key in values)
            db.execute(
                f"UPDATE bm_sites SET {assignments},version=version+1,updated_at=now() WHERE id=%s",
                [*values.values(), ident],
            )
        else:
            names = ",".join(values)
            marks = ",".join(["%s"] * len(values))
            row = db.execute(
                f"INSERT INTO bm_sites({names}) VALUES({marks}) RETURNING id", list(values.values())
            ).fetchone()
            assert row
            ident = row["id"]
        return self._site(db, int(ident or 0))

    def _write_link(
        self,
        db: psycopg.Connection[dict[str, Any]],
        data: dict[str, Any],
        ident: int | None,
        source_id: int | None,
        before: dict[str, Any] | None,
        test_result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        merged = {**self._stored(before), **data}
        site_id = before["source_id"] if before else source_id
        site = self._site(db, site_id or 0)
        kind = merged.get("kind")
        if kind not in ("feed", "sitemap"):
            raise CatalogError("Selecione RSS/Atom ou sitemap.")
        model = merged.get("model", "auto")
        if before and kind != before["kind"] and "model" not in data:
            model = "auto"
        get_model(model, kind)
        interval = merged.get("search_interval_seconds", 14400)
        if type(interval) is not int or not 1 <= interval <= 9223372036854775807:
            raise CatalogError(
                "O intervalo de busca deve ser um número inteiro positivo de segundos."
            )
        try:
            transport = validate_transport(merged.get("access_transport", "direct"))
        except ValueError as error:
            raise CatalogError(str(error)) from error
        read_order = merged.get("read_order", UNKNOWN)
        if read_order not in READ_ORDERS:
            raise CatalogError("Ordem de leitura inválida.")
        url = clean_url(merged.get("url"))
        proof_is_stale = bool(
            before
            and (
                url != before["url"]
                or kind != before["kind"]
                or model != before["model"]
                or transport != before["access_transport"]
            )
        )
        if proof_is_stale and "read_order" not in data:
            read_order = UNKNOWN
        values: dict[str, Any] = dict(
            url=url,
            kind=kind,
            mechanism=model,
            label=_text(merged, "label", 200),
            notes=_text(merged, "notes", 4000),
            active=_active(merged),
            search_interval_seconds=interval,
            access_transport=transport,
            read_order=read_order,
        )
        if before:
            assignments = ",".join(f"{key}=%s" for key in values)
            db.execute(
                f"UPDATE bm_links SET {assignments},version=version+1,updated_at=now() WHERE id=%s",
                [*values.values(), ident],
            )
        else:
            names = ",".join([*values, "site_id"])
            marks = ",".join(["%s"] * (len(values) + 1))
            row = db.execute(
                f"INSERT INTO bm_links({names}) VALUES({marks}) RETURNING id",
                [*values.values(), site_id],
            ).fetchone()
            assert row
            ident = row["id"]
        if proof_is_stale:
            db.execute(
                "UPDATE bm_link_state SET last_test='{}',evidence='{}',updated_at=now() "
                "WHERE link_id=%s",
                (ident,),
            )
        saved = self._link(db, int(ident or 0))
        if test_result is not None:
            self._apply_test(db, saved, site, test_result)
            saved = self._link(db, int(ident or 0))
        return saved

    def _apply_test(
        self,
        db: psycopg.Connection[dict[str, Any]],
        link: dict[str, Any],
        site: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Valida que a prova pertence à configuração atual e a grava no estado do link."""
        model_version = self._model_versions(db)[link["model"]]
        if result.get("model_version") != model_version:
            raise CatalogError("A configuração do mecanismo mudou. Execute um novo teste.", 409)
        if (
            result["url"] != link["url"]
            or result["model"] != link["model"]
            or result["source_id"] != link["source_id"]
            or result["requested_kind"] != link["kind"]
            or result["source_url"] != site["url"]
            or result.get("access_transport", "direct") != link["access_transport"]
        ):
            raise CatalogError("O teste pertence a outra configuração. Teste novamente.")
        proven = bool(result.get("xml_valid") and result.get("compatible"))
        evidence = self._evidence(result) if proven else None
        db.execute(
            "INSERT INTO bm_link_state(link_id,last_test,evidence) VALUES(%s,%s,COALESCE(%s,'{}')) "
            "ON CONFLICT (link_id) DO UPDATE SET last_test=EXCLUDED.last_test,"
            "evidence=CASE WHEN %s THEN EXCLUDED.evidence ELSE bm_link_state.evidence END,"
            "updated_at=now()",
            (
                link["id"],
                Jsonb(result),
                Jsonb(evidence) if evidence is not None else None,
                proven,
            ),
        )
        if proven and result.get("read_order") in READ_ORDERS:
            db.execute(
                "UPDATE bm_links SET read_order=%s WHERE id=%s",
                (result["read_order"], link["id"]),
            )

    @staticmethod
    def _evidence(result: dict[str, Any]) -> dict[str, Any]:
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

    @staticmethod
    def _event(
        db: psycopg.Connection[dict[str, Any]],
        entity: str,
        ident: int | None,
        action: str,
        before: dict[str, Any] | None,
        after: dict[str, Any],
        actor: str = "user",
    ) -> None:
        watched = {
            key: value
            for key, value in after.items()
            if key not in ("evidence", "last_test", "deactivated_at", "deleted_at")
            or key == "deleted_at"
        }
        db.execute(
            "INSERT INTO bm_events(entity,entity_id,action,actor,changes) VALUES(%s,%s,%s,%s,%s)",
            (entity, ident, action, actor, Jsonb(_diff(before, watched))),
        )
