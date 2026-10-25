#!/usr/bin/env python3
"""Reteste finito de exclusões por robots_unavailable, sem alterar o catálogo.

Experimento solicitado: HTTP direto, sem robots e sem provedor pago. Preserva
TLS, DNS público, escopo e limites; não comprova texto de notícia nem reativa links.
Resultados são gravados por link para permitir retomada após interrupção.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import gzip
import hashlib
import json
import os
import threading
import time
from collections import Counter, defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from buscador_midias.catalog.mechanisms.parsing import parse_document
from buscador_midias.catalog.reader import TestReader
from buscador_midias.catalog.store import CatalogError

MAX_BYTES = 8 * 1024 * 1024
_HOST_LOCK = threading.Lock()
_HOST_NEXT: dict[str, float] = {}


def pace_host(host: str) -> None:
    """Espaça acessos ao mesmo host também entre leitores e redirecionamentos."""
    with _HOST_LOCK:
        slot = max(time.monotonic(), _HOST_NEXT.get(host, 0))
        _HOST_NEXT[host] = slot + 0.3
    time.sleep(max(0, slot - time.monotonic()))


class DirectProbeReader(TestReader):
    """Omissão de robots restrita a este experimento, com proteção de redirecionamentos."""

    def __init__(self, url: str, counts: dict[str, int], timeout: float) -> None:
        """Mantém orçamento por endereço entre tentativas e rejeita ciclos de redireção."""
        super().__init__(
            [url],
            lambda _: None,
            seconds=timeout,
            requests=6,
            max_bytes=MAX_BYTES,
            before_request=pace_host,
        )
        self.counts = counts
        self.visited: set[str] = set()

    def _allowed(self, url: str) -> tuple[bool, str]:
        """Neste reteste explícito, não solicita robots.txt em nenhum domínio."""
        return True, ""

    def _scope(self, url: str) -> str:
        """Valida escopo antes de aplicar orçamento; não segue ciclos indefinidamente."""
        clean = super()._scope(url)
        if clean in self.visited:
            raise CatalogError("redirect_cycle: redirecionamento para endereço já visitado")
        if self.counts.get(clean, 0) >= 3:
            raise CatalogError("request_budget: três tentativas para o mesmo endereço")
        self.visited.add(clean)
        self.counts[clean] = self.counts.get(clean, 0) + 1
        return clean


def probe(url: str, root: Path, timeout: float) -> dict[str, Any]:
    """Até três tentativas, sem repetir erros permanentes nem páginas já recebidas."""
    started = time.monotonic()
    counts: dict[str, int] = {}
    attempts: list[dict[str, Any]] = []
    last: dict[str, Any] = {}
    for number in range(1, 4):
        reader = DirectProbeReader(url, counts, timeout)
        last = dict(
            attempt=number,
            status=None,
            http_ok=False,
            xml_valid=False,
            entries=0,
            children=0,
            kind=None,
            error=None,
        )
        try:
            response = reader.get(url)
            last.update(
                status=response.status,
                http_ok=response.ok,
                error=response.error,
                bytes=len(response.content),
                final_url=response.final_url,
                content_type=response.content_type,
            )
            if response.ok:
                digest = hashlib.sha256(response.content).hexdigest()
                target = root / "bodies" / (digest + ".gz")
                # Vários links podem devolver os mesmos bytes. Escrita atômica por tarefa.
                temporary = target.with_suffix(
                    ".tmp-" + str(os.getpid()) + "-" + hashlib.sha256(url.encode()).hexdigest()[:12]
                )
                temporary.write_bytes(gzip.compress(response.content, compresslevel=4))
                temporary.replace(target)
                last.update(body_sha256=digest, body_file=str(target))
                try:
                    document = parse_document(
                        response.content, response.final_url or url, MAX_BYTES
                    )
                    last.update(
                        xml_valid=True,
                        kind=document["kind"],
                        entries=len(document["entries"]),
                        children=len(document["children"]),
                        index=document["index"],
                    )
                except (ValueError, TypeError) as error:
                    last["parse_error"] = str(error)[:300]
        except Exception as error:
            last["error"] = type(error).__name__ + ": " + str(error)[:300]
        last["requests"] = reader.requests
        attempts.append(dict(last))
        status, error_text = last["status"], last["error"] or ""
        # 2xx/4xx encerram a decisão do link; indisponibilidade 5xx/rede pode repetir.
        if (
            last["http_ok"]
            or (status is not None and 400 <= status < 500)
            or any(
                marker in error_text
                for marker in (
                    "redirect_cycle:",
                    "request_budget:",
                    "fora dos domínios",
                    "não pública",
                )
            )
        ):
            break
        if number < 3:
            time.sleep(0.5 * number)
    if last["xml_valid"]:
        category = "xml_with_urls" if last["entries"] or last["children"] else "xml_empty"
    elif last["http_ok"]:
        category = "http_without_valid_xml"
    elif last["status"] is not None:
        category = "http_" + str(last["status"])
    else:
        category = "redirect_or_transport_error"
    return dict(
        url=url,
        category=category,
        **{k: v for k, v in last.items() if k != "attempt"},
        attempt_count=len(attempts),
        http_requests=sum(a["requests"] for a in attempts),
        seconds=round(time.monotonic() - started, 3),
        attempts=attempts,
        robots_requests=0,
        bright_data_requests=0,
    )


def export(
    root: Path, rows: list[dict[str, Any]], total: int, started: float, complete: bool = False
) -> dict[str, Any]:
    """Retrato atômico e CSV; JSONL continua sendo o checkpoint por link."""
    summary = dict(
        state="completed" if complete else "running",
        updated_at=datetime.now(UTC).isoformat(),
        total=total,
        completed=len(rows),
        remaining=total - len(rows),
        http_ok=sum(r["http_ok"] for r in rows),
        xml_valid=sum(r["xml_valid"] for r in rows),
        xml_with_urls=sum(r["category"] == "xml_with_urls" for r in rows),
        categories=dict(Counter(r["category"] for r in rows)),
        elapsed_this_process_seconds=round(time.monotonic() - started, 1),
        robots_requests=0,
        bright_data_requests=0,
        catalog_changed=False,
        news_text_tested=False,
    )
    temporary = root / "resumo.tmp"
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    temporary.replace(root / "resumo.json")
    if complete or len(rows) % 50 == 0:
        keys = [
            "link_id",
            "source_id",
            "source",
            "url",
            "category",
            "status",
            "http_ok",
            "xml_valid",
            "kind",
            "entries",
            "children",
            "error",
            "attempt_count",
            "http_requests",
        ]
        with (root / "links.tmp").open("w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=keys, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        (root / "links.tmp").replace(root / "links.csv")
    return summary


def main() -> int:
    """Fila finita, até doze hostnames simultâneos, um link por hostname por vez."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--audit", type=Path, default=Path("var/gerenciador/auditoria-exclusoes-robots.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("var/gerenciador/reteste-2921-sem-robots")
    )
    parser.add_argument("--workers", type=int, default=12, choices=range(1, 17))
    parser.add_argument("--timeout", type=float, default=12)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 30:
        parser.error("Timeout deve estar entre 1 e 30 segundos.")
    root = args.output
    root.mkdir(parents=True, exist_ok=True)
    (root / "bodies").mkdir(exist_ok=True)
    with (root / "run.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest_path = root / "manifesto.json"
        if manifest_path.exists():
            records = json.loads(manifest_path.read_text())["links"]
        else:
            records = [r for r in json.loads(args.audit.read_text())["links"] if r["unavailable"]]
            if len(records) != 2921 or len({r["link_id"] for r in records}) != 2921:
                raise ValueError("A auditoria deve conter os 2.921 links distintos autorizados.")
            manifest_path.write_text(
                json.dumps(
                    dict(
                        started_at=datetime.now(UTC).isoformat(),
                        input=str(args.audit),
                        links=records,
                        workers=args.workers,
                        timeout=args.timeout,
                        max_attempts=3,
                        robots=False,
                        bright_data=False,
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        checkpoint = root / "resultados.jsonl"
        rows = []
        if checkpoint.exists():
            raw = checkpoint.read_bytes()
            # Uma interrupção pode deixar a última linha incompleta. Só ela é descartada.
            if raw and not raw.endswith(b"\n"):
                raw = raw[: raw.rfind(b"\n") + 1]
                checkpoint.write_bytes(raw)
            rows = [json.loads(line) for line in raw.splitlines()]
        finished = {r["link_id"] for r in rows}
        reused = {r["url"]: r for r in rows}
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            if record["link_id"] not in finished:
                grouped[record["url"]].append(record)
        hosts: dict[str, deque[str]] = defaultdict(deque)
        for url in grouped:
            hosts[urlsplit(url).hostname or ""].append(url)
        waiting = deque(hosts)
        started = time.monotonic()
        active: dict[Future[dict[str, Any]], tuple[str, str]] = {}
        summary = export(root, rows, len(records), started)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        with (
            checkpoint.open("a", buffering=1) as output,
            ThreadPoolExecutor(max_workers=args.workers) as pool,
        ):

            def save(url: str, result: dict[str, Any]) -> None:
                for record in grouped[url]:
                    row = dict(
                        result,
                        link_id=record["link_id"],
                        source_id=record["source_id"],
                        source=record["source"],
                        checked_at=datetime.now(UTC).isoformat(),
                    )
                    output.write(json.dumps(row, ensure_ascii=False) + "\n")
                    rows.append(row)
                output.flush()
                os.fsync(output.fileno())

            last_print = time.monotonic()
            while waiting or active:
                while waiting and len(active) < args.workers:
                    host = waiting.popleft()
                    url = hosts[host].popleft()
                    if url in reused:
                        save(url, reused[url])
                        if hosts[host]:
                            waiting.append(host)
                    else:
                        active[pool.submit(probe, url, root, args.timeout)] = (host, url)
                if active:
                    completed, _ = wait(active, timeout=5, return_when=FIRST_COMPLETED)
                    for future in completed:
                        host, url = active.pop(future)
                        result = future.result()
                        reused[url] = result
                        save(url, result)
                        if hosts[host]:
                            waiting.append(host)
                summary = export(root, rows, len(records), started)
                if time.monotonic() - last_print >= 20:
                    print(json.dumps(summary, ensure_ascii=False), flush=True)
                    last_print = time.monotonic()
        summary = export(root, rows, len(records), started, complete=True)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
