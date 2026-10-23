"""Auditoria finita do catálogo local; sem downloads nem descoberta de novos caminhos."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from unidecode import unidecode

from buscador_midias.catalog.models import get_model
from buscador_midias.catalog.store import CatalogError, CatalogStore, clean_url

# Decisões explícitas: seções/edições regionais no mesmo portal e aliases confirmados.
# Idiomas distintos, marcas editoriais e emissoras ficam separados.
_REVIEWED = [
    ("https://www1.folha.uol.com.br", "https://www.folha.uol.com.br", "alias confirmado"),
    ("https://lupa.uol.com.br", "https://www.agencialupa.org", "redirecionamento oficial"),
    ("https://www.estadao.com.br/estadao-verifica", "https://www.estadao.com.br", "seção"),
    ("https://www.uol.com.br/tilt", "https://www.uol.com.br", "seção"),
    ("https://g1.globo.com/fato-ou-fake", "https://g1.globo.com", "seção"),
    ("https://elpais.com/america", "https://elpais.com", "edição regional"),
    ("https://www.theguardian.com/au", "https://www.theguardian.com", "edição regional"),
    ("https://theconversation.com/au", "https://theconversation.com", "edição regional"),
    ("https://www.bbc.com/news/world/africa", "https://www.bbc.com/news", "seção regional"),
    ("https://www.semafor.com/newsletters/africa", "https://www.semafor.com", "seção regional"),
]


def reviewed_plan(store: CatalogStore) -> list[dict[str, Any]]:
    """Gera plano estrito; revisões de fontes e todos os links protegem edições concorrentes."""
    with store.connection() as db:
        sources = {
            r["url"]: dict(r) for r in db.execute("SELECT * FROM sites WHERE deleted_at IS NULL")
        }
        result = []
        for old_url, target_url, reason in _REVIEWED:
            if old_url not in sources or target_url not in sources:
                continue
            old, target = sources[old_url], sources[target_url]
            versions = [
                tuple(r)
                for r in db.execute(
                    "SELECT id,version,deleted_at FROM links WHERE source_id IN (?,?) ORDER BY id",
                    (old["id"], target["id"]),
                )
            ]
            result.append(
                dict(
                    source_id=old["id"],
                    target_id=target["id"],
                    version=old["version"],
                    target_version=target["version"],
                    name=old["name"],
                    url=old_url,
                    reason=reason,
                    endpoints_revision=hashlib.sha256(json.dumps(versions).encode()).hexdigest(),
                )
            )
        return result


def audit_catalog(store: CatalogStore) -> dict[str, Any]:
    """Inspeciona todas as fontes/associações; referências externas não são erros presumidos."""
    with store.connection() as db:
        integrity = [r[0] for r in db.execute("PRAGMA integrity_check")]
        foreign_keys = [tuple(r) for r in db.execute("PRAGMA foreign_key_check")]
        sources = [
            dict(r) for r in db.execute("SELECT * FROM sites WHERE deleted_at IS NULL ORDER BY id")
        ]
        links = [
            dict(r)
            for r in db.execute(
                "SELECT e.* FROM links e JOIN sites s ON s.id=e.source_id "
                "WHERE e.deleted_at IS NULL AND s.deleted_at IS NULL ORDER BY e.id"
            )
        ]
    by_id = {s["id"]: s for s in sources}
    urls: dict[str, list[int]] = defaultdict(list)
    names: dict[str, list[int]] = defaultdict(list)
    hosts: dict[str, list[int]] = defaultdict(list)
    endpoint_urls: dict[str, list[dict[str, Any]]] = defaultdict(list)
    normalized: dict[tuple[int, str], list[int]] = defaultdict(list)
    issues, external, without_xml, models = [], [], [], []
    counts: dict[int, int] = defaultdict(int)
    for source in sources:
        parts = urlsplit(source["url"])
        host = (parts.hostname or "").removeprefix("www.")
        urls[host + parts.path.rstrip("/")].append(source["id"])
        names[unidecode(source["name"]).casefold().strip()].append(source["id"])
        hosts[host].append(source["id"])
        try:
            clean_url(source["url"], source=True)
        except CatalogError as error:
            issues.append(dict(entity="source", id=source["id"], reason=str(error)))
    for link in links:
        counts[link["source_id"]] += 1
        endpoint_urls[link["url"]].append(dict(id=link["id"], source_id=link["source_id"]))
        try:
            clean_url(link["url"])
            get_model(link["model"], link["kind"])
        except CatalogError as error:
            issues.append(dict(entity="endpoint", id=link["id"], reason=str(error)))
        parts = urlsplit(link["url"])
        # Só sinaliza variantes; não presume equivalência de barra, esquema, www ou query.
        path = quote(unquote(parts.path), safe="/@:!$&'()*+,;=-._~")
        key = urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))
        normalized[(link["source_id"], key)].append(link["id"])
        source = by_id[link["source_id"]]
        host = (urlsplit(source["url"]).hostname or "").removeprefix("www.")
        endpoint_host = (parts.hostname or "").removeprefix("www.")
        if not (endpoint_host == host or endpoint_host.endswith("." + host)):
            external.append(dict(id=link["id"], source_id=source["id"], url=link["url"]))
        evidence = json.loads(link["evidence"])
        if not evidence:
            without_xml.append(link["id"])
        if (link["model"] == "rss" and link["kind"] != "feed") or (
            link["model"] in {"sitemap", "sitemap_tree", "news_sitemap"}
            and link["kind"] != "sitemap"
        ):
            models.append(link["id"])
    return dict(
        sources=len(sources),
        endpoints=len(links),
        unique_endpoint_urls=len(endpoint_urls),
        integrity=integrity,
        foreign_key_violations=foreign_keys,
        invalid_records=issues,
        same_source_addresses=[ids for ids in urls.values() if len(ids) > 1],
        same_names=[ids for ids in names.values() if len(ids) > 1],
        shared_hosts=[ids for ids in hosts.values() if len(ids) > 1],
        shared_endpoint_urls=[
            dict(url=url, associations=items)
            for url, items in endpoint_urls.items()
            if len(items) > 1
        ],
        encoded_url_candidates=[ids for ids in normalized.values() if len(ids) > 1],
        external_endpoint_hosts=external,
        without_xml_evidence=without_xml,
        incompatible_kind_model=models,
        sources_without_links=[s["id"] for s in sources if not counts[s["id"]]],
        source_inventory=[
            dict(
                id=s["id"],
                name=s["name"],
                url=s["url"],
                country=s["country"],
                language=s["language"],
                endpoints=counts[s["id"]],
            )
            for s in sources
        ],
    )


def repair_url_plan(store: CatalogStore) -> list[dict[str, Any]]:
    """Codifica apenas espaços literais no caminho, sem adivinhar endereços alternativos."""
    with store.connection() as db:
        result = []
        for row in db.execute(
            "SELECT e.* FROM links e JOIN sites s ON s.id=e.source_id "
            "WHERE e.deleted_at IS NULL AND s.deleted_at IS NULL ORDER BY e.id"
        ):
            parts = urlsplit(row["url"])
            if " " not in parts.path:
                continue
            url = urlunsplit(
                (
                    parts.scheme,
                    parts.netloc,
                    parts.path.replace(" ", "%20"),
                    parts.query,
                    parts.fragment,
                )
            )
            try:
                url = clean_url(url)
            except CatalogError:
                continue
            if db.execute(
                "SELECT 1 FROM links WHERE source_id=? AND url=?", (row["source_id"], url)
            ).fetchone():
                continue
            result.append(
                dict(id=row["id"], version=row["version"], old_url=row["url"], new_url=url)
            )
        return result


def repair_malformed_links(store: CatalogStore, plan: list[dict[str, Any]]) -> int:
    """Corrige a representação da URL com auditoria; a nova configuração precisa de teste."""
    from buscador_midias.catalog.store import _stamp

    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if plan != repair_url_plan(store):
            raise CatalogError("O catálogo mudou. Gere novamente o plano de correção.", 409)
        for item in plan:
            before = store._record(db, "links", item["id"])
            db.execute(
                "UPDATE links SET url=?,evidence='{}',last_test='{}',"
                "version=version+1,updated_at=? WHERE id=?",
                (item["new_url"], _stamp(), item["id"]),
            )
            after = dict(db.execute("SELECT * FROM links WHERE id=?", (item["id"],)).fetchone())
            store._event(db, "endpoint", item["id"], "repair_url", before, after)
    return len(plan)
