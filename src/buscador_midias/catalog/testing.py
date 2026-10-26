"""Testes interativos em amostra, com progresso e resultado independente de recência."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urljoin

from buscador_midias.catalog.bright_reader import BrightTestReader, bright_data_available
from buscador_midias.catalog.cadence import estimate_cadence
from buscador_midias.catalog.http_transport import make_reader, validate_transport
from buscador_midias.catalog.mechanisms import get_mechanism
from buscador_midias.catalog.reader import TestLimitError, TestReader
from buscador_midias.catalog.store import CatalogError, CatalogStore, clean_url
from buscador_midias.domain.site import Site
from buscador_midias.extraction.reliability import PageSnapshot
from buscador_midias.extraction.sample_audit import audit_page
from buscador_midias.net.fetcher import FetchResult

_LOG = logging.getLogger(__name__)


class Reader(Protocol):
    """Porta injetável de leitura com orçamento por teste."""

    requests: int

    def get(self, url: str) -> FetchResult:
        """Obtém uma resposta dentro do escopo e dos limites."""
        ...


ReaderFactory = Callable[[list[str], Callable[[dict[str, object]], None]], Reader]


def run_test(
    config: dict[str, Any],
    source: dict[str, Any],
    progress: Callable[[dict[str, object]], None],
    factory: ReaderFactory | None = None,
) -> dict[str, Any]:
    """Executa a amostra configurada; HTTP 200 isolado nunca confirma texto."""
    started = time.monotonic()
    now = (
        datetime.fromisoformat(config["window_until"])
        if config.get("window_until")
        else datetime.now(UTC)
    )
    mechanism = get_mechanism(config["model"], config["kind"])
    parameters = mechanism.validate(config.get("parameters", {}))
    since = (
        now - timedelta(days=parameters["lookback_days"])
        if parameters["lookback_days"]
        else datetime.min.replace(tzinfo=UTC)
    )
    dates: dict[str, datetime] = {}
    site = Site.create(
        name=source["name"],
        url=source["url"],
        region="",
        country="",
        country_code="",
        language="",
        source_type="",
    )
    roots = [source["url"], config["url"]]
    transport = validate_transport(config.get("access_transport", "direct"))
    use_bright = bool(config.get("bright_data")) and transport == "direct"
    reader = (
        factory(roots, progress)
        if factory is not None
        else (
            BrightTestReader if use_bright else TestReader if transport == "direct" else make_reader
        )(
            roots,
            progress,
            seconds=parameters["timeout_seconds"],
            requests=parameters["max_requests"],
            max_bytes=parameters["max_response_mib"] * 1024 * 1024,
            **(
                {"max_bright_requests": config.get("max_bright_requests", 8)}
                if use_bright
                else {"transport": transport}
                if transport != "direct"
                else {}
            ),
        )
    )
    result: dict[str, Any] = dict(
        window_since=since.isoformat(),
        window_until=now.isoformat(),
        test_id=config["test_id"],
        source_id=source["id"],
        source_url=source["url"],
        url=config["url"],
        model=config["model"],
        model_version=config.get("model_version", 1),
        parameters=parameters,
        requested_kind=config["kind"],
        kind=None,
        endpoint_accessible=False,
        xml_valid=False,
        compatible=False,
        root_entries=0,
        root_children=0,
        root_index=False,
        xml_documents=0,
        news_urls=0,
        samples=[],
        text_samples=0,
        outcome="inconclusive",
        warnings=[],
        complete=True,
        observed_at=datetime.now(UTC).isoformat(),
    )
    queue = deque([(config["url"], 0)])
    scheduled = {config["url"]}
    candidates: dict[str, None] = {}
    root_loaded = False
    candidate_documents = 0
    try:
        while queue:
            if candidates and (
                result["xml_documents"] >= parameters.get("max_documents", 1)
                or time.monotonic() - started >= parameters["timeout_seconds"] * 0.5
                or reader.requests >= parameters["max_requests"] - 8
            ):
                result["complete"] = False
                result["warnings"].append(
                    "Amostra XML limitada para reservar orçamento às notícias."
                )
                break
            if result["xml_documents"] >= parameters.get("max_documents", 1):
                raise TestLimitError(
                    f"Amostra limitada a {parameters.get('max_documents', 1)} documentos XML; "
                    "há caminhos não testados."
                )
            url, depth = queue.popleft()
            progress(dict(stage="xml", message=f"Lendo XML no nível {depth}", url=url))
            response = reader.get(url)
            result["xml_documents"] += 1
            if not root_loaded:
                result["endpoint_accessible"] = response.ok
                result["endpoint_status"] = response.status
                result["endpoint_error"] = response.error
            if not response.ok:
                message = response.error or f"HTTP {response.status}"
                result["warnings"].append(message)
                result["complete"] = False
                if not root_loaded:
                    result["outcome"] = (
                        "blocked"
                        if response.status in (401, 403, 429) or ("robots_" in message)
                        else "inconclusive"
                    )
                    break
                continue
            try:
                document = mechanism.read(response.content, response.final_url or url, parameters)
            except ValueError as error:
                result["warnings"].append(str(error)[:240])
                result["complete"] = False
                if not root_loaded:
                    result["outcome"] = "inconclusive" if "acima" in str(error) else "invalid"
                    break
                continue
            if not root_loaded:
                root_loaded = True
                result.update(
                    xml_valid=True,
                    kind=document["kind"],
                    root_entries=len(set(document["entries"])),
                    root_children=len(set(document["children"])),
                    root_index=document["index"],
                )
                compatible = mechanism.accepts(document)
                result["requested_kind"] = mechanism.requested_kind(config["kind"], document)
                result["compatible"] = compatible
                if not compatible:
                    result["outcome"] = "incompatible"
                    result["warnings"].append(
                        "XML interpretado, mas incompatível com o modelo/tipo."
                    )
                    break
            elif document["kind"] != result["kind"]:
                result["warnings"].append("Um filho tem formato diferente do documento inicial.")
                result["complete"] = False
                continue
            entries = document["entries"]
            if parameters["lookback_days"]:
                entries = sorted(entries, key=lambda u: document["hints"].get(u, ""), reverse=True)
            before_candidates = len(candidates)
            for candidate in entries:
                try:
                    candidate = clean_url(urljoin(response.final_url or url, candidate))
                    if not site.owns(candidate):
                        result["warnings"].append(
                            "URLs fora da fonte configurada não foram amostradas."
                        )
                        continue
                    stamp = document["dates"].get(candidate)
                    if stamp:
                        published = datetime.fromisoformat(stamp)
                        if not since <= published <= now:
                            continue
                        if len(dates) < 5000:
                            dates[candidate] = published
                    if len(candidates) < 5000:
                        candidates[candidate] = None
                    else:
                        result["complete"] = False
                        result["warnings"].append("Retidas até 5.000 URLs candidatas na amostra.")
                except CatalogError:
                    continue
            if len(candidates) > before_candidates:
                candidate_documents += 1
            if mechanism.expand:
                children = mechanism.children(document)
                if parameters["lookback_days"]:
                    children = sorted(
                        children, key=lambda u: document["child_dates"].get(u, ""), reverse=True
                    )
                for child in children:
                    try:
                        child = clean_url(urljoin(response.final_url or url, child))
                    except CatalogError:
                        result["complete"] = False
                        continue
                    if child in scheduled:
                        continue
                    if depth >= parameters.get("max_depth", 0):
                        result["warnings"].append(
                            f"Profundidade máxima {parameters.get('max_depth', 0)}; "
                            "filhos restantes não lidos."
                        )
                        result["complete"] = False
                        continue
                    scheduled.add(child)
                    queue.append((child, depth + 1))
            # Um teste interativo comprova um caminho; não varre um arquivo histórico inteiro.
            if candidates and (
                not queue or not parameters["lookback_days"] or candidate_documents >= 3
            ):
                if queue:
                    result["complete"] = False
                    result["warnings"].append(
                        "Caminho até notícias encontrado; outros filhos não lidos."
                    )
                break
        result["news_urls"] = len(candidates)
        urls = list(candidates)
        count = min(parameters["article_samples"], len(urls))
        if parameters["sample_order"] == "first":
            positions = list(range(count))
        elif parameters["sample_order"] == "last":
            positions = list(range(len(urls) - count, len(urls)))
        elif count <= 1:
            positions = [0] if count else []
        else:
            positions = [min(len(urls) - 1, i * len(urls) // (count - 1)) for i in range(count)]
        for position in positions:
            url = urls[position]
            progress(dict(stage="article", message="Conferindo notícia de exemplo", url=url))
            response = reader.get(url)
            if response.final_url and not site.owns(response.final_url):
                result["samples"].append(
                    dict(
                        url=url,
                        text=False,
                        accessed=False,
                        article=False,
                        title="",
                        reason="Redirecionamento fora da fonte",
                    )
                )
                continue
            if not response.ok:
                result["samples"].append(
                    dict(
                        url=url,
                        text=False,
                        accessed=False,
                        article=False,
                        title="",
                        status=response.status,
                        reason=response.error or f"HTTP {response.status}",
                    )
                )
                continue
            audit = audit_page(
                PageSnapshot(
                    url,
                    response.status,
                    response.content_type,
                    response.final_url,
                    response.content,
                ),
                since=since,
                until=now,
                now=now,
            )
            if audit["publicado_em"]:
                published = datetime.fromisoformat(audit["publicado_em"])
                if since <= published <= now:
                    dates[url] = published
                else:
                    dates.pop(url, None)
            eligible = not parameters["lookback_days"] or audit["publicacao_na_janela"] != "nao"
            result["samples"].append(
                dict(
                    eligible=eligible,
                    published_at=audit["publicado_em"],
                    publication_window=audit["publicacao_na_janela"],
                    url=url,
                    final_url=response.final_url,
                    status=response.status,
                    accessed=True,
                    article=audit["artigo_acessivel"] and not audit["audio_dedicado"],
                    category=audit["categoria"],
                    text=audit["texto_util_confirmado"],
                    title=audit["titulo"],
                    excerpt=audit["trecho"][:220],
                    audio=audit["audio_dedicado"],
                    chars=audit["caracteres_texto"],
                    reason=audit["motivos"],
                )
            )
    except TestLimitError as error:
        result["warnings"].append(str(error))
        result["complete"] = False
    result["news_urls"] = len(candidates)
    result["text_samples"] = sum(
        bool(s.get("text") and s.get("eligible", True)) for s in result["samples"]
    )
    if result["xml_valid"] and result["compatible"]:
        result["outcome"] = (
            "text_confirmed"
            if result["text_samples"]
            else "audio_only"
            if result["samples"] and all(s.get("audio") for s in result["samples"])
            else "xml_only"
            if candidates or not result["complete"] or result["root_entries"]
            else "empty"
        )
    result["cadence"] = estimate_cadence(dates)
    result.update(assess_news_search(result))
    result["requests"] = reader.requests
    result["connections_reused"] = getattr(reader, "connections_reused", 0)
    result["cookies_sent"] = getattr(reader, "cookies_sent", 0)
    result["retry_after_seconds"] = getattr(reader, "retry_after_seconds", None)
    result["bright_data_enabled"] = use_bright
    result["access_transport"] = transport
    result["transport_events"] = getattr(reader, "transport_events", [])
    result["bright_data_requests"] = getattr(reader, "bright_requests", 0)
    result["bright_data_events"] = getattr(reader, "bright_events", [])
    result["bright_data_provider_error"] = getattr(reader, "bright_provider_error", False)
    result["bright_data_limit_reached"] = getattr(reader, "bright_limit_reached", False)
    result["access_method"] = (
        "bright_data"
        if result["bright_data_requests"]
        else "curl"
        if transport != "direct"
        else "direct"
    )
    if factory is None and hasattr(reader, "close"):
        reader.close()
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    result["warnings"] = list(dict.fromkeys(result["warnings"]))[:15]
    return result


def assess_news_search(result: dict[str, Any]) -> dict[str, Any]:
    """Qualifica o caminho XML → artigo → texto, sem transformar ausência em erro."""
    samples = result["samples"]
    articles = sum(bool(sample.get("article")) for sample in samples)
    usable = bool(result["xml_valid"] and result["compatible"] and result["text_samples"])
    checks = [
        dict(
            id="access",
            label="Acesso ao RSS/sitemap",
            status="passed" if result["endpoint_accessible"] else "not_confirmed",
            detail=(
                "Endereço acessível."
                if result["endpoint_accessible"]
                else "Não foi possível comprovar o acesso; confira os avisos."
            ),
        ),
        dict(
            id="news",
            label="Obtenção de notícia",
            status="passed" if articles else "not_confirmed" if samples else "not_tested",
            detail=(
                f"{articles} notícia(s) acessível(is) confirmada(s) na amostra."
                if articles
                else "As páginas amostradas não confirmaram uma notícia acessível."
                if samples
                else "Nenhuma página de notícia foi conferida."
            ),
        ),
        dict(
            id="text",
            label="Conteúdo textual da notícia",
            status="passed"
            if result["text_samples"]
            else "not_confirmed"
            if samples
            else "not_tested",
            detail=(
                f"{result['text_samples']} notícia(s) com texto útil extraído."
                if result["text_samples"]
                else "Texto útil de notícia não confirmado na amostra."
                if samples
                else "Sem amostras de notícia para verificar texto."
            ),
        ),
    ]
    return dict(
        assessment_version=1,
        usable_for_news=usable,
        article_samples=articles,
        checks=checks,
    )


class TestJobs:
    """Dois testes simultâneos, sem fila infinita; rascunhos expiram após 15 minutos."""

    __test__ = False

    def __init__(self, store: CatalogStore, factory: ReaderFactory | None = None) -> None:
        """Define repositório e porta de rede, injetável para testes sem internet."""
        self.store, self.factory = store, factory
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="catalog-test")
        self.slots = threading.BoundedSemaphore(2)
        self.jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def start(self, data: dict[str, Any]) -> dict[str, Any]:
        """Valida o rascunho ou revisão cadastrada e inicia trabalho limitado."""
        if data.keys() - {
            "source_id",
            "url",
            "kind",
            "model",
            "endpoint_id",
            "version",
            "bright_data",
            "access_transport",
        }:
            raise CatalogError("Campos de teste desconhecidos.")
        source_id = data.get("source_id")
        if type(source_id) is not int:
            raise CatalogError("Selecione um site antes de testar.")
        source = self.store.source(source_id)
        kind = data.get("kind")
        if kind not in ("feed", "sitemap"):
            raise CatalogError("Selecione o tipo RSS ou sitemap.")
        model = data.get("model", "auto")
        get_mechanism(model, kind)
        settings = self.store.model_settings(model)
        bright = data.get("bright_data", bright_data_available())
        if type(bright) is not bool:
            raise CatalogError("A opção Bright Data deve ser verdadeira ou falsa.")
        if bright and not bright_data_available():
            raise CatalogError("Credenciais Bright Data não configuradas no servidor.")
        test_parameters = dict(settings["parameters"], lookback_days=0)
        if bright:
            test_parameters["timeout_seconds"] = 60
        config = dict(
            url=clean_url(data.get("url")),
            kind=kind,
            model=model,
            test_id=uuid.uuid4().hex,
            parameters=test_parameters,
            bright_data=bright,
            model_version=settings["version"],
            access_transport=validate_transport(data.get("access_transport", "direct")),
        )
        if data.get("endpoint_id") is not None:
            ident = data["endpoint_id"]
            if type(ident) is not int:
                raise CatalogError("Link inválido.")
            endpoint = self.store.endpoint(ident)
            if "access_transport" not in data:
                config["access_transport"] = endpoint.get("access_transport", "direct")
            if (
                endpoint["source_id"] != source_id
                or any(endpoint[k] != config[k] for k in ("url", "kind", "model"))
                or endpoint["version"] != data.get("version")
            ):
                raise CatalogError("Cadastro alterado. Atualize a tela antes de testar.", 409)
        if not self.slots.acquire(blocking=False):
            raise CatalogError("Dois testes estão em execução. Aguarde um deles terminar.", 429)
        ident = config["test_id"]
        with self.lock:
            for key, job in list(self.jobs.items()):
                if job["state"] != "running" and (
                    time.monotonic() - job["created"] > 900 or len(self.jobs) >= 100
                ):
                    del self.jobs[key]
            self.jobs[ident] = dict(
                id=ident, state="running", steps=[], result=None, created=time.monotonic()
            )
        self.pool.submit(self._run, config, source, dict(data))
        return dict(id=ident, state="running")

    def _run(self, config: dict[str, Any], source: dict[str, Any], data: dict[str, Any]) -> None:
        ident = config["test_id"]

        def progress(step: dict[str, object]) -> None:
            with self.lock:
                self.jobs[ident]["steps"] = [*self.jobs[ident]["steps"], step][-40:]

        try:
            result = run_test(config, source, progress, self.factory)
            result["applied_version"] = None
            if data.get("endpoint_id"):
                try:
                    if self.store.source(source["id"])["url"] != source["url"]:
                        raise CatalogError("O endereço do site mudou durante o teste.", 409)
                    result["applied_version"] = self.store.record_test(
                        data["endpoint_id"], data["version"], result
                    )
                except (CatalogError, OSError, sqlite3.Error) as error:
                    result["save_warning"] = (
                        str(error)
                        if isinstance(error, CatalogError)
                        else ("Teste concluído, mas não foi possível salvar no cadastro.")
                    )
            with self.lock:
                self.jobs[ident].update(state="completed", result=result)
        except Exception:
            _LOG.exception("Falha interna no teste de mecanismo")
            # Não transformar erro interno em reprovação do site nem vazar corpo ou credenciais.
            with self.lock:
                self.jobs[ident].update(state="failed", error="Não foi possível concluir o teste.")
        finally:
            self.slots.release()

    def get(self, ident: str) -> dict[str, Any]:
        """Obtém uma cópia do progresso sem bloquear requisições HTTP de cadastro."""
        with self.lock:
            job = self.jobs.get(ident)
            if job is None or time.monotonic() - job["created"] > 900:
                raise CatalogError("Teste expirado ou não encontrado. Execute novamente.", 404)
            return dict(job, steps=list(job["steps"]))

    def result(self, ident: object) -> dict[str, Any]:
        """Fornece apenas resultado calculado pelo servidor, nunca prova enviada pelo navegador."""
        if not isinstance(ident, str):
            raise CatalogError("Teste inválido.")
        job = self.get(ident)
        if job["state"] != "completed":
            raise CatalogError("Aguarde a conclusão do teste.", 409)
        return dict(job["result"])

    def close(self) -> None:
        """Encerra o executor; requisições iniciadas terminam nos limites da amostra."""
        self.pool.shutdown(wait=False, cancel_futures=True)
