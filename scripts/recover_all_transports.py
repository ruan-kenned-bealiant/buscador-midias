"""Rodada única dos excluídos automáticos e falhas de descoberta, com até três perfis curl."""

from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from psycopg.rows import dict_row

from buscador_midias.catalog.batch_reader import BatchCache
from buscador_midias.catalog.curl_reader import CurlReader
from buscador_midias.catalog.http_session import SESSIONS
from buscador_midias.catalog.mechanisms.parsing import parse_document
from buscador_midias.catalog.postgres import connect_postgres
from buscador_midias.catalog.reader import TestLimitError
from buscador_midias.catalog.testing import run_test
from buscador_midias.extraction.reliability import PageSnapshot
from buscador_midias.extraction.sample_audit import audit_page
from map_editorial_priority import credentials

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/gerenciador/recuperacao-transportes-20261026"
PROFILES = ("curl_chrome_h2", "curl_chrome_h1", "curl_firefox_h2")
CACHE = BatchCache()
LOCK = threading.Lock()
DISK_LOCK = threading.Lock()
PROOF_BYTES = 0
CONFIRMATIONS = {}
MAX_PROOF_BYTES = 160 * 1024 * 1024


def stamp():
    """Horário UTC da prova."""
    return datetime.now(UTC).isoformat()


def write_gz(path, data):
    """Comprime checkpoints para preservar espaço sem perder os resultados."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_bytes(
        gzip.compress(json.dumps(data, ensure_ascii=False, default=str).encode(), compresslevel=4)
    )
    temp.replace(path)


def read_gz(path):
    """Lê um checkpoint completo."""
    return json.loads(gzip.decompress(path.read_bytes()))


def freeze():
    """Congela a lista; não restaura exclusões manuais ou pais excluídos."""
    path = OUT / "manifest.json.gz"
    if path.exists():
        return read_gz(path)
    with connect_postgres(credentials()) as db:
        db.row_factory = dict_row
        db.execute("SET TRANSACTION READ ONLY")
        rows = db.execute("""
          SELECT l.id,l.url,l.kind,l.model,l.version,l.source_id,l.active,l.deleted_at,
            l.last_test->>'outcome' previous_outcome,s.name source_name,s.url source_url,
            s.version source_version,s.active source_active
          FROM midias_manager.links l JOIN midias_manager.sites s ON s.id=l.source_id
          WHERE s.deleted_at IS NULL AND (
            (l.deleted_at IS NOT NULL AND EXISTS(SELECT 1 FROM midias_manager.events e
              WHERE e.entity='endpoint' AND e.entity_id=l.id AND e.action='validation_exclude'
              AND e.id=(SELECT max(id) FROM midias_manager.events e2
                WHERE e2.entity='endpoint' AND e2.entity_id=l.id)))
            OR (l.active=1 AND l.deleted_at IS NULL AND EXISTS(
              SELECT 1 FROM midias_manager.ingest_polls p
              JOIN midias_manager.ingest_jobs j ON j.poll_id=p.id
              WHERE p.link_id=l.id AND j.stage='discovery' AND j.state='failed')))
          ORDER BY l.id
        """).fetchall()
        models = db.execute("SELECT * FROM midias_manager.mechanisms").fetchall()
        existing = {row["url"]: row for row in rows}
        # Rotas comprovadas no piloto também participam, mesmo sem falha persistida na ingestão.
        pilot = json.loads(
            (ROOT / "var/gerenciador/piloto-http-20-20261026/alvos.json").read_text()
        )
        for item in pilot["targets"]:
            if item["kind"] != "xml":
                continue
            if item["url"] in existing:
                continue
            row = db.execute(
                "SELECT l.id,l.url,l.kind,l.model,l.version,l.source_id,l.active,l.deleted_at,"
                "l.last_test->>'outcome' previous_outcome,s.name source_name,s.url source_url,"
                "s.version source_version,s.active source_active FROM midias_manager.links l "
                "JOIN midias_manager.sites s ON s.id=l.source_id WHERE l.url=%s AND "
                "s.deleted_at IS NULL ORDER BY (l.deleted_at IS NULL) DESC LIMIT 1",
                (item["url"],),
            ).fetchone()
            if row:
                # Excluídos fora da seleção automática não podem ser restaurados por esta via.
                if row["deleted_at"]:
                    continue
            else:
                source = db.execute(
                    "SELECT * FROM midias_manager.sites WHERE id=%s AND deleted_at IS NULL",
                    (item["source_id"],),
                ).fetchone()
                if not source:
                    continue
                row = dict(
                    id=-item["id"],
                    url=item["url"],
                    kind="sitemap",
                    model="auto",
                    version=0,
                    source_id=source["id"],
                    active=0,
                    deleted_at=None,
                    source_name=source["name"],
                    source_url=source["url"],
                    source_version=source["version"],
                    source_active=source["active"],
                    previous_outcome="pilot_candidate",
                )
            rows.append(row)
            existing[row["url"]] = row
    result = dict(
        id=uuid.uuid4().hex,
        created_at=stamp(),
        links=rows,
        models=models,
        profiles=PROFILES,
        workers=32,
        seconds_per_link_profile=25,
        xml_documents=4,
        article_samples=3,
        attempts_per_profile=1,
        confirmation="fresh_article_same_profile",
        bright_data=False,
        openrouter=False,
        proof_body_budget=MAX_PROOF_BYTES,
    )
    write_gz(path, result)
    return read_gz(path)


class CachedCurl(CurlReader):
    """Cache da rodada isolado por fonte/perfil e observações sem conteúdo de sessão."""

    def __init__(self, item, profile, seconds=25):
        """Define escopo e orçamento antes da rede."""
        super().__init__(
            [item["source_url"], item["url"]],
            lambda _: None,
            transport=profile,
            seconds=seconds,
            requests=16,
            max_bytes=8 * 1024 * 1024,
        )
        self.item = item
        self.observations = []
        self.bodies = {}
        self.cache_hits = 0

    def get(self, url):
        """Registra prova de cada etapa e reaproveita apenas respostas desta rodada."""
        url = self._scope(url)
        self._check()
        key = f"{self.item['source_id']}|{self.transport}|{url}"
        cached = CACHE.get(key)
        if cached:
            self.cache_hits += 1
            response = cached
        else:
            response = super().get(url)
            CACHE.put(key, response)
        self.observations.append(
            dict(
                url=url,
                status=response.status,
                error=response.error,
                final_url=response.final_url,
                content_type=response.content_type,
                cache_reused=bool(cached),
                sha256=hashlib.sha256(response.content).hexdigest(),
                bytes=len(response.content),
                retry_after_seconds=response.retry_after_seconds,
            )
        )
        if response.ok:
            self.bodies[url] = response.content
        return response


def preserve(reader, ids):
    """Preserva corpos positivos até o orçamento de disco, mantendo hashes em todos os casos."""
    global PROOF_BYTES
    for url in ids:
        body = reader.bodies.get(url)
        if not body:
            continue
        digest = hashlib.sha256(body).hexdigest()
        path = OUT / "bodies" / (digest + ".gz")
        if path.exists():
            continue
        data = gzip.compress(body, compresslevel=4)
        with DISK_LOCK:
            if PROOF_BYTES + len(data) > MAX_PROOF_BYTES:
                continue
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(data)
            PROOF_BYTES += len(data)


def inspect_link(item, models, preferred):
    """Uma passagem por link; muda perfil somente quando pode melhorar o resultado."""
    path = OUT / "results" / f"{item['id']}.json.gz"
    if path.exists():
        return read_gz(path)
    row = dict(link=item, started_at=stamp(), attempts=[], confirmed=False)
    profiles = list(PROFILES)
    if preferred in profiles:
        profiles.remove(preferred)
        profiles.insert(0, preferred)
    for profile in profiles:
        reader = CachedCurl(item, profile)
        try:
            root = reader.get(item["url"])
            attempt = dict(profile=profile, status=root.status, error=root.error)
            row["attempts"].append(attempt)
            if not root.ok:
                attempt["observations"] = reader.observations
                if (
                    root.status in (404, 410)
                    or root.retry_after_seconds
                    or reader.cooldown_deferred
                ):
                    break
                continue
            try:
                doc = parse_document(root.content, root.final_url or item["url"], 8 * 1024 * 1024)
            except ValueError as error:
                attempt["format_error"] = str(error)[:200]
                attempt["observations"] = reader.observations
                continue
            model = (
                "rss"
                if doc["kind"] == "feed"
                else "sitemap_tree"
                if doc["index"]
                else "news_sitemap"
                if doc["news"]
                else "sitemap"
            )
            if item["url"] == "https://g1.globo.com/sitemap/g1/sitemap.xml":
                model = "g1_daily"
            settings = models[model]
            parameters = dict(
                settings["parameters"],
                lookback_days=0,
                timeout_seconds=25,
                max_requests=16,
                article_samples=3,
                sample_order="distributed",
            )
            if doc["index"]:
                parameters.update(max_depth=10, max_documents=4)
            config = dict(
                test_id=uuid.uuid4().hex,
                url=item["url"],
                kind=doc["kind"],
                model=model,
                model_version=settings["version"],
                parameters=parameters,
                bright_data=False,
            )
            source = dict(id=item["source_id"], name=item["source_name"], url=item["source_url"])
            result = run_test(
                config, source, lambda _: None, factory=lambda *_, active=reader: active
            )
            result.update(
                access_transport=profile,
                transport_events=reader.transport_events,
                cache_hits=reader.cache_hits,
            )
            attempt.update(
                outcome=result["outcome"],
                text_samples=result["text_samples"],
                observations=reader.observations,
            )
            row["result"] = result
            if result.get("usable_for_news") and result["text_samples"]:
                sample = next(
                    s
                    for s in result["samples"]
                    if s.get("text") and s.get("article") and not s.get("audio")
                )
                # Uma prova independente recente do mesmo artigo pode servir a vários
                # feeds da mesma fonte. O horário original permanece auditável.
                key = (item["source_id"], profile, sample["url"])
                with LOCK:
                    cached = CONFIRMATIONS.get(key)
                if (
                    cached
                    and (
                        datetime.now(UTC) - datetime.fromisoformat(cached["observed_at"])
                    ).total_seconds()
                    < 300
                ):
                    row["confirmation"] = dict(cached, reused_at=stamp())
                    row["confirmed"] = True
                else:
                    # A confirmação não usa o cache da rodada nem os cookies da primeira sessão.
                    confirm = CurlReader(
                        [item["source_url"], item["url"]],
                        lambda _: None,
                        transport=profile,
                        seconds=12,
                        requests=6,
                        max_bytes=8 * 1024 * 1024,
                    )
                    try:
                        response = confirm.get(sample["url"])
                        now = datetime.now(UTC)
                        audit = (
                            audit_page(
                                PageSnapshot(
                                    url=sample["url"],
                                    status=response.status,
                                    content_type=response.content_type,
                                    final_url=response.final_url,
                                    html=response.content,
                                ),
                                since=datetime.min.replace(tzinfo=UTC),
                                until=now,
                                now=now,
                            )
                            if response.ok
                            else {}
                        )
                        row["confirmation"] = dict(
                            url=sample["url"],
                            status=response.status,
                            error=response.error,
                            observed_at=stamp(),
                            audit=audit,
                            events=confirm.transport_events,
                            sha256=hashlib.sha256(response.content).hexdigest(),
                        )
                        row["confirmed"] = bool(
                            audit.get("texto_util_confirmado")
                            and audit.get("artigo_acessivel")
                            and not audit.get("audio_dedicado")
                        )
                    finally:
                        confirm.close()
                    if row["confirmed"]:
                        with LOCK:
                            if len(CONFIRMATIONS) >= 512:
                                CONFIRMATIONS.pop(next(iter(CONFIRMATIONS)))
                            CONFIRMATIONS[key] = row["confirmation"]
                if row["confirmed"]:
                    preserve(reader, [item["url"], sample["url"]])
                    row["config"] = config
                    row["profile"] = profile
                    break
            if doc["kind"] == "feed" and not doc["entries"]:
                break
            if result["samples"] and all(
                s.get("accessed")
                and not s.get("text")
                and "challenge" not in str(s.get("reason", "")).lower()
                for s in result["samples"]
            ):
                row["content_failure"] = True
                break
            if result["xml_valid"] and result["news_urls"] == 0 and result["complete"]:
                break
        except Exception as error:
            if not row["attempts"] or row["attempts"][-1].get("profile") != profile:
                row["attempts"].append(dict(profile=profile))
            row["attempts"][-1]["exception"] = f"{type(error).__name__}: {error}"[:250]
            if isinstance(error, TestLimitError):
                continue
        finally:
            reader.close()
    row["finished_at"] = stamp()
    write_gz(path, row)
    return row


def group(items, models):
    """Concorrência limitada nos grandes grupos, com intervalo global e pausas persistidas."""
    preferred = None
    pause_lock = threading.Lock()
    first_host = urlsplit(items[0]["url"]).hostname or ""
    pause_path = OUT / "pauses" / (hashlib.sha256(first_host.encode()).hexdigest() + ".json.gz")
    pause_state = read_gz(pause_path) if pause_path.exists() else {}
    pause_used = pause_state.get("seconds", 0)
    if pause_state.get("until"):
        remaining_pause = (
            int((datetime.fromisoformat(pause_state["until"]) - datetime.now(UTC)).total_seconds())
            + 1
        )
        if remaining_pause > 0:
            SESSIONS.defer(pause_state.get("host", first_host), remaining_pause)

    def process(item):
        nonlocal preferred, pause_used
        host = urlsplit(item["url"]).hostname or ""
        if not (OUT / "results" / f"{item['id']}.json.gz").exists():
            # Aguarda pausas curtas antes de iniciar o orçamento do próximo link.
            # Três minutos de espera por grupo encerram a recuperação de pausas.
            with pause_lock:
                delay = SESSIONS.remaining(host)
                if delay and delay <= 90 and pause_used + delay <= 180:
                    print(stamp(), "PAUSA", host, delay, flush=True)
                    pause_used += delay
                    write_gz(
                        pause_path,
                        dict(
                            host=host,
                            seconds=pause_used,
                            until=(datetime.now(UTC) + timedelta(seconds=delay)).isoformat(),
                        ),
                    )
                    time.sleep(delay)
        row = inspect_link(item, models, preferred)
        if row["confirmed"]:
            preferred = row["profile"]
        with LOCK:
            COUNTS["done"] += 1
            COUNTS["confirmed"] += bool(row["confirmed"])
            COUNTS["profiles"] += len(row["attempts"])
            if COUNTS["done"] % 25 == 0 or row["confirmed"]:
                (OUT / "progress.json").write_text(
                    json.dumps(dict(COUNTS, at=stamp(), total=TOTAL))
                )
            if COUNTS["done"] % 100 == 0:
                print(stamp(), dict(COUNTS), "/", TOTAL, flush=True)

    remaining = sum(not (OUT / "results" / f"{i['id']}.json.gz").exists() for i in items)
    if remaining >= 100:
        host = urlsplit(items[0]["url"]).hostname or ""
        # Os dois hosts já demonstraram >90% de aprovação em centenas de testes,
        # sem Retry-After. O limite global de início por host permanece 300 ms.
        workers = (
            4
            if host in {"www.prensalibre.com", "www.information.dk"}
            else 3
            if host == "www.occrp.org"
            else 2
        )
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for future in as_completed([pool.submit(process, item) for item in items]):
                future.result()
    else:
        for item in items:
            process(item)


COUNTS = Counter()
TOTAL = 0


def main():
    """Trabalha na lista congelada; retomadas usam checkpoints sem repetir links concluídos."""
    global TOTAL, PROOF_BYTES
    PROOF_BYTES = sum(p.stat().st_size for p in (OUT / "bodies").glob("*.gz"))
    manifest = freeze()
    TOTAL = len(manifest["links"])
    models = {m["model"]: m for m in manifest["models"]}
    groups = defaultdict(list)
    priority = {
        1,
        2,
        3,
        4,
        15,
        20,
        22,
        24,
        27,
        30,
        93,
        373,
        374,
        375,
        376,
        377,
        378,
        429,
        430,
        442,
        499,
    }
    for item in sorted(
        manifest["links"],
        key=lambda i: (
            i["source_id"] not in priority,
            not i["active"],
            i["previous_outcome"] not in ("blocked", "inconclusive"),
            i["id"],
        ),
    ):
        host = (urlsplit(item["url"]).hostname or "").removeprefix("www.")
        groups[host].append(item)
    print(stamp(), "INÍCIO", TOTAL, "links", len(groups), "hosts", flush=True)
    with ThreadPoolExecutor(max_workers=32) as pool:
        for future in as_completed(
            [pool.submit(group, items, models) for items in groups.values()]
        ):
            future.result()
    (OUT / "progress.json").write_text(
        json.dumps(dict(COUNTS, at=stamp(), total=TOTAL, completed=True))
    )
    print(stamp(), "CONCLUÍDO", dict(COUNTS), flush=True)


if __name__ == "__main__":
    main()
