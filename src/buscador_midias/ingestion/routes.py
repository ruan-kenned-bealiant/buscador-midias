"""Rotas temporais calibradas pelas provas textuais já existentes no catálogo."""

from __future__ import annotations

import re
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

_DATE = re.compile(r"^(https://[^?#]+?)/(20\d{2})/(\d{2})/(\d{2})(/.*|(?:_\d+)?\.xml(?:\.gz)?)$")


def partition(url: str, kind: str) -> tuple[str, datetime] | None:
    """Identifica um formato de rota; sozinho, seu nome não prova uma publicação."""
    if not isinstance(url, str):
        return None
    match = _DATE.fullmatch(url)
    if not match:
        return None
    prefix, year, month, day, suffix = match.groups()
    if kind == "document" and not suffix.endswith((".xml", ".xml.gz")):
        return None
    if kind == "article" and not suffix.startswith("/"):
        return None
    if kind == "article" and prefix.endswith("/noticia"):
        prefix = "https://" + (urlsplit(url).hostname or "") + "/*/noticia"
    try:
        return prefix, datetime(int(year), int(month), int(day), tzinfo=UTC)
    except ValueError:
        return None


def learn(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exige múltiplas provas de publicação e rejeita famílias com contraexemplo."""
    evidence: dict[tuple[str, str], dict[str, tuple[str, str]]] = defaultdict(dict)
    rejected: set[tuple[str, str]] = set()
    for row in rows:
        sample = row["sample"]
        if not isinstance(sample, dict):
            continue
        if not sample.get("text") or not sample.get("published_at"):
            continue
        try:
            published = datetime.fromisoformat(sample["published_at"])
            if published.tzinfo is None:
                continue
        except (ValueError, TypeError):
            continue
        for kind, url in (("article", sample.get("url", "")), ("document", row["url"])):
            found = partition(url, kind)
            if not found:
                continue
            prefix, day = found
            key = kind, prefix
            # Fuso e notícias da véspera podem atravessar a meia-noite; arquivos
            # contendo publicações de períodos diferentes invalidam a inferência.
            if not day - timedelta(days=1) <= published < day + timedelta(days=2):
                rejected.add(key)
            evidence[key][sample["url"]] = (url, published.isoformat())
    rules = []
    for (kind, prefix), samples in evidence.items():
        periods = {partition(url, kind)[1].date() for url, _ in samples.values()}  # type: ignore[index]
        if (kind, prefix) in rejected or len(samples) < 3 or len(periods) < 2:
            continue
        rules.append(
            dict(
                kind=kind,
                prefix=prefix,
                confidence="mapped_samples",
                samples=len(samples),
                periods=len(periods),
                evidence=[dict(url=u, published_at=p) for u, p in samples.values()],
            )
        )
    return rules


class Routes:
    """Aprende por fonte, conserva a prova e permite desativar inferências no mecanismo."""

    def __init__(self, repository: Any) -> None:
        """Mantém cache curto de regras, reconstruído a partir das provas do catálogo."""
        self.repository = repository
        self.cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}

    def rules(self, source_id: int) -> list[dict[str, Any]]:
        """Reavalia o mapeamento a cada cinco minutos e após reinício do serviço."""
        cached = self.cache.get(source_id)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        with self.repository.connection() as db:
            rows = db.execute(
                "SELECT l.url,x.sample FROM links l CROSS JOIN LATERAL "
                "jsonb_array_elements(CASE WHEN jsonb_typeof(l.last_test->'samples')='array' "
                "THEN l.last_test->'samples' ELSE '[]'::jsonb END) x(sample) "
                "WHERE l.source_id=%s AND l.deleted_at IS NULL AND "
                "l.last_test->>'usable_for_news'='true'",
                (source_id,),
            ).fetchall()
            observations = db.execute(
                "SELECT o.document_url AS url,jsonb_build_object('url',a.url,'text',true,"
                "'published_at',a.payload->>'published_at') AS sample FROM ingest_articles a "
                "JOIN ingest_occurrences o ON o.hash=a.hash WHERE a.source_id=%s AND "
                "a.payload->>'published_at_basis'='html' AND "
                "a.payload->>'published_at' IS NOT NULL LIMIT 500",
                (source_id,),
            ).fetchall()
            rows.extend(observations)
            rules = learn(rows)
            db.execute(
                "INSERT INTO ingest_route_profiles(source_id,rules) VALUES(%s,%s) "
                "ON CONFLICT(source_id) DO UPDATE SET rules=excluded.rules,learned_at=now()",
                (source_id, Jsonb(rules)),
            )
        self.cache[source_id] = (time.monotonic() + 300, rules)
        return rules

    def outside(self, job: dict[str, Any], url: str, kind: str) -> dict[str, Any] | None:
        """Exclui apenas partições calibradas, com sobreposição e diagnóstico explícito."""
        parameters = job["snapshot"]["parameters"]
        if not parameters.get("learn_temporal_routes", 1):
            return None
        found = partition(url, kind)
        if not found:
            return None
        prefix, day = found
        rule = next(
            (
                r
                for r in self.rules(job["snapshot"]["source"]["id"])
                if r["kind"] == kind and r["prefix"] == prefix
            ),
            None,
        )
        if not rule:
            return None
        margin = timedelta(days=parameters.get("archive_overlap_days", 2))
        start, end = day - margin, day + timedelta(days=1) + margin
        if start <= job["run"]["until"] and end >= job["run"]["since"]:
            return None
        return dict(
            outcome="particao_fora_da_janela",
            status="outside_window",
            article=dict(
                date_basis="learned_route",
                route_period_since=start.isoformat(),
                route_period_until=end.isoformat(),
            ),
            routing=dict(
                prefix=prefix,
                basis="mapped_text_publication_samples",
                samples=rule["samples"],
                overlap_days=margin.days,
                inferred=True,
            ),
            metrics=dict(
                avoided_article_requests=int(kind == "article"),
                avoided_xml_requests=int(kind == "document"),
            ),
        )
