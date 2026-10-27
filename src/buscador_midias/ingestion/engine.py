"""Executores independentes: RSS/XML, corpus, texto, contexto, evidência e persistência."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from psycopg import sql

from buscador_midias.analysis.context import AnalysisItem, OpenRouterContextAnalyzer
from buscador_midias.analysis.entity_context import CONTEXT_BATCH_SIZE, EntityContext
from buscador_midias.analysis.openrouter import TRUNCATED_RESPONSE, OpenRouterChat
from buscador_midias.analysis.prompt import PROMPT_VERSION
from buscador_midias.catalog.http_policy import retryable
from buscador_midias.catalog.http_transport import make_reader
from buscador_midias.catalog.mechanisms import get_mechanism
from buscador_midias.corpus.article import build_corpus_article
from buscador_midias.corpus.writer import PostgresCorpusWriter
from buscador_midias.criteria import meets_criteria
from buscador_midias.domain.site import Site
from buscador_midias.domain.url import url_hash_candidates
from buscador_midias.evidence.capture import EvidenceCapturer
from buscador_midias.evidence.s3_storage import make_evidence_store
from buscador_midias.extraction.fetch import PageFetch, PageStatus
from buscador_midias.extraction.html import decode_html, extract_article
from buscador_midias.extraction.reliability import PageSnapshot
from buscador_midias.extraction.sample_audit import audit_page
from buscador_midias.ingestion.artifacts import Artifacts
from buscador_midias.ingestion.content_filter import (
    category_exclusion,
    title_exclusion,
    url_exclusion,
)
from buscador_midias.ingestion.document_memo import DocumentMemo
from buscador_midias.ingestion.flow import lastmod_outside, publication_outside
from buscador_midias.ingestion.routes import Routes, partition
from buscador_midias.ingestion.schema import DEFAULTS
from buscador_midias.ingestion.store import IngestStore
from buscador_midias.state.store import CorpusCandidate, FindingUrl


class HostBusyError(Exception):
    """Outro worker está usando o host; adiar não é uma falha da notícia."""

    def __init__(self, message: str = "Host ocupado", retry_after_seconds: int = 5) -> None:
        """Conserva o prazo do servidor sem consumir tentativas de acesso."""
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class RetryableError(Exception):
    """Etapa precisa de nova tentativa e conserva o diagnóstico sanitizado."""

    def __init__(
        self, reason: str, metrics: dict[str, Any] | None = None, *, retryable: bool = True
    ) -> None:
        """Associa consumo já realizado, inclusive quando a resposta do provedor falha."""
        super().__init__(reason)
        self.metrics = metrics or {}
        self.retryable = retryable


class ProviderUnavailableError(RetryableError):
    """Indisponibilidade de conta/modelo/provedor adia a etapa, sem reprovar a fonte."""


class Engine:
    """Reutiliza os componentes de análise e corpus, sem simular uma diligência."""

    def __init__(
        self,
        repository: IngestStore,
        *,
        artifacts: Artifacts | None = None,
        reader_factory: Any = None,
        analyzer_factory: Any = None,
        evidence_store: Any = None,
        writer_factory: Any = None,
        corpus_schema: str = "public",
        host_slots: Callable[[], int] | None = None,
    ) -> None:
        """Portas substituíveis permitem testar o pipeline completo sem gastos externos."""
        self.repository = repository
        self.artifacts = artifacts or Artifacts()
        self.reader_factory = reader_factory
        self.analyzer_factory = analyzer_factory
        self.evidence_store = evidence_store
        self.writer_factory = writer_factory
        self.corpus_schema = corpus_schema
        self.routes = Routes(repository)
        self.document_memo = DocumentMemo()
        # Vagas simultâneas por host; o serviço liga isto à configuração salva.
        self.host_slots: Callable[[], int] = host_slots or (lambda: 1)
        self._country_registry: dict[str, str] | None = None
        self._country_registry_at = 0.0
        self._country_registry_lock = threading.Lock()

    @staticmethod
    def site(job: dict[str, Any]) -> Site:
        """Mantém o mesmo escopo editorial dos testes do gerenciador."""
        source = job["snapshot"]["source"]
        return Site.create(
            name=source["name"],
            url=source["url"],
            region="",
            country="",
            country_code="",
            language="",
            source_type="",
        )

    @staticmethod
    def reserve_host(lock_db: Any, host: str, slots: int = 1) -> None:
        """Ocupa uma das ``slots`` vagas de acesso ao host até o fim da transação.

        A vaga 0 usa a mesma chave de antes, então processos antigos e novos continuam
        excluindo-se mutuamente enquanto ``slots`` for 1.
        """
        for slot in range(slots):
            row = lock_db.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s),%s) locked",
                (host, 674931220 + slot),
            ).fetchone()
            if row["locked"]:
                return
        raise HostBusyError()

    @contextmanager
    def reader(self, job: dict[str, Any], roots: list[str]) -> Iterator[Any]:
        """Trava por host real entre processos; usa somente HTTP direto."""
        if self.reader_factory:
            yield self.reader_factory(roots, lambda _: None)
            return
        with self.repository.lease_connection() as lock_db:
            acquired = set()

            def reserve(host: str) -> None:
                if host in acquired:
                    return
                self.reserve_host(lock_db, host, max(1, int(self.host_slots())))
                cooldown = lock_db.execute(
                    "SELECT ceil(extract(epoch FROM available_at-now()))::bigint seconds "
                    "FROM ingest_host_cooldowns WHERE host=%s AND available_at>now()",
                    (host,),
                ).fetchone()
                if cooldown:
                    raise HostBusyError("Host aguardando Retry-After", int(cooldown["seconds"]))
                acquired.add(host)

            reader = make_reader(
                roots,
                lambda _: None,
                transport=job["snapshot"].get("access_transport", "direct"),
                seconds=60,
                requests=24,
                max_bytes=16 * 1024 * 1024,
                before_request=reserve,
            )
            try:
                yield reader
            finally:
                if hasattr(reader, "close"):
                    reader.close()

    @staticmethod
    def metrics(reader: Any) -> dict[str, Any]:
        """Contadores do transporte, sem credenciais ou conteúdo das respostas de API."""
        result = dict(
            requests=reader.requests,
            bright_data_requests=getattr(reader, "bright_requests", 0),
            connections_reused=getattr(reader, "connections_reused", 0),
            cookies_sent=getattr(reader, "cookies_sent", 0),
            access_transport=getattr(reader, "transport", "direct"),
            http_protocols=sorted(
                {event["protocol"] for event in getattr(reader, "transport_events", [])}
            ),
        )
        if getattr(reader, "retry_after_seconds", None) is not None:
            result.update(
                retry_after_seconds=reader.retry_after_seconds,
                retry_after_host=reader.retry_after_host,
            )
        return result

    def http_failure(self, reader: Any, response: Any, message: str) -> None:
        """Classifica falhas antes de reenfileirar; não repete erros permanentes."""
        if getattr(reader, "cooldown_deferred", False):
            raise HostBusyError("Host aguardando Retry-After", reader.retry_after_seconds or 5)
        raise RetryableError(
            response.error or f"HTTP {response.status} {message}",
            self.metrics(reader),
            retryable=retryable(response.status, response.error)
            or (response.status == 403 and bool(getattr(reader, "session_updated", False))),
        )

    def execute(self, job: dict[str, Any]) -> dict[str, Any]:
        """Uma mensagem executa somente sua etapa, sem chamar o próximo worker."""
        return dict(getattr(self, job["stage"])(job))

    def discovery(self, job: dict[str, Any]) -> dict[str, Any]:
        """Um documento por tarefa; filhos são enfileirados no commit, com deduplicação."""
        snapshot = job["snapshot"]
        site = self.site(job)
        daily = job["run"]["config"].get("window_mode", "today") == "today"
        with self.repository.connection() as db:
            current = db.execute(
                "SELECT l.active,l.deleted_at,l.version,s.active sa,s.deleted_at sd,"
                "s.version sv,m.version mv FROM links l JOIN sites s ON s.id=l.source_id "
                "JOIN mechanisms m ON m.model=l.model WHERE l.id=%s",
                (snapshot["link_id"],),
            ).fetchone()
        if (
            not current
            or not current["active"]
            or not current["sa"]
            or current["deleted_at"]
            or current["sd"]
        ):
            return dict(outcome="cadastro_inativo")
        if (current["version"], current["sv"], current["mv"]) != (
            snapshot["link_version"],
            snapshot["source"]["version"],
            snapshot["model_version"],
        ):
            return dict(outcome="cadastro_alterado")
        route = (
            self.routes.outside(job, job["payload"]["url"], "document")
            if snapshot["model"] != "g1_daily"
            else None
        )
        if route:
            route.pop("article", None)
            route.pop("status", None)
            return route
        if job["payload"].get("plan_key"):
            plan = self.repository.read_plan(job["payload"]["plan_key"])
            return self.discovery_slice(job, plan, job["payload"]["plan_key"])
        if (
            not self.reader_factory
            and shutil.disk_usage(
                self.artifacts.root.parent
                if self.artifacts.root.parent.exists()
                else self.artifacts.root.parents[1]
            ).free
            < 128 * 1024 * 1024
        ):
            raise HostBusyError(
                "Armazenamento de trabalho abaixo da reserva; aguardando escoamento"
            )
        url = job["payload"]["url"]
        mechanism = get_mechanism(snapshot["model"], snapshot["kind"])
        with self.reader(job, [site.url, snapshot["url"]]) as reader:
            use_cache = daily or snapshot["model"] == "g1_daily"
            cached = self.repository.cached_document(job) if use_cache else None
            if cached and hasattr(reader, "conditional_headers"):
                reader.conditional_headers[url] = {
                    key: value
                    for key, value in (
                        ("If-None-Match", cached["etag"]),
                        ("If-Modified-Since", cached["last_modified"]),
                    )
                    if value
                }
            headers = getattr(reader, "conditional_headers", {}).get(url, {})
            memo_key = (
                job["run"]["id"],
                site.key,
                snapshot.get("access_transport", "direct"),
                url,
                headers.get("If-None-Match", ""),
                headers.get("If-Modified-Since", ""),
            )
            response, shared = self.document_memo.get(memo_key, lambda: reader.get(url))
            metrics = self.metrics(reader)
            if shared:
                metrics["shared_xml_http_hits"] = 1
                metrics["avoided_duplicate_xml_requests"] = 1
            if not response.ok and not (response.status == 304 and cached):
                self.http_failure(reader, response, "no XML")
            try:
                if cached and (
                    response.status == 304
                    or hashlib.sha256(response.content).hexdigest() == cached["content_hash"]
                ):
                    document = cached["document"]
                    metrics["cached_xml_parses"] = 1
                    metrics["not_modified_responses"] = int(response.status == 304)
                else:
                    document = mechanism.read(
                        response.content, response.final_url or url, snapshot["parameters"]
                    )
            except ValueError as error:
                raise RetryableError("XML inválido: " + str(error)[:180], metrics) from error
            if not mechanism.accepts(document):
                raise RetryableError("Mecanismo incompatível com o XML", metrics)
            if use_cache and response.status != 304:
                self.repository.cache_document(job, document, response)
            entries = []
            outside_urls = 0
            lastmod_excluded = 0
            lastmod_mode = job["run"]["config"].get("lastmod_filter", "shadow")
            modified = document.get("hints") or {}
            # Datas de publicação de feed/news sitemap são pistas; lastmod nunca vira publicação,
            # mas um lastmod anterior à janela prova que a publicação também foi.
            for address in document["entries"]:
                if not site.owns(address):
                    continue
                candidate = dict(
                    url=address,
                    published_hint=document["dates"].get(address),
                    published_hint_basis="xml_publication",
                )
                if daily and publication_outside(
                    dict(article=dict(payload=candidate), run=job["run"])
                ):
                    outside_urls += 1
                    continue
                if (
                    lastmod_mode != "off"
                    and address not in document["dates"]
                    and (found := lastmod_outside(modified.get(address), job["run"]["since"]))
                ):
                    if lastmod_mode == "enforce":
                        lastmod_excluded += 1
                        continue
                    candidate["shadow_lastmod_exclusion"] = found
                entries.append(candidate)
            metrics["outside_window_urls"] = outside_urls
            metrics["lastmod_excluded_urls"] = lastmod_excluded
            collection = mechanism.plan_collection(
                document, job["run"]["since"], job["run"]["until"], snapshot["parameters"]
            )
            children = list(collection.children)
            unrecognized = list(collection.unrecognized)
            daily_skipped = 0
            if daily and document["index"] and snapshot["model"] != "g1_daily":
                selected = []
                for child in children:
                    stamp = document["child_dates"].get(child)
                    try:
                        modified = datetime.fromisoformat(stamp) if stamp else None
                    except ValueError:
                        modified = None
                    if modified is None or modified.tzinfo is None:
                        unrecognized.append(child)
                    elif modified >= job["run"]["since"]:
                        selected.append(child)
                    else:
                        daily_skipped += 1
                children = selected
            children = list(dict.fromkeys(children))

            # Metadados lastmod e nomes de caminhos ordenam; só uma regra calibrada exclui.
            def priority(address: str) -> tuple[int, str]:
                stamp = document["child_dates"].get(address)
                if stamp:
                    return 2, stamp
                inferred = partition(address, "document")
                return (1, inferred[1].isoformat()) if inferred else (0, "")

            children.sort(key=priority, reverse=True)
            selected = []
            learned_exclusions = 0
            for child in children:
                if snapshot["model"] != "g1_daily" and self.routes.outside(job, child, "document"):
                    learned_exclusions += 1
                else:
                    selected.append(child)
            children = selected
            metrics["avoided_xml_requests"] = (
                learned_exclusions + collection.skipped + daily_skipped
            )
            maximum = snapshot["parameters"].get("max_depth", 10)
            if children and job["payload"]["depth"] >= maximum:
                return dict(
                    outcome="profundidade_limitada",
                    candidates=entries,
                    metrics=metrics,
                    coverage_partial=True,
                )
            plan = dict(
                outcome="urls_encontradas" if entries or children else "sem_novidades",
                candidates=entries,
                children=children,
                metrics=metrics,
                archive_routes_skipped=learned_exclusions,
                calendar_routes_skipped=collection.skipped,
                daily_routes_skipped=daily_skipped,
                collection_strategy="daily_index_lastmod_candidates"
                if daily and document["index"] and snapshot["model"] != "g1_daily"
                else collection.strategy,
                unrecognized_children=unrecognized,
                coverage_partial=bool(unrecognized),
            )
            if len(entries) <= 500 and len(children) <= 64:
                return plan
            key = self.repository.save_plan(job, plan)
            return self.discovery_slice(job, plan, key)

    @staticmethod
    def discovery_slice(job: dict[str, Any], plan: dict[str, Any], key: str) -> dict[str, Any]:
        """Continua um XML em lotes sem relê-lo nem perder as URLs ainda não emitidas."""
        cursor = job["payload"].get("cursor", 0)
        entries, children = plan["candidates"], plan["children"]
        result = dict(
            plan,
            candidates=entries[cursor * 500 : (cursor + 1) * 500],
            children=children[cursor * 64 : (cursor + 1) * 64],
            plan_key=key,
        )
        result["document_candidates"] = len(entries)
        result["document_children"] = len(children)
        if cursor:
            result["metrics"] = dict(cached_document_continuations=1)
            result["outcome"] = "documento_continuacao"
        if (cursor + 1) * 500 < len(entries) or (cursor + 1) * 64 < len(children):
            result["continuation"] = dict(plan_key=key, cursor=cursor + 1)
        return result

    def dedup(self, job: dict[str, Any]) -> dict[str, Any]:
        """Consulta o corpus antes de qualquer análise paga; considera hashes legados."""
        return self.dedup_many([job])[0]

    def dedup_many(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Consulta completude em lote e carrega análise/entidades apenas para reparos."""
        hashes = {j["id"]: list(url_hash_candidates(j["article"]["url"])) for j in jobs}
        started = time.perf_counter()
        with self.repository.connection() as db:
            query = sql.SQL(
                "SELECT a.id,a.url_hash,a.evidencia_status,a.evidencia_path,"
                "(length(a.body)>0) has_body,a.ai_model,EXISTS(SELECT 1 FROM "
                "{}.midias_ia_article_entities e WHERE e.article_id=a.id) has_entities "
                "FROM {}.midias_ia_articles a WHERE a.url_hash=ANY(%s)"
            ).format(sql.Identifier(self.corpus_schema), sql.Identifier(self.corpus_schema))
            rows = db.execute(query, (list({h for v in hashes.values() for h in v}),)).fetchall()
            repair_ids = [r["id"] for r in rows if not self.corpus_complete(r)]
            if repair_ids:
                query = sql.SQL(
                    "SELECT a.id,a.ai_verdict,a.ai_reason,a.ai_summary,a.resumo,a.ai_score,"
                    "a.content_category,a.source_country,COALESCE((SELECT jsonb_agg("
                    "jsonb_build_object('name',e.name,'entity_type',e.entity_type,'role',e.role)) "
                    "FROM {}.midias_ia_article_entities e WHERE e.article_id=a.id),"
                    "'[]'::jsonb) entities FROM {}.midias_ia_articles a WHERE a.id=ANY(%s)"
                ).format(sql.Identifier(self.corpus_schema), sql.Identifier(self.corpus_schema))
                repairs = {r["id"]: r for r in db.execute(query, (repair_ids,)).fetchall()}
                rows = [
                    dict(r, **{k: v for k, v in repairs.get(r["id"], {}).items() if k != "id"})
                    for r in rows
                ]
        lookup_seconds = time.perf_counter() - started
        existing_by_hash = {r["url_hash"]: r for r in rows}
        results = []
        for job in jobs:
            matches = [existing_by_hash[h] for h in hashes[job["id"]] if h in existing_by_hash]
            matches.sort(key=lambda r: r["evidencia_status"] == "captured", reverse=True)
            result = self.dedup_result(job, matches[0] if matches else None)
            result.setdefault("metrics", {}).update(
                dedup_batch_size=len(jobs), corpus_lookup_seconds=round(lookup_seconds, 6)
            )
            results.append(result)
        return results

    @staticmethod
    def corpus_complete(existing: Any) -> bool:
        """Comprova completude sem carregar entidades e resumos de artigos já conhecidos."""
        return bool(
            existing["evidencia_status"] == "captured"
            and existing["evidencia_path"]
            and existing.get("has_body")
            and existing.get("ai_model")
            and existing.get("has_entities", bool(existing.get("entities")))
        )

    def dedup_result(self, job: dict[str, Any], existing: Any) -> dict[str, Any]:
        """Reutiliza análise existente e evita baixar publicação explicitamente fora da janela."""
        if existing and self.corpus_complete(existing):
            return dict(outcome="existente", status="known", article_id=existing["id"])
        outside = publication_outside(job) or self.routes.outside(
            job, job["article"]["url"], "article"
        )
        if outside:
            return outside
        if existing:
            entities = existing["entities"]
            patch = dict(
                existing_hash=existing["url_hash"],
                existing_article_id=existing["id"],
                existing_source_country=existing.get("source_country"),
            )
            if entities and existing.get("ai_model"):
                analysis = dict(
                    entities=[
                        dict(
                            name=e["name"],
                            type="PF" if e["entity_type"] == "PER" else "PJ",
                            role=e["role"] or "citado",
                        )
                        for e in entities
                    ],
                    finding=existing.get("ai_verdict") == "relevante",
                    themes=[],
                    adverse_fact=existing.get("ai_reason") or "",
                    summary=existing.get("ai_summary") or existing.get("resumo") or "",
                    confidence=float(existing.get("ai_score") or 0),
                    basis="text",
                    model=existing["ai_model"],
                    prompt_version="reused-corpus",
                    content_category=existing.get("content_category"),
                )
                patch["analysis"] = analysis
            return dict(
                next="fetch",
                outcome="existente_reparar_evidencia",
                article=patch,
                article_id=existing["id"],
            )
        return dict(next="fetch", outcome="nova_url")

    def fetch(self, job: dict[str, Any]) -> dict[str, Any]:
        """Confirma notícia textual, aplica janela e guarda HTML para a evidência."""
        outside = publication_outside(job) or self.routes.outside(
            job, job["article"]["url"], "article"
        )
        if outside:
            return outside
        article = job["article"]
        url = article["url"]
        site = self.site(job)
        mode = job["run"]["config"].get("content_filter", "shadow")
        # Artigo já no corpus só precisa de reparo de evidência: nada a excluir aqui.
        filtering = mode != "off" and not article["payload"].get("existing_hash")
        flag = None
        if filtering and (found := url_exclusion(url)):
            if mode == "enforce":
                return dict(
                    outcome="conteudo_sem_interesse",
                    status="excluded_content",
                    article=dict(exclusion=asdict(found)),
                    metrics=dict(avoided_article_requests=1),
                )
            flag = asdict(found)
        with self.reader(job, [site.url, job["snapshot"]["url"]]) as reader:
            response = reader.get(url)
            metrics = self.metrics(reader)
            if not response.ok:
                self.http_failure(reader, response, "na notícia")
            page = PageSnapshot(
                url, response.status, response.content_type, response.final_url, response.content
            )
            run = job["run"]
            audit = audit_page(page, since=run["since"], until=run["until"], now=datetime.now(UTC))
            if not audit["texto_util_confirmado"]:
                return dict(
                    outcome=audit["categoria"],
                    status="no_text",
                    metrics=metrics,
                    article=dict(diagnostic=audit["motivos"]),
                )
            content = extract_article(
                decode_html(response.content, response.content_type),
                max_chars=30000,
                now=datetime.now(UTC),
            )
            published = content.published_at
            publication_basis = "html" if published else None
            if (
                published is None
                and article["payload"].get("published_hint")
                and article["payload"].get("published_hint_basis") == "xml_publication"
            ):
                with suppress(ValueError, TypeError):
                    published = datetime.fromisoformat(article["payload"]["published_hint"])
                    publication_basis = "xml_hint"
            patch = dict(
                title=content.title,
                description=content.description,
                body=content.text,
                published_at=published.isoformat() if published else None,
                published_at_basis=publication_basis,
                language=content.language,
                final_url=response.final_url,
                content_type=response.content_type,
            )
            if published and published.tzinfo is None:
                published = None
                patch["published_at"] = None
            if published and not run["since"] <= published <= run["until"]:
                patch.pop("body", None)
                return dict(
                    outcome="fora_da_janela",
                    status="outside_window",
                    article=patch,
                    metrics=metrics,
                )
            if not published and (
                not run["config"]["allow_undated"]
                or run["config"].get("window_mode", "today") == "today"
            ):
                patch.pop("body", None)
                return dict(
                    outcome="data_desconhecida",
                    status="date_unknown",
                    article=patch,
                    metrics=metrics,
                )
            if filtering and (found := title_exclusion(url, patch.get("title"))):
                if mode == "enforce":
                    patch.pop("body", None)
                    return dict(
                        outcome="conteudo_sem_interesse",
                        status="excluded_content",
                        article=dict(patch, exclusion=asdict(found)),
                        metrics=metrics,
                    )
                flag = flag or asdict(found)
            if flag:
                patch["shadow_exclusion"] = flag
            self.artifacts.put(article["hash"], response.content)
            return dict(
                next="evidence" if article["payload"].get("analysis") else "context",
                outcome="texto_confirmado",
                article=patch,
                metrics=metrics,
            )

    def context(self, job: dict[str, Any]) -> dict[str, Any]:
        """Identifica entidades e completa pareceres individuais em lotes retomáveis."""
        data = job["article"]["payload"]
        config = job["run"]["config"]
        if not self.analyzer_factory and not os.environ.get("OPENROUTER_API_KEY"):
            raise RetryableError("OpenRouter não configurado")
        with httpx.Client(trust_env=False) as client:
            analyzer = (
                self.analyzer_factory()
                if self.analyzer_factory
                else OpenRouterContextAnalyzer(
                    OpenRouterChat(
                        client,
                        api_key=os.environ["OPENROUTER_API_KEY"],
                        model=config["model"],
                        base_url=os.environ.get("OPENROUTER_BASE_URL")
                        or "https://openrouter.ai/api/v1",
                        attempts=1,
                    ),
                    max_text_chars=config["analysis_text_chars"],
                )
            )
            before = analyzer.usage if hasattr(analyzer, "usage") else None
            item = AnalysisItem(
                1,
                urlsplit(job["article"]["url"]).hostname or "",
                data.get("title") or "",
                data.get("description") or "",
                data["body"][: config["analysis_text_chars"]],
                "text",
            )
            signature = hashlib.sha256(
                json.dumps([asdict(item), config["model"], PROMPT_VERSION], sort_keys=True).encode()
            ).hexdigest()
            progress = data.get("entity_context_progress") or {}
            if progress.get("signature") == signature:
                verdict = dict(progress["analysis"])
            else:
                result = analyzer.analyze([item])
                usage = asdict(analyzer.usage.minus(before)) if before else {}
                if not result or not result[0].decided:
                    error = usage.get("last_error")
                    if error and error != TRUNCATED_RESPONSE:
                        raise ProviderUnavailableError("OpenRouter indisponível: " + error, usage)
                    raise RetryableError("Análise sem resposta válida", usage)
                verdict = asdict(result[0])
                verdict.update(model=config["model"], prompt_version=PROMPT_VERSION)
                verdict["entity_limit_reached"] = len(verdict["entities"]) >= 30
                progress = dict(signature=signature, analysis=verdict, contexts=[])
            usage = asdict(analyzer.usage.minus(before)) if before else {}
            if not meets_criteria(basis="text", entities=verdict["entities"]):
                self.artifacts.remove(job["hash"])
                return dict(
                    status="no_entities",
                    outcome="sem_entidade_identificavel",
                    article=dict(analysis=verdict, body=None),
                    metrics=usage,
                )
            blocked = config.get(
                "blocked_content_categories", DEFAULTS["blocked_content_categories"]
            )
            found = category_exclusion(
                verdict.get("content_category"), bool(verdict["finding"]), blocked
            )
            if found and not data.get("existing_hash"):
                self.artifacts.remove(job["hash"])
                return dict(
                    status="excluded_content",
                    outcome="categoria_sem_interesse",
                    article=dict(analysis=verdict, body=None, exclusion=asdict(found)),
                    metrics=usage,
                )
            completed = {(c["name"], c["entity_type"]): c for c in progress["contexts"]}
            missing = [e for e in verdict["entities"] if (e["name"], e["type"]) not in completed]
            self.repository.save_entity_context_progress(job, progress)
            for batches, start in enumerate(range(0, len(missing), CONTEXT_BATCH_SIZE), 1):
                batch = missing[start : start + CONTEXT_BATCH_SIZE]
                expected = {(e["name"], e["type"]) for e in batch}
                individual = analyzer.analyze_entities(item, batch)
                for context in individual:
                    key = (context.name, context.entity_type)
                    if key in expected:
                        completed[key] = asdict(context)
                progress["contexts"] = list(completed.values())
                progress["validation"] = dict(getattr(analyzer, "context_validation", {}))
                self.repository.save_entity_context_progress(job, progress)
                usage = asdict(analyzer.usage.minus(before)) if before else {}
                usage.update(
                    entity_context_batches=batches,
                    entity_context_completed=len(completed),
                    entity_context_validation=progress["validation"],
                )
                if not expected.issubset(completed):
                    error = usage.get("last_error")
                    if error and error != TRUNCATED_RESPONSE:
                        raise ProviderUnavailableError("OpenRouter indisponível: " + error, usage)
                    raise RetryableError("Contexto individual incompleto; lote pendente", usage)
            verdict["entity_contexts"] = list(completed.values())
            return dict(
                next="evidence",
                outcome="analise_concluida",
                article=dict(analysis=verdict),
                metrics=usage,
                coverage_partial=bool(verdict.get("entity_limit_reached")),
            )

    def evidence(self, job: dict[str, Any]) -> dict[str, Any]:
        """Reaproveita HTML confiável e captura apenas quando necessário pela política."""
        data = job["article"]["payload"]
        analysis = data["analysis"]
        site = self.site(job)
        config = job["run"]["config"]
        key = job["hash"]
        store = self.evidence_store or make_evidence_store(
            os.environ, self.artifacts.root.parent / "evidence"
        )
        body = self.artifacts.read(key)
        page = PageFetch(
            job["article"]["url"],
            PageStatus.OK,
            200,
            body,
            data.get("content_type"),
            data.get("final_url"),
        )
        # O capturador reaproveita o HTML HTTP já salvo. Transporte pago está desativado.
        finding = FindingUrl(
            data.get("existing_hash", key),
            job["article"]["url"],
            data.get("title"),
            site.key,
            tuple(e["name"] for e in analysis["entities"]),
            analysis["finding"] or not config["evidence_paid_adverse_only"],
        )
        capturer = EvidenceCapturer(
            store,
            bright_data=None,
            reuse=True,
            now=lambda: datetime.now(UTC),
        )
        captured = capturer.capture(finding, page, expected_headline=data.get("title") or "")
        record = asdict(captured)
        metrics = dict(
            requests=0,
            bright_data_requests=0,
            connections_reused=0,
            cookies_sent=0,
            access_transport=job["snapshot"].get("access_transport", "direct"),
            http_protocols=[],
        )
        if captured.status != "captured" and job["attempts"] < min(3, config["max_attempts"]):
            raise RetryableError("Evidência não confirmada; texto e análise preservados", metrics)
        return dict(
            next="persist",
            outcome="evidencia_capturada"
            if captured.status == "captured"
            else "evidencia_pendente",
            article=dict(evidence=record),
            metrics=metrics,
        )

    def persist(self, job: dict[str, Any]) -> dict[str, Any]:
        """Grava com o writer idempotente existente e preserva pareceres de clientes."""
        article = job["article"]
        data = article["payload"]
        analysis = data["analysis"]
        evidence = data["evidence"]
        site = self.site(job)
        candidate = CorpusCandidate(
            data.get("existing_hash", article["hash"]),
            article["url"],
            site.key,
            data.get("title"),
            data.get("description"),
            data.get("body"),
            datetime.fromisoformat(data["published_at"]) if data.get("published_at") else None,
            data.get("language"),
            tuple(analysis["themes"]),
            tuple(analysis["entities"]),
            analysis["adverse_fact"],
            analysis["summary"],
            analysis["confidence"],
            "text",
            analysis["model"],
            evidence["status"],
            evidence.get("path"),
            evidence.get("attempts", job["attempts"]),
            evidence.get("provider"),
            analysis["finding"],
            content_category=analysis.get("content_category"),
            article_country=analysis.get("article_country") or data.get("existing_source_country"),
            entity_contexts=tuple(
                EntityContext(**dict(context, themes=tuple(context.get("themes", ()))))
                for context in analysis.get("entity_contexts", ())
            ),
            context_prompt_version=analysis.get("prompt_version"),
        )
        writer = (
            self.writer_factory()
            if self.writer_factory
            else PostgresCorpusWriter.from_env(os.environ)
        )
        try:
            if not self.writer_factory:
                writer.check_schema()
            countries = None
            if hasattr(writer, "source_countries"):
                with self._country_registry_lock:
                    if (
                        self._country_registry is None
                        or time.monotonic() - self._country_registry_at > 300
                    ):
                        self._country_registry = writer.source_countries()
                        self._country_registry_at = time.monotonic()
                    countries = self._country_registry
            corpus_article = build_corpus_article(
                candidate, site, source_names={site.host: site.name}, source_countries=countries
            )
            results = writer.write([corpus_article])
        finally:
            if not self.writer_factory:
                writer.close()
        result = results[0]
        if result.error or not result.article_id:
            raise RetryableError("Não foi possível gravar artigo no corpus")
        # A transação do corpus já confirmou; uma reentrega apenas reconhece o url_hash.
        self.artifacts.remove(article["hash"])
        return dict(
            outcome="gravada" if result.created else "reutilizada_na_gravacao",
            status="persisted" if evidence["status"] == "captured" else "evidence_pending",
            article_id=result.article_id,
            article=dict(persistence=asdict(result), body=None),
        )
