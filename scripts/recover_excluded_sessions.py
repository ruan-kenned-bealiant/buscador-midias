#!/usr/bin/env python3
"""Rodada finita dos excluídos originais com sessão HTTP e restauração apenas textual."""

from __future__ import annotations

import argparse
import csv
import fcntl
import gzip
import hashlib
import json
import os
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.validate_recovered_direct import apply, atomic

from buscador_midias.catalog.batch_reader import BatchCache
from buscador_midias.catalog.health_store import HealthStore
from buscador_midias.catalog.http_policy import retryable
from buscador_midias.catalog.mechanisms import get_mechanism
from buscador_midias.catalog.mechanisms.parsing import parse_document
from buscador_midias.catalog.postgres import PostgresCatalogStore
from buscador_midias.catalog.reader import TestReader
from buscador_midias.catalog.store import CatalogError
from buscador_midias.catalog.testing import run_test
from buscador_midias.ingestion.store import IngestStore
from buscador_midias.net.fetcher import FetchResult

MAX_BYTES = 8 * 1024 * 1024


def append(path: Path, row: dict[str, Any]) -> None:
    """Confirma cada checkpoint antes de continuar; somente o coordenador escreve."""
    with path.open("a") as file:
        file.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        file.flush()
        os.fsync(file.fileno())


def load(path: Path) -> list[dict[str, Any]]:
    """Recupera checkpoints completos e descarta somente uma última linha interrompida."""
    if not path.exists():
        return []
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raw = raw[: raw.rfind(b"\n") + 1]
        path.write_bytes(raw)
    return [json.loads(line) for line in raw.splitlines()]


def freeze(repository: IngestStore) -> dict[str, Any]:
    """Seleciona apenas exclusões automáticas remanescentes da lista original autorizada."""
    audit = json.loads(Path("var/gerenciador/auditoria-exclusoes-robots.json").read_text())
    ids = [row["link_id"] for row in audit["links"] if row["unavailable"]]
    if len(ids) != 2921 or len(set(ids)) != 2921:
        raise ValueError("Lista histórica inesperada")
    with repository.connection() as db:
        models = {
            row["model"]: row["version"]
            for row in db.execute("SELECT model,version FROM mechanisms")
        }
        rows = db.execute(
            "SELECT l.*,s.name source_name,s.url source_url,s.version source_version,"
            "s.active source_active,s.deleted_at source_deleted FROM links l JOIN sites s "
            "ON s.id=l.source_id JOIN LATERAL (SELECT action FROM events WHERE entity='endpoint' "
            "AND entity_id=l.id ORDER BY id DESC LIMIT 1) e ON true WHERE l.id=ANY(%s) "
            "AND l.deleted_at IS NOT NULL AND l.active=0 AND s.deleted_at IS NULL "
            "AND e.action='validation_exclude' ORDER BY l.id",
            (ids,),
        ).fetchall()
    items = []
    for row in rows:
        model = row["model"]
        params = get_mechanism(model).defaults()
        params.update(lookback_days=0, timeout_seconds=40, article_samples=3)
        items.append(
            dict(
                original=HealthStore.jsonable(row),
                automatic_exclusion=True,
                source=dict(id=row["source_id"], name=row["source_name"], url=row["source_url"]),
                config=dict(
                    url=row["url"],
                    kind=row["kind"],
                    model=model,
                    model_version=models[model],
                    parameters=params,
                    bright_data=False,
                    test_id=uuid.uuid4().hex,
                ),
            )
        )
    return dict(
        id=uuid.uuid4().hex,
        created_at=datetime.now(UTC).isoformat(),
        links=items,
        model_versions=models,
        robots=False,
        bright_data=False,
        root_deadline_seconds=1500,
        total_deadline_seconds=3000,
        max_attempts=3,
        workers=12,
    )


class RecoveryReader(TestReader):
    """Rede atual, até três acessos por endereço; preserva os corpos usados como prova."""

    def __init__(
        self,
        roots: list[str],
        root: Path,
        *,
        seconds: float = 40,
        counts: dict[str, int] | None = None,
        attempts: int = 3,
        cached: dict[str, Any] | None = None,
        cache: BatchCache | None = None,
    ) -> None:
        """O XML raiz recebido nesta rodada pode ser reutilizado durante a prova textual."""
        super().__init__(roots, lambda _: None, seconds=seconds, requests=24, max_bytes=MAX_BYTES)
        self.root, self.counts, self.attempts = root, counts if counts is not None else {}, attempts
        self.cached, self.cache = cached, cache
        self.observations: list[dict[str, Any]] = []

    def _scope(self, url: str) -> str:
        clean = super()._scope(url)
        if self.counts.get(clean, 0) >= 3:
            raise CatalogError("request_budget: três acessos para o mesmo endereço")
        self.counts[clean] = self.counts.get(clean, 0) + 1
        return clean

    def get(self, url: str) -> FetchResult:
        """Não dorme durante Retry-After: a raiz volta ao coordenador ou a amostra é adiada."""
        if self.cached and url == self.cached["url"]:
            body = gzip.decompress(Path(self.cached["body_file"]).read_bytes())
            if hashlib.sha256(body).hexdigest() != self.cached["body_sha256"]:
                raise ValueError("Hash do XML divergente")
            return FetchResult(
                url,
                self.cached["status"],
                body,
                None,
                self.cached["final_url"],
                self.cached.get("content_type"),
            )
        # O cache de corpos permanece isolado pela fonte, assim como cookies e conexões.
        key = self.namespace + "\n" + url
        previous = self.cache.get(key) if self.cache else None
        if previous:
            TestReader._scope(self, url)
            digest = hashlib.sha256(previous.content).hexdigest()
            self.observations.append(
                dict(
                    url=url,
                    status=previous.status,
                    cache_reused=True,
                    body_sha256=digest,
                    body_file=str(self.root / "bodies" / (digest + ".gz")),
                )
            )
            return previous
        response = FetchResult(url, None, b"", "não tentado")
        for number in range(self.attempts):
            self._check()
            response = super().get(url)
            observation: dict[str, Any] = dict(
                url=url,
                status=response.status,
                error=response.error,
                final_url=response.final_url,
                retry_after_seconds=response.retry_after_seconds,
            )
            if response.ok:
                digest = hashlib.sha256(response.content).hexdigest()
                target = self.root / "bodies" / (digest + ".gz")
                tmp = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
                tmp.write_bytes(gzip.compress(response.content, compresslevel=4))
                tmp.replace(target)
                observation.update(body_file=str(target), body_sha256=digest)
                if self.cache:
                    self.cache.put(key, response)
            self.observations.append(observation)
            if (
                response.ok
                or response.retry_after_seconds
                or self.cooldown_deferred
                or (
                    not retryable(response.status, response.error)
                    and not (response.status == 403 and self.session_updated)
                )
                or "request_budget:" in (response.error or "")
            ):
                break
            if number + 1 < self.attempts:
                time.sleep(min(0.5 * (number + 1), max(0, self.deadline - time.monotonic())))
        return response


def root_probe(item: dict[str, Any], root: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Uma tentativa por tarefa; o coordenador controla os próximos prazos por host."""
    reader = RecoveryReader(
        [item["source"]["url"], item["config"]["url"]],
        root,
        seconds=12,
        counts=dict(state.get("counts", {})),
        attempts=1,
    )
    url = item["config"]["url"]
    result: dict[str, Any] = dict(url=url, xml_valid=False, entries=0, children=0, http_ok=False)
    try:
        response = reader.get(url)
        result.update(
            status=response.status,
            error=response.error,
            http_ok=response.ok,
            final_url=response.final_url or url,
            content_type=response.content_type,
        )
        if response.ok:
            result.update(reader.observations[-1])
            try:
                doc = parse_document(response.content, response.final_url or url, MAX_BYTES)
                result.update(
                    xml_valid=True,
                    kind=doc["kind"],
                    index=doc["index"],
                    news=bool(doc.get("news")),
                    entries=len(doc["entries"]),
                    children=len(doc["children"]),
                )
            except (TypeError, ValueError) as error:
                result["parse_error"] = str(error)[:300]
    except Exception as error:
        result.update(error=type(error).__name__ + ": " + str(error)[:300], status=None)
    result.update(
        counts=reader.counts,
        attempt=state.get("attempt", 0) + 1,
        physical_requests=state.get("physical_requests", 0) + reader.requests,
        connections_reused=state.get("connections_reused", 0) + reader.connections_reused,
        cookies_sent=state.get("cookies_sent", 0) + reader.cookies_sent,
        checked_at=datetime.now(UTC).isoformat(),
    )
    status, error_text = result.get("status"), result.get("error")
    terminal = (
        result["http_ok"]
        or result["attempt"] >= 3
        or (not retryable(status, error_text) and not (status == 403 and reader.session_updated))
        or "request_budget:" in (error_text or "")
    )
    after = reader.retry_after_seconds or 0
    result.update(
        final=terminal,
        retry_at=time.time() + max(after, 2 ** result["attempt"]),
        retry_host=reader.retry_after_host,
        server_until=time.time() + after if after else 0,
    )
    return result


def roots_phase(manifest: dict[str, Any], root: Path, report: Any) -> dict[str, Any]:
    """Um trabalho por hostname, pausas do servidor e prazo total congelado de 25 minutos."""
    items = {item["config"]["url"]: item for item in manifest["links"]}
    states = {row["url"]: row for row in load(root / "raizes.jsonl")}
    pending = {url for url in items if not states.get(url, {}).get("final")}
    deadline = (
        datetime.fromisoformat(manifest["created_at"]).timestamp()
        + manifest["root_deadline_seconds"]
    )
    blocks: dict[str, float] = {}
    for row in states.values():
        if row.get("retry_host"):
            host = row["retry_host"]
            blocks[host] = max(blocks.get(host, 0), row.get("server_until", 0))
    active: dict[Any, str] = {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        while pending or active:
            hosts = {urlsplit(url).hostname for url in active.values()}
            now = time.time()
            for url in sorted(pending, key=lambda u: (states.get(u, {}).get("attempt", 0), u)):
                host = urlsplit(url).hostname or ""
                due = max(states.get(url, {}).get("retry_at", 0), blocks.get(host, 0))
                if now >= deadline or due > deadline:
                    row = dict(
                        states.get(url, {}),
                        url=url,
                        final=True,
                        deferred=True,
                        error="server_wait_exceeds_budget" if due > deadline else "round_deadline",
                        checked_at=datetime.now(UTC).isoformat(),
                    )
                    states[url] = row
                    append(root / "raizes.jsonl", row)
                    pending.remove(url)
                    continue
                if len(active) >= 12:
                    break
                if host in hosts or due > now:
                    continue
                active[pool.submit(root_probe, items[url], root, states.get(url, {}))] = url
                pending.remove(url)
                hosts.add(host)
            if active:
                completed, _ = wait(active, timeout=1, return_when=FIRST_COMPLETED)
                for future in completed:
                    url = active.pop(future)
                    row = future.result()
                    states[url] = row
                    append(root / "raizes.jsonl", row)
                    if row.get("retry_host"):
                        host = row["retry_host"]
                        blocks[host] = max(blocks.get(host, 0), row.get("server_until", 0))
                    if not row["final"]:
                        pending.add(url)
            elif pending:
                time.sleep(0.5)
            report("access", states, {})
    return states


def config_for(
    item: dict[str, Any], access: dict[str, Any], versions: dict[str, int]
) -> dict[str, Any]:
    """Atribui um modelo explícito somente a partir do XML realmente recebido."""
    config = dict(item["config"])
    if access.get("xml_valid"):
        model = (
            "rss"
            if access["kind"] == "feed"
            else "sitemap_tree"
            if access["index"]
            else "news_sitemap"
            if access.get("news")
            else "sitemap"
        )
        params = get_mechanism(model).defaults()
        params.update(lookback_days=0, timeout_seconds=40, max_requests=24, article_samples=3)
        config.update(
            model=model, kind=access["kind"], model_version=versions[model], parameters=params
        )
    return config


def validate(
    item: dict[str, Any], access: dict[str, Any], root: Path, deadline: float, cache: BatchCache
) -> dict[str, Any]:
    """Reutiliza a raiz e exige artigo textual; não usa IA nem filtro de data."""
    reader = None
    eligible = access.get("xml_valid") and (access.get("entries") or access.get("children"))
    if eligible and time.time() < deadline:
        try:

            def factory(roots: list[str], progress: Any) -> RecoveryReader:
                nonlocal reader
                reader = RecoveryReader(
                    roots, root, cached=access, cache=cache, seconds=min(40, deadline - time.time())
                )
                return reader

            result = run_test(item["config"], item["source"], lambda _: None, factory)
        except Exception as error:
            result = dict(
                outcome="inconclusive",
                error=type(error).__name__ + ": " + str(error)[:300],
                usable_for_news=False,
                text_samples=0,
            )
    else:
        outcome = (
            "inconclusive"
            if access.get("deferred") or eligible
            else "empty"
            if access.get("xml_valid")
            else "invalid"
            if access.get("http_ok")
            else "blocked"
            if access.get("status") in (401, 403, 429)
            else "inconclusive"
        )
        result = dict(
            outcome=outcome,
            usable_for_news=False,
            text_samples=0,
            error=access.get("error") or access.get("parse_error"),
            endpoint_accessible=access.get("http_ok", False),
            endpoint_status=access.get("status"),
            xml_valid=access.get("xml_valid", False),
            root_entries=access.get("entries", 0),
            root_children=access.get("children", 0),
        )
    result.update(
        test_id=item["config"]["test_id"],
        model=item["config"]["model"],
        model_version=item["config"]["model_version"],
        parameters=item["config"]["parameters"],
        observed_at=datetime.now(UTC).isoformat(),
        url=item["config"]["url"],
        root_access=access,
        observations=reader.observations if reader else [],
        robots_requests=0,
        bright_data_requests=0,
        access_method="direct",
    )
    return result


def main() -> None:
    """Preserva resultados anteriores; retomadas usam manifesto, checkpoints e testes únicos."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("var/gerenciador/reteste-http-melhorado-20261025")
    )
    args = parser.parse_args()
    root = args.output
    root.mkdir(parents=True, exist_ok=True)
    (root / "bodies").mkdir(exist_ok=True)
    with (root / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        repository = IngestStore(PostgresCatalogStore())
        repository.ready = True
        if not (root / "manifesto.json").exists():
            atomic(root / "manifesto.json", freeze(repository))
        manifest = json.loads((root / "manifesto.json").read_text())
        last_print = [0.0]

        def report(phase: str, access: dict[str, Any], done: dict[int, Any]) -> None:
            summary = dict(
                id=manifest["id"],
                phase=phase,
                total=len(manifest["links"]),
                root_urls=len({i["config"]["url"] for i in manifest["links"]}),
                roots_completed=sum(bool(x.get("final")) for x in access.values()),
                roots_xml_with_urls=sum(
                    bool(x.get("xml_valid") and (x.get("entries") or x.get("children")))
                    for x in access.values()
                ),
                roots_deferred=sum(bool(x.get("deferred")) for x in access.values()),
                roots_attempted=sum(bool(x.get("physical_requests")) for x in access.values()),
                root_http_requests=sum(x.get("physical_requests", 0) for x in access.values()),
                completed=len(done),
                actions=Counter(x["action"] for x in done.values()),
                text_confirmed=sum(bool(x["result"].get("usable_for_news")) for x in done.values()),
                updated_at=datetime.now(UTC).isoformat(),
                robots_requests=0,
                bright_data_requests=0,
            )
            atomic(root / "resumo.json", summary)
            if time.monotonic() - last_print[0] >= 20 or phase == "completed":
                print(json.dumps(summary, ensure_ascii=False), flush=True)
                last_print[0] = time.monotonic()

        access = roots_phase(manifest, root, report)
        done = {r["link_id"]: r for r in load(root / "resultados.jsonl")}
        items = []
        for original in manifest["links"]:
            item = dict(
                original,
                config=config_for(
                    original, access[original["config"]["url"]], manifest["model_versions"]
                ),
            )
            if item["original"]["id"] not in done:
                items.append(item)
        # Alterna fontes para não deixar fontes pequenas atrás de centenas de feeds de uma só.
        groups: dict[int, list[Any]] = defaultdict(list)
        for item in items:
            groups[item["source"]["id"]].append(item)
        ordered = []
        while any(groups.values()):
            for group in groups.values():
                if group:
                    ordered.append(group.pop(0))
        deadline = (
            datetime.fromisoformat(manifest["created_at"]).timestamp()
            + manifest["total_deadline_seconds"]
        )
        cache = BatchCache()
        report("text_validation", access, done)
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = {
                pool.submit(validate, i, access[i["config"]["url"]], root, deadline, cache): i
                for i in ordered
            }
            for future in as_completed(futures):
                item = futures[future]
                result = future.result()
                action = apply(repository, manifest, item, result)
                row = dict(
                    link_id=item["original"]["id"],
                    source_id=item["source"]["id"],
                    source=item["source"]["name"],
                    url=item["config"]["url"],
                    action=action,
                    result=result,
                )
                append(root / "resultados.jsonl", row)
                done[row["link_id"]] = row
                report("text_validation", access, done)
        with (root / "links.csv").open("w", newline="", encoding="utf-8-sig") as output:
            writer = csv.writer(output)
            writer.writerow(
                [
                    "site",
                    "link_id",
                    "url",
                    "http_status",
                    "xml_valido",
                    "adiado_servidor",
                    "mecanismo",
                    "resultado",
                    "acao",
                    "noticia_exemplo",
                ]
            )
            for item in manifest["links"]:
                row = done[item["original"]["id"]]
                result = row["result"]
                observed = access[row["url"]]
                example = next(
                    (
                        s["url"]
                        for s in result.get("samples", [])
                        if s.get("text") and s.get("eligible", True)
                    ),
                    "",
                )
                writer.writerow(
                    [
                        row["source"],
                        row["link_id"],
                        row["url"],
                        observed.get("status"),
                        observed.get("xml_valid", False),
                        observed.get("deferred", False),
                        result["model"],
                        result["outcome"],
                        row["action"],
                        example,
                    ]
                )
        report("completed", access, done)


if __name__ == "__main__":
    main()
