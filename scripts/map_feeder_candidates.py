"""Descobre RSS no diretório público do Feeder e comprova notícia/evidência antes de ativar.

Rodada finita: não usa endpoints privados do Feeder, serviços pagos ou ingestão contínua.
O diretório apenas sugere URLs; toda confirmação ocorre no veículo de origem.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from lxml import html
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from buscador_midias.catalog.editorial_mapping import normalized, recent_samples
from buscador_midias.catalog.health_restoration import automatic_event
from buscador_midias.catalog.http_transport import make_reader
from buscador_midias.catalog.postgres import WRITE_LOCK, connect_postgres
from buscador_midias.catalog.store import CatalogStore
from buscador_midias.catalog.testing import run_test
from buscador_midias.domain.site import Site
from buscador_midias.evidence.capture import EvidenceCapturer
from buscador_midias.evidence.storage import LocalEvidenceStore
from buscador_midias.extraction.fetch import PageFetch, PageStatus
from buscador_midias.state.store import FindingUrl
from map_editorial_priority import credentials, write
from probe_priority_news_evidence import RecordingReader, editorial

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/gerenciador/mapeamento-feeder-20261026"
RUN = "feeder-20261026"
LOCK = threading.Lock()
# Distribuição externa conhecida; não torna feeds de outros terceiros automaticamente oficiais.
DISTRIBUTORS = {
    "wsj.com": ("feeds.content.dowjones.io", "feeds.a.dj.com"),
    "bbc.com": ("feeds.bbci.co.uk",),
    "bbc.co.uk": ("feeds.bbci.co.uk",),
}


def site_object(source):
    """Reconstrói o escopo editorial: domínio e prefixo da fonte são obrigatórios."""
    return Site.create(
        name=source["name"],
        url=source["url"],
        region="",
        country="",
        country_code="",
        language="",
        source_type="",
    )


def feed_origin(url, source):
    """Classifica a origem sem confundir agregadores/podcasts com RSS do veículo."""
    parts = urlsplit(url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        return "invalid"
    if site_object(source).owns_host(url):
        return "publisher_domain"
    base = (urlsplit(source["url"]).hostname or "").removeprefix("www.")
    if parts.hostname in DISTRIBUTORS.get(base, ()):
        return "known_distributor"
    if parts.hostname == "news.google.com":
        return "aggregator"
    return "third_party"


def extract_candidates(body, source):
    """Extrai somente URLs exibidas como feeds; nunca interpreta a homepage como RSS."""
    document = html.fromstring(body)
    found = {}
    for item in document.xpath(
        '//span[contains(concat(" ", normalize-space(@class), " "), " discover-feeds__text ")]'
    ):
        titles = item.xpath("./a/text()")
        label = "".join(titles).strip()
        small = item.xpath("./small[1]")
        if not small:
            continue
        text = "".join(small[0].itertext()).strip()
        if not text.startswith(("https://", "http://")):
            # "Feeder builds this feed from ..." não é um endereço RSS exportável.
            continue
        url = text.split()[0]
        status = " ".join(item.xpath('.//small[contains(@class,"meta")]//text()')).strip()
        found.setdefault(
            normalized(url),
            dict(
                url=url,
                label=label,
                feeder_status=status,
                feeder_trouble=bool(
                    item.xpath('.//*[contains(@class,"discover-status--trouble")]')
                ),
                origin=feed_origin(url, source),
            ),
        )
    return list(found.values())


def freeze():
    """Congela fontes, links e mecanismos; consultas não alteram o banco de produção."""
    ids = [
        r["source_id"]
        for r in json.loads(
            (
                ROOT
                / "var/gerenciador/recuperacao-transportes-20261026/cobertura-prioritarios.json"
            ).read_text()
        )
    ]
    with connect_postgres(credentials()) as db:
        db.row_factory = dict_row
        db.execute("SET TRANSACTION READ ONLY")
        sources = db.execute(
            "SELECT * FROM midias_manager.sites WHERE id=ANY(%s) ORDER BY id", (ids,)
        ).fetchall()
        links = db.execute("SELECT * FROM midias_manager.links").fetchall()
        models = db.execute("SELECT * FROM midias_manager.mechanisms").fetchall()
    result = dict(
        id=RUN,
        started_at=datetime.now(UTC).isoformat(),
        sources=sources,
        links=links,
        models=models,
        limits=dict(
            workers=8,
            candidates_per_source=8,
            seconds_per_source=120,
            seconds_per_test=35,
            article_samples=3,
            transport_attempts=3,
            max_evidence_bytes=80 * 1024 * 1024,
        ),
        services=dict(bright_data=False, openrouter=False),
        corpus_writes=False,
    )
    with gzip.open(OUT / "manifest.json.gz", "wt") as handle:
        json.dump(result, handle, ensure_ascii=False, default=str)
    return result


def discover(source):
    """Consulta uma página pública por fonte e preserva o HTML de procedência."""
    path = OUT / "discovery" / f"{source['id']}.json"
    if path.exists():
        return json.loads(path.read_text())
    host = (urlsplit(source["url"]).hostname or "").removeprefix("www.")
    url = f"https://feeder.co/discover/site/{host}"
    reader = make_reader(
        ["https://feeder.co"],
        lambda _: None,
        transport="curl_chrome_h2",
        seconds=15,
        requests=3,
        max_bytes=2 * 1024 * 1024,
    )
    try:
        response = reader.get(url)
        result = dict(
            source_id=source["id"],
            source_name=source["name"],
            provenance=url,
            status=response.status,
            error=response.error,
            observed_at=datetime.now(UTC).isoformat(),
            candidates=[],
        )
        if response.ok:
            result["candidates"] = extract_candidates(response.content, source)
            folder = OUT / "feeder-pages"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{source['id']}.html.gz").write_bytes(gzip.compress(response.content))
    except Exception as exc:
        result = dict(
            source_id=source["id"],
            provenance=url,
            error=f"{type(exc).__name__}: {exc}",
            candidates=[],
        )
    finally:
        reader.close()
    write(path, result)
    return result


def candidates_for(source, discovered, manifest):
    """Registra duplicatas, terceiros e problemas sem ativá-los ou descartá-los do histórico."""
    existing = {
        normalized(link["url"]): link for link in manifest["links"] if link["deleted_at"] is None
    }
    own = {
        normalized(link["url"]): link
        for link in manifest["links"]
        if link["source_id"] == source["id"]
    }
    rows = []
    for c in discovered["candidates"]:
        row = dict(c, source_id=source["id"], provenance=discovered["provenance"])
        match = existing.get(normalized(c["url"]))
        row["existing_id"] = (match or own.get(normalized(c["url"])) or {}).get("id")
        if source["id"] == 25:
            row["decision"] = "source_identity_review"
        elif c["origin"] not in ("publisher_domain", "known_distributor"):
            row["decision"] = "third_party_review"
        elif not editorial(c["url"]) or any(
            word in c["label"].lower() for word in ("podcast", "audio briefing", "minute briefing")
        ):
            row["decision"] = "non_editorial"
        elif match and match["source_id"] != source["id"]:
            row["decision"] = "duplicate_other_source"
        elif match and match["active"] and (match.get("last_test") or {}).get("usable_for_news"):
            row["decision"] = "already_functional"
        else:
            row["decision"] = "candidate"
        rows.append(row)
    return sorted(
        rows, key=lambda c: (c["feeder_trouble"], "opinion" in c["label"].lower(), c["url"])
    )


def probe(source, discovery, manifest):
    """Faz uma única rodada limitada; HTTP 200 do XML não aprova um link sozinho."""
    path = OUT / "sources" / f"{source['id']}.json"
    if path.exists():
        return json.loads(path.read_text())
    candidates = candidates_for(source, discovery, manifest)
    result = dict(
        source=source, candidates=candidates, results=[], started_at=datetime.now(UTC).isoformat()
    )
    model = next(m for m in manifest["models"] if m["model"] == "rss")
    limits = manifest.get("probe_limits", {})
    candidate_limit = limits.get("max_candidates", 8)
    source_seconds = limits.get("source_seconds", 120)
    test_seconds = limits.get("test_seconds", 35)
    storage_bytes = limits.get("storage_bytes", 80 * 1024 * 1024)
    started = time.monotonic()
    tested = 0
    for candidate in candidates:
        if candidate["decision"] != "candidate":
            continue
        if tested >= candidate_limit or time.monotonic() - started >= source_seconds:
            candidate["decision"] = "budget_deferred"
            continue
        tested += 1
        row = dict(candidate=candidate, tests=[], evidence=[], confirmed=False)
        for profile in ("curl_chrome_h2", "curl_chrome_h1", "curl_firefox_h2"):
            remaining = source_seconds - (time.monotonic() - started)
            if remaining < 5:
                break
            reader = RecordingReader([source["url"], candidate["url"]], lambda _: None, profile)
            reader.inner.deadline = min(
                reader.inner.deadline, time.monotonic() + min(remaining, test_seconds)
            )
            settings = dict(
                model["parameters"],
                lookback_days=0,
                timeout_seconds=test_seconds,
                max_requests=24,
                article_samples=3,
                sample_order="first",
                max_response_mib=4,
            )
            config = dict(
                test_id=uuid.uuid4().hex,
                url=candidate["url"],
                kind="feed",
                model="rss",
                model_version=model["version"],
                parameters=settings,
                access_transport=profile,
                bright_data=False,
            )
            try:
                test = run_test(
                    config, source, lambda _: None, factory=lambda _r, _p, reader=reader: reader
                )
                row["tests"].append(test)
                for sample in test["samples"]:
                    if not (
                        sample.get("text") and sample.get("article") and editorial(sample["url"])
                    ):
                        continue
                    page = reader.pages.get(sample["url"])
                    if not page or not site_object(source).owns(page.final_url or sample["url"]):
                        continue
                    identity = sample["url"]
                    if limits.get("immutable_evidence"):
                        identity += "\0" + hashlib.sha256(page.content).hexdigest()
                    digest = hashlib.sha256(identity.encode()).hexdigest()
                    with LOCK:
                        used = sum(p.stat().st_size for p in (OUT / "evidence").rglob("*.html"))
                        if used + len(page.content) + 1024 > storage_bytes:
                            row["storage_limit"] = True
                            break
                        record = EvidenceCapturer(
                            LocalEvidenceStore(OUT / "evidence"),
                            bright_data=None,
                            reuse=True,
                            now=lambda: datetime.now(UTC),
                        ).capture(
                            FindingUrl(
                                digest, sample["url"], sample["title"], str(source["id"]), ()
                            ),
                            PageFetch(
                                sample["url"],
                                PageStatus.OK,
                                page.status,
                                page.content,
                                page.content_type,
                                page.final_url,
                            ),
                            expected_headline=sample["title"],
                        )
                    row["evidence"].append(
                        dict(
                            **asdict(record),
                            url=sample["url"],
                            title=sample["title"],
                            published_at=sample.get("published_at"),
                        )
                    )
                    if record.status == "captured":
                        assert (
                            hashlib.sha256(
                                (OUT / "evidence" / record.path).read_bytes()
                            ).hexdigest()
                            == record.sha256
                        )
                        row.update(confirmed=True, result=test, profile=profile)
                        break
                if row["confirmed"]:
                    break
                # Uma resposta XML funcional não precisa ser repetida por não ter notícias.
                if test.get("xml_valid") and not test.get("news_urls"):
                    break
                if test.get("endpoint_status") == 429 or test.get("retry_after_seconds"):
                    break
            except Exception as exc:
                row["tests"].append(
                    dict(
                        url=candidate["url"],
                        error=f"{type(exc).__name__}: {exc}",
                        access_transport=profile,
                    )
                )
            finally:
                reader.close()
        candidate["decision"] = "confirmed" if row["confirmed"] else "not_confirmed"
        result["results"].append(row)
    result["finished_at"] = datetime.now(UTC).isoformat()
    write(path, result)
    return result


def as_json(value):
    """Serializa somente dados de auditoria; credenciais não entram nos artefatos."""
    return Jsonb(value, dumps=lambda x: json.dumps(x, ensure_ascii=False, default=str))


def event(db, entity, ident, action, before, after):
    """Mantém revisão anterior e identificação da rodada de descoberta."""
    db.execute(
        "INSERT INTO events(entity,entity_id,action,happened_at,before_json,after_json) "
        "VALUES(%s,%s,%s,now(),%s,%s)",
        (entity, ident, action, as_json(before), as_json(dict(after, feeder_mapping_run=RUN))),
    )


def apply_source(row, manifest):
    """Aplica somente provas válidas e revisões congeladas, preservando decisões manuais."""
    source = row["source"]
    frozen = {link["id"]: link for link in manifest["links"]}
    decisions = []
    with connect_postgres(credentials()) as db:
        db.row_factory = dict_row
        db.execute("SET LOCAL search_path TO midias_manager,pg_catalog")
        db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
        parent = db.execute(
            "SELECT * FROM sites WHERE id=%s FOR UPDATE", (source["id"],)
        ).fetchone()
        if not parent or parent["deleted_at"] or parent["version"] != source["version"]:
            return [dict(source_id=source["id"], action="source_revision_preserved")]
        if not parent["active"] and not automatic_event(
            db, "source", source["id"], "validation_inactivate", parent["version"]
        ):
            return [dict(source_id=source["id"], action="manual_source_state_preserved")]
        for item in row["results"]:
            if not item["confirmed"]:
                continue
            test = item["result"]
            candidate = item["candidate"]
            url = candidate["url"]
            proof = next((e for e in item["evidence"] if e["status"] == "captured"), None)
            if (
                not proof
                or not test.get("usable_for_news")
                or not site_object(source).owns(proof["url"])
            ):
                continue
            body = OUT / "evidence" / proof["path"]
            if hashlib.sha256(body.read_bytes()).hexdigest() != proof["sha256"]:
                raise ValueError("Integridade da evidência inválida")
            current_model = db.execute(
                "SELECT * FROM mechanisms WHERE model=%s", ("rss",)
            ).fetchone()
            if current_model["version"] != test["model_version"]:
                decisions.append(dict(url=url, action="model_revision_preserved"))
                continue
            all_links = db.execute(
                "SELECT id,source_id,url FROM links WHERE deleted_at IS NULL"
            ).fetchall()
            duplicate = next(
                (link for link in all_links if normalized(link["url"]) == normalized(url)), None
            )
            if duplicate and duplicate["source_id"] != source["id"]:
                decisions.append(dict(url=url, action="duplicate_preserved"))
                continue
            before = db.execute(
                "SELECT * FROM links WHERE source_id=%s AND url=%s FOR UPDATE", (source["id"], url)
            ).fetchone()
            if duplicate and (before is None or duplicate["id"] != before["id"]):
                decisions.append(dict(url=url, action="duplicate_preserved"))
                continue
            if before:
                snapshot = frozen.get(before["id"])
                if not snapshot or snapshot["version"] != before["version"]:
                    decisions.append(dict(url=url, action="link_revision_preserved"))
                    continue
                if before["deleted_at"] and not automatic_event(
                    db, "endpoint", before["id"], "validation_exclude", before["version"]
                ):
                    decisions.append(dict(url=url, action="manual_deletion_preserved"))
                    continue
                latest = db.execute(
                    "SELECT action FROM events WHERE entity='endpoint' AND "
                    "entity_id=%s ORDER BY id "
                    "DESC LIMIT 1",
                    (before["id"],),
                ).fetchone()
                if (
                    not before["active"]
                    and not before["deleted_at"]
                    and latest
                    and latest["action"] == "update"
                ):
                    decisions.append(dict(url=url, action="manual_inactivation_preserved"))
                    continue
            test = dict(test, feeder_provenance=candidate, evidence_capture=proof)
            if before:
                after = db.execute(
                    "UPDATE links SET "
                    "active=1,deleted_at=NULL,model=%s,kind=%s,access_transport=%s,"
                    "last_test=%s,evidence=%s,version=version+1,updated_at=now() "
                    "WHERE id=%s RETURNING *",
                    (
                        "rss",
                        "feed",
                        item["profile"],
                        as_json(test),
                        as_json(CatalogStore._test_evidence(test)),
                        before["id"],
                    ),
                ).fetchone()
                action = (
                    "feeder_restore"
                    if before["deleted_at"] or not before["active"]
                    else "feeder_verify"
                )
            else:
                after = db.execute(
                    "INSERT INTO "
                    "links(source_id,url,kind,model,access_transport,label,notes,"
                    "active,created_at,updated_at,last_test,evidence,origin,"
                    "search_interval_seconds) VALUES(%s,%s,%s,%s,%s,%s,%s,1,now(),"
                    "now(),%s,%s,%s,14400) RETURNING *",
                    (
                        source["id"],
                        url,
                        "feed",
                        "rss",
                        item["profile"],
                        candidate["label"],
                        f"Descoberto em {candidate['provenance']}. "
                        f"Origem: {candidate['origin']}. "
                        "XML, notícia textual e evidência validados em amostra; "
                        "não garante cobertura integral.",
                        as_json(test),
                        as_json(CatalogStore._test_evidence(test)),
                        RUN,
                    ),
                ).fetchone()
                action = "feeder_create"
            db.execute(
                "INSERT INTO "
                "link_test_results(link_id,model,test_id,model_version,"
                "observed_at,parameters,result) VALUES(%s,%s,%s,%s,%s,%s,%s) ON "
                "CONFLICT DO NOTHING",
                (
                    after["id"],
                    "rss",
                    test["test_id"],
                    test["model_version"],
                    test["observed_at"],
                    as_json(test["parameters"]),
                    as_json(test),
                ),
            )
            event(db, "endpoint", after["id"], action, before, after)
            # Apenas notícia com data recente comprovada habilita a rota diária automaticamente.
            daily = "updates" if recent_samples(test) else "review"
            current_route = db.execute(
                "SELECT * FROM ingest_daily_routes WHERE link_id=%s", (after["id"],)
            ).fetchone()
            route_after = db.execute(
                "INSERT INTO ingest_daily_routes(link_id,purpose,reason) VALUES(%s,%s,%s) ON "
                "CONFLICT DO NOTHING RETURNING *",
                (
                    after["id"],
                    daily,
                    "RSS descoberto via Feeder; prova textual e evidência preservadas.",
                ),
            ).fetchone()
            if route_after:
                event(db, "endpoint", after["id"], "feeder_daily_route", None, route_after)
            daily = current_route["purpose"] if current_route else daily
            decisions.append(
                dict(
                    source_id=source["id"],
                    url=url,
                    link_id=after["id"],
                    action=action,
                    purpose=daily,
                )
            )
        if any(d.get("link_id") for d in decisions) and not parent["active"]:
            after = db.execute(
                "UPDATE sites SET active=1,version=version+1,updated_at=now() WHERE id=%s "
                "RETURNING *",
                (source["id"],),
            ).fetchone()
            event(db, "source", source["id"], "feeder_activate_source", parent, after)
            decisions.append(dict(source_id=source["id"], action="source_activated"))
    return decisions


def main():
    """Retoma checkpoints e exige opção explícita para modificar o catálogo."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "manifest.json.gz"
    if path.exists():
        with gzip.open(path, "rt") as handle:
            manifest = json.load(handle)
    else:
        manifest = freeze()
    with ThreadPoolExecutor(max_workers=6) as pool:
        discoveries = list(pool.map(discover, manifest["sources"]))
    print(
        "DISCOVERY",
        dict(pages=len(discoveries), candidates=sum(len(d["candidates"]) for d in discoveries)),
        flush=True,
    )
    by_source = {d["source_id"]: d for d in discoveries}
    rows = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(probe, s, by_source[s["id"]], manifest) for s in manifest["sources"]]
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            print(
                json.dumps(
                    dict(
                        done=len(rows),
                        source=row["source"]["name"],
                        candidates=len(row["candidates"]),
                        tested=len(row["results"]),
                        confirmed=sum(r["confirmed"] for r in row["results"]),
                    ),
                    ensure_ascii=False,
                ),
                flush=True,
            )
    decisions = []
    if args.apply:
        for row in sorted(rows, key=lambda r: r["source"]["id"]):
            applied = OUT / "applied" / f"{row['source']['id']}.json"
            result = (
                json.loads(applied.read_text()) if applied.exists() else apply_source(row, manifest)
            )
            write(applied, result)
            decisions.extend(result)
    summary = dict(
        started_at=manifest["started_at"],
        finished_at=datetime.now(UTC).isoformat(),
        sources=len(rows),
        pages_accessible=sum(d.get("status") == 200 for d in discoveries),
        candidates=sum(len(r["candidates"]) for r in rows),
        decisions=dict(Counter(c["decision"] for row in rows for c in row["candidates"])),
        confirmed=sum(r["confirmed"] for row in rows for r in row["results"]),
        applied=dict(Counter(d["action"] for d in decisions)),
    )
    write(OUT / "summary.json", summary)
    write(OUT / "application.json", decisions)
    from report_feeder_mapping import build_report

    build_report(OUT)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
