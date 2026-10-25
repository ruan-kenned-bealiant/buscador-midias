#!/usr/bin/env python3
"""Validação textual finita e restauração auditada dos XMLs recuperados via HTTP."""

from __future__ import annotations

import csv
import fcntl
import gzip
import hashlib
import json
import os
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb
from scripts.retest_excluded_direct import MAX_BYTES, pace_host

from buscador_midias.catalog.health_store import HealthStore
from buscador_midias.catalog.mechanisms import get_mechanism
from buscador_midias.catalog.mechanisms.parsing import parse_document
from buscador_midias.catalog.postgres import WRITE_LOCK, PostgresCatalogStore
from buscador_midias.catalog.reader import TestReader
from buscador_midias.catalog.store import CatalogError, CatalogStore
from buscador_midias.catalog.testing import run_test
from buscador_midias.ingestion.store import IngestStore
from buscador_midias.net.fetcher import FetchResult

ROOT = Path("var/gerenciador/validacao-textual-recuperados")
INPUT = Path("var/gerenciador/reteste-2921-sem-robots/resultados.jsonl")


def atomic(path: Path, value: Any) -> None:
    """Grava o relatório por substituição atômica."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    temporary.replace(path)


class ValidationReader(TestReader):
    """Reutiliza XML preservado; notícias/filhos por HTTP, sem robots/proxy pago."""

    def __init__(
        self, roots: list[str], progress: Any, cached: dict[str, Any], body: bytes, directory: Path
    ) -> None:
        """Impõe orçamento total de 40 segundos e 24 requisições."""
        super().__init__(
            roots, progress, seconds=40, requests=24, max_bytes=MAX_BYTES, before_request=pace_host
        )
        self.cached, self.body, self.directory = cached, body, directory
        self.counts: dict[str, int] = {}
        self.visited: set[str] = set()
        self.observations: list[dict[str, Any]] = []

    def _allowed(self, url: str) -> tuple[bool, str]:
        return True, ""

    def _scope(self, url: str) -> str:
        clean = super()._scope(url)
        if clean in self.visited:
            raise CatalogError("redirect_cycle: ciclo de redirecionamento")
        if self.counts.get(clean, 0) >= 3:
            raise CatalogError("request_budget: máximo de três tentativas por URL")
        self.visited.add(clean)
        self.counts[clean] = self.counts.get(clean, 0) + 1
        return clean

    def get(self, url: str) -> FetchResult:
        """Lê XML preservado ou tenta HTTP até três vezes dentro do orçamento."""
        if url == self.cached["url"]:
            return FetchResult(
                url,
                self.cached["status"],
                self.body,
                None,
                self.cached["final_url"],
                self.cached.get("content_type"),
            )
        response = FetchResult(url, None, b"", "não tentado")
        for _ in range(3):
            self._check()
            self.visited.clear()
            response = super().get(url)
            observation = dict(
                url=url, status=response.status, error=response.error, final_url=response.final_url
            )
            if response.ok:
                digest = hashlib.sha256(response.content).hexdigest()
                target = self.directory / "bodies" / (digest + ".gz")
                temporary = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
                temporary.write_bytes(gzip.compress(response.content, compresslevel=4))
                temporary.replace(target)
                observation.update(body_sha256=digest, body_file=str(target))
            self.observations.append(observation)
            if response.ok or (response.status is not None and response.status < 500):
                break
            if any(
                word in (response.error or "")
                for word in ("redirect_cycle:", "request_budget:", "domínios", "não pública")
            ):
                break
        return response


def prepare(repository: IngestStore) -> dict[str, Any]:
    """Congela vínculos, revisões e mecanismos antes das chamadas de rede."""
    rows = [json.loads(line) for line in INPUT.read_text().splitlines()]
    rows = [row for row in rows if row["category"] == "xml_with_urls"]
    manifest: dict[str, Any] = dict(
        id=uuid.uuid4().hex,
        created_at=datetime.now(UTC).isoformat(),
        robots=False,
        bright_data=False,
        workers=12,
        links=[],
    )
    with repository.connection() as db:
        models = {row["model"]: row for row in db.execute("SELECT * FROM mechanisms")}
        for row in rows:
            current = db.execute(
                "SELECT l.*,s.name source_name,s.url source_url,s.version source_version,"
                "s.active source_active,s.deleted_at source_deleted FROM links l "
                "JOIN sites s ON s.id=l.source_id WHERE l.id=%s",
                (row["link_id"],),
            ).fetchone()
            if not current or current["url"] != row["url"]:
                raise ValueError("Link de entrada ausente ou URL alterada")
            body = gzip.decompress(Path(row["body_file"]).read_bytes())
            if hashlib.sha256(body).hexdigest() != row["body_sha256"]:
                raise ValueError("Hash do XML preservado não corresponde")
            doc = parse_document(body, row["final_url"] or row["url"], MAX_BYTES)
            model = (
                "rss"
                if doc["kind"] == "feed"
                else "sitemap_tree"
                if doc["index"]
                else "news_sitemap"
                if doc.get("news")
                else "sitemap"
            )
            mechanism = get_mechanism(model)
            parameters = mechanism.defaults()
            parameters.update(
                lookback_days=0,
                timeout_seconds=40,
                max_requests=24,
                article_samples=3,
                sample_order="distributed",
            )
            event = db.execute(
                "SELECT action FROM events WHERE entity='endpoint' "
                "AND entity_id=%s ORDER BY id DESC LIMIT 1",
                (row["link_id"],),
            ).fetchone()
            manifest["links"].append(
                dict(
                    cached=row,
                    original=HealthStore.jsonable(current),
                    automatic_exclusion=bool(event and event["action"] == "validation_exclude"),
                    source=dict(
                        id=current["source_id"],
                        name=current["source_name"],
                        url=current["source_url"],
                    ),
                    config=dict(
                        url=row["url"],
                        kind=doc["kind"],
                        model=model,
                        model_version=models[model]["version"],
                        parameters=parameters,
                        bright_data=False,
                        test_id=uuid.uuid4().hex,
                    ),
                )
            )
    return manifest


def validate(item: dict[str, Any], directory: Path) -> dict[str, Any]:
    """Exige notícia textual, preservando respostas e ignorando recência."""
    cached = item["cached"]
    reader = None
    try:
        body = gzip.decompress(Path(cached["body_file"]).read_bytes())
        if hashlib.sha256(body).hexdigest() != cached["body_sha256"]:
            raise ValueError("XML preservado adulterado")

        def factory(roots: list[str], progress: Any) -> ValidationReader:
            nonlocal reader
            reader = ValidationReader(roots, progress, cached, body, directory)
            return reader

        result = run_test(item["config"], item["source"], lambda _: None, factory)
    except Exception as error:
        result = dict(
            item["config"],
            usable_for_news=False,
            text_samples=0,
            outcome="inconclusive",
            error=type(error).__name__ + ": " + str(error),
            observed_at=datetime.now(UTC).isoformat(),
        )
    result.update(
        direct_without_robots=True,
        robots_requests=0,
        bright_data_requests=0,
        root_cached=True,
        root_observed_at=cached["checked_at"],
        root_body_sha256=cached["body_sha256"],
        observations=reader.observations if reader else [],
    )
    return dict(link_id=cached["link_id"], source_id=cached["source_id"], result=result)


def apply(
    repository: IngestStore, manifest: dict[str, Any], item: dict[str, Any], result: dict[str, Any]
) -> str:
    """Arquiva todas as provas; só restaura aprovados sem sobrescrever edições concorrentes."""
    original, config = item["original"], item["config"]
    ident = original["id"]
    with repository.connection() as db:
        db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
        prior = db.execute(
            "SELECT 1 FROM link_test_results WHERE link_id=%s AND test_id=%s",
            (ident, config["test_id"]),
        ).fetchone()
        if prior:
            return "already_recorded"
        db.execute(
            "INSERT INTO link_test_results(link_id,model,test_id,model_version,"
            "observed_at,parameters,result) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                ident,
                config["model"],
                config["test_id"],
                config["model_version"],
                result["observed_at"],
                Jsonb(config["parameters"]),
                Jsonb(result),
            ),
        )
        if not result.get("usable_for_news") or not result.get("text_samples"):
            return "not_confirmed"
        before = db.execute("SELECT * FROM links WHERE id=%s FOR UPDATE", (ident,)).fetchone()
        source = db.execute(
            "SELECT * FROM sites WHERE id=%s FOR UPDATE", (original["source_id"],)
        ).fetchone()
        model = db.execute(
            "SELECT version FROM mechanisms WHERE model=%s", (config["model"],)
        ).fetchone()
        if (
            before["version"] != original["version"]
            or before["url"] != original["url"]
            or model["version"] != config["model_version"]
            or source["deleted_at"]
        ):
            return "concurrent_edit"
        if before["deleted_at"] and not item["automatic_exclusion"]:
            return "manual_deletion_preserved"
        event = db.execute(
            "SELECT * FROM events WHERE entity='source' AND entity_id=%s ORDER BY id DESC LIMIT 1",
            (source["id"],),
        ).fetchone()
        own_restore = bool(
            event
            and event["action"] == "direct_text_restore_site"
            and event["after_json"].get("validation_run_id") == manifest["id"]
            and event["after_json"].get("version") == source["version"]
        )
        if source["version"] != original["source_version"] and not own_restore:
            return "source_concurrent_edit"
        if not source["active"]:
            if not event or event["action"] != "validation_inactivate":
                return "manual_inactivation_preserved"
            updated_source = db.execute(
                "UPDATE sites SET active=1,version=version+1,"
                "updated_at=now() WHERE id=%s RETURNING *",
                (source["id"],),
            ).fetchone()
            audited = dict(HealthStore.jsonable(updated_source), validation_run_id=manifest["id"])
            db.execute(
                "INSERT INTO events(entity,entity_id,action,happened_at,before_json,"
                "after_json) VALUES('source',%s,'direct_text_restore_site',now(),%s,%s)",
                (source["id"], Jsonb(HealthStore.jsonable(source)), Jsonb(audited)),
            )
        after = db.execute(
            "UPDATE links SET active=1,deleted_at=NULL,model=%s,kind=%s,"
            "version=version+1,updated_at=now(),last_test=%s,evidence=%s "
            "WHERE id=%s RETURNING *",
            (
                config["model"],
                config["kind"],
                Jsonb(result),
                Jsonb(CatalogStore._test_evidence(result)),
                ident,
            ),
        ).fetchone()
        audited = dict(HealthStore.jsonable(after), validation_run_id=manifest["id"])
        db.execute(
            "INSERT INTO events(entity,entity_id,action,happened_at,before_json,"
            "after_json) VALUES('endpoint',%s,'direct_text_restore',now(),%s,%s)",
            (ident, Jsonb(HealthStore.jsonable(before)), Jsonb(audited)),
        )
        return "restored" if before["deleted_at"] else "validated_active"


def export_report(manifest: dict[str, Any], done: dict[int, Any]) -> None:
    """Exporta por link a decisão e uma notícia textual de exemplo."""
    with (ROOT / "links.csv").open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.writer(output)
        writer.writerow(
            [
                "site_id",
                "site",
                "link_id",
                "url",
                "mecanismo",
                "resultado",
                "acao",
                "amostras_textuais",
                "noticia_exemplo",
                "teste_id",
            ]
        )
        for item in manifest["links"]:
            row = done[item["original"]["id"]]
            result = row["result"]
            example = next(
                (
                    sample["url"]
                    for sample in result.get("samples", [])
                    if sample.get("text") and sample.get("eligible", True)
                ),
                "",
            )
            writer.writerow(
                [
                    item["source"]["id"],
                    item["source"]["name"],
                    row["link_id"],
                    item["config"]["url"],
                    item["config"]["model"],
                    result["outcome"],
                    row["action"],
                    result.get("text_samples", 0),
                    example,
                    item["config"]["test_id"],
                ]
            )


def main() -> None:
    """Executa uma rodada retomável sem iniciar ingestão de notícias."""
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "bodies").mkdir(exist_ok=True)
    with (ROOT / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        repository = IngestStore(PostgresCatalogStore())
        repository.ready = True
        path = ROOT / "manifesto.json"
        if not path.exists():
            atomic(path, prepare(repository))
        manifest = json.loads(path.read_text())
        path = ROOT / "resultados.jsonl"
        done: dict[int, Any] = {}
        if path.exists():
            raw = path.read_bytes()
            if raw and not raw.endswith(b"\n"):
                raw = raw[: raw.rfind(b"\n") + 1]
                path.write_bytes(raw)
            done = {row["link_id"]: row for row in map(json.loads, raw.splitlines())}

        def summary() -> None:
            atomic(
                ROOT / "resumo.json",
                dict(
                    id=manifest["id"],
                    total=len(manifest["links"]),
                    completed=len(done),
                    finished=len(done) == len(manifest["links"]),
                    updated_at=datetime.now(UTC).isoformat(),
                    outcomes=Counter(row["result"]["outcome"] for row in done.values()),
                    actions=Counter(row["action"] for row in done.values()),
                    robots_requests=0,
                    bright_data_requests=0,
                ),
            )

        summary()
        # Ordenação alternada entre fontes evita deixar todas as fontes pequenas atrás de RTV.
        pending = [item for item in manifest["links"] if item["original"]["id"] not in done]
        groups: dict[int, list[Any]] = {}
        for item in pending:
            groups.setdefault(item["source"]["id"], []).append(item)
        pending = []
        while any(groups.values()):
            for group in groups.values():
                if group:
                    pending.append(group.pop(0))
        with path.open("a") as output, ThreadPoolExecutor(max_workers=12) as pool:
            futures = {pool.submit(validate, item, ROOT): item for item in pending}
            for future in as_completed(futures):
                item = futures[future]
                row = future.result()
                row["action"] = apply(repository, manifest, item, row["result"])
                done[row["link_id"]] = row
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
                output.flush()
                os.fsync(output.fileno())
                summary()
                print(
                    json.dumps(
                        dict(
                            completed=len(done),
                            total=len(manifest["links"]),
                            link_id=row["link_id"],
                            action=row["action"],
                            outcome=row["result"]["outcome"],
                        )
                    ),
                    flush=True,
                )
        export_report(manifest, done)


if __name__ == "__main__":
    main()
