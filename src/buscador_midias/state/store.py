"""Estado persistente em Postgres: o que já vimos, o que mudou e como cada rodada se saiu."""

from __future__ import annotations

import os
import re
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from psycopg.types.json import Jsonb

from buscador_midias.criteria import MEETS_CRITERIA_SQL
from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.domain.mapping import MapPage, MapSource, PageStatus
from buscador_midias.domain.roles import is_adverse_role
from buscador_midias.domain.strategy import (
    CandidateOrigin,
    CandidateReport,
    Measure,
    ProbeState,
    Role,
    RoundHealth,
    StrategyKind,
    Verdict,
)

# Pedaço máximo de uma consulta em lote (mantém as consultas e as listas de parâmetros pequenas).
_CHUNK = 500

STATE_ENV_VARS = (
    "DB_MIDIAS_IA_HOST",
    "DB_MIDIAS_IA_PORT",
    "DB_MIDIAS_IA_DATABASE",
    "DB_MIDIAS_IA_USERNAME",
    "DB_MIDIAS_IA_PASSWORD",
)
SCHEMA_ENV = "BUSCADOR_STATE_SCHEMA"

LABEL_GROUPS = ("in_corpus", "no_entity")


class StateConfigError(Exception):
    """Faltam variáveis de conexão do estado (a mensagem nunca traz a senha)."""


class StateSchemaError(Exception):
    """As tabelas do estado não existem no banco."""


def _clean_json(value: Any) -> Any:
    """Remove o caractere NUL de textos dentro de uma estrutura JSON."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_clean_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _clean_json(item) for key, item in value.items()}
    return value


class UrlKind(StrEnum):
    """Como uma URL foi classificada quando apareceu pela primeira vez."""

    NEW = "new"
    BASELINE = "baseline"
    BACKLOG = "backlog"
    KNOWN = "known"
    FILTERED = "filtered"


@dataclass(frozen=True, slots=True)
class EnrichPending:
    """URL nova que ainda espera extração e análise de contexto.

    Attributes:
        url_hash: Hash canônico.
        url: URL.
        title: Título do news sitemap, se houve.
        site_key: Fonte.
        content_status: Desfecho do último download (``None`` se nunca tentamos).
    """

    url_hash: str
    url: str
    title: str | None
    site_key: str
    content_status: str | None


@dataclass(frozen=True, slots=True)
class FindingUrl:
    """URL (achado adverso ou matéria com entidade) que espera a captura da evidência.

    Attributes:
        url_hash: Hash canônico.
        url: URL.
        title: Título do news sitemap, se houve.
        site_key: Fonte.
        entity_names: Entidades que a evidência precisa mencionar (ao menos uma): as de papel
            adverso no achado adverso; qualquer uma nas demais matérias.
        adverse: Se a análise apontou fato adverso. Só estes justificam pagar pelo Bright Data.
    """

    url_hash: str
    url: str
    title: str | None
    site_key: str
    entity_names: tuple[str, ...]
    adverse: bool = False


@dataclass(frozen=True, slots=True)
class ContentRecord:
    """O que o download e a extração produziram para uma URL.

    Attributes:
        url_hash: Hash canônico.
        status: ``ok``, ``blocked``, ``not_found``, ``robots_disallowed`` ou ``error``.
        http_status: Status HTTP, se houve resposta.
        final_url: URL final depois de redirecionamentos.
        title: Título extraído da página.
        description: Descrição (``og:description``/meta).
        lede: Início do texto do artigo.
        text_chars: Tamanho do texto completo extraído.
        published_at: Data de publicação extraída do HTML.
        language: Idioma extraído do HTML.
        error: Explicação curta da falha.
    """

    url_hash: str
    status: str
    http_status: int | None
    final_url: str | None
    title: str | None
    description: str | None
    lede: str | None
    text_chars: int
    published_at: datetime | None
    language: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class AnalysisRecord:
    """Resultado da análise de contexto de uma URL.

    Attributes:
        url_hash: Hash canônico.
        finding: Se a matéria relata fato adverso sobre pessoa ou empresa nomeada.
        themes: Temas apontados.
        entities: Entidades citadas (``name``, ``type``, ``role``).
        adverse_fact: Fato adverso, em uma frase.
        summary: Resumo curto.
        confidence: Confiança do modelo (0 a 1).
        basis: ``text`` (leu a matéria) ou ``headline`` (só a manchete, confiança menor).
        model: Modelo que analisou (preenchido na leitura).
        prompt_version: Versão do prompt (preenchida na leitura).
    """

    url_hash: str
    finding: bool
    themes: tuple[str, ...]
    entities: tuple[dict[str, str], ...]
    adverse_fact: str
    summary: str
    confidence: float
    basis: str
    model: str = ""
    prompt_version: str = ""


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """Estado da captura da evidência de uma URL.

    Attributes:
        url_hash: Hash canônico.
        status: ``captured`` ou ``failed``.
        provider: ``direct`` (HTML reaproveitado) ou ``brightdata``.
        path: Caminho relativo do HTML no armazenamento.
        sha256: Hash do conteúdo gravado (integridade).
        size_bytes: Tamanho gravado.
        content_type: Tipo de conteúdo recebido.
        http_status: Status HTTP da fonte do conteúdo.
        final_url: URL final da fonte do conteúdo.
        rejected_reasons: Por que o HTML do download direto não foi reaproveitado (se não foi).
        error: Falha da captura, quando ``failed``.
        attempts: Tentativas feitas (preenchido na leitura).
    """

    url_hash: str
    status: str
    provider: str | None
    path: str | None
    sha256: str | None
    size_bytes: int
    content_type: str | None
    http_status: int | None
    final_url: str | None
    rejected_reasons: tuple[str, ...]
    error: str | None
    attempts: int = 0


@dataclass(frozen=True, slots=True)
class EnrichTotals:
    """Totais e consumo de uma execução do enriquecimento.

    Attributes:
        urls: URLs processadas.
        fetched_ok: Downloads bem-sucedidos.
        fetched_blocked: Downloads bloqueados (401/403/429 ou robots).
        fetched_other: Demais falhas de download (404, rede...).
        analyzed: URLs com análise gravada.
        findings: Análises com achado.
        evidence_reused: Evidências aproveitadas do download direto.
        evidence_brightdata: Evidências capturadas pelo Bright Data.
        evidence_failed: Capturas que falharam.
        bright_data_requests: Chamadas pagas ao Bright Data.
        prompt_tokens: Tokens de entrada do LLM.
        completion_tokens: Tokens de saída do LLM.
        cost_usd: Custo do LLM informado pelo provedor.
        retries: Tentativas repetidas ao LLM.
        errors: Lotes sem resposta do LLM.
    """

    urls: int = 0
    fetched_ok: int = 0
    fetched_blocked: int = 0
    fetched_other: int = 0
    analyzed: int = 0
    findings: int = 0
    evidence_reused: int = 0
    evidence_brightdata: int = 0
    evidence_failed: int = 0
    bright_data_requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None
    retries: int = 0
    errors: int = 0


@dataclass(frozen=True, slots=True)
class EnrichRun:
    """Uma execução do enriquecimento (extração, análise e evidência).

    Attributes:
        id: Identificador sequencial.
        started_at: Início (UTC).
        model: Modelo da análise.
        prompt_version: Versão do prompt.
        evidence_mode: ``auto`` (tenta reaproveitar) ou ``brightdata``.
        totals: Totais e consumo.
        finished_at: Fim (UTC), ou ``None`` enquanto não terminou.
    """

    id: int
    started_at: datetime
    model: str
    prompt_version: str
    evidence_mode: str
    totals: EnrichTotals
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CorpusCandidate:
    """Um achado pronto para ser gravado no corpus de Mídias IA.

    Attributes:
        url_hash: Hash canônico.
        url: URL da matéria.
        site_key: Fonte (``Site.key``).
        title: Título (da página ou do sitemap).
        description: Descrição da página.
        lede: Início do texto extraído.
        published_at: Data de publicação extraída do HTML.
        language: Idioma extraído do HTML.
        themes: Temas apontados.
        entities: Entidades (``name``, ``type``, ``role``).
        adverse_fact: Fato adverso em uma frase.
        summary: Resumo curto.
        confidence: Confiança da análise.
        basis: ``text`` ou ``headline``.
        model: Modelo que analisou.
        evidence_status: ``captured``/``failed``, ou ``None`` sem tentativa.
        evidence_path: Caminho relativo da evidência.
        evidence_attempts: Tentativas de captura.
        evidence_provider: ``direct`` ou ``brightdata``.
        adverse: Se a análise apontou fato adverso (senão é uma matéria só com entidade).
    """

    url_hash: str
    url: str
    site_key: str
    title: str | None
    description: str | None
    lede: str | None
    published_at: datetime | None
    language: str | None
    themes: tuple[str, ...]
    entities: tuple[dict[str, str], ...]
    adverse_fact: str
    summary: str
    confidence: float
    basis: str
    model: str
    evidence_status: str | None
    evidence_path: str | None
    evidence_attempts: int
    evidence_provider: str | None
    adverse: bool


@dataclass(frozen=True, slots=True)
class CorpusTotals:
    """Totais de uma execução da gravação no corpus.

    Attributes:
        candidates: Achados que esperavam gravação (novos ou com a evidência mudada).
        created: Artigos inseridos no corpus.
        existing: Artigos que já estavam no corpus (de qualquer origem) e foram só reconhecidos.
        evidence_updated: Artigos já existentes cuja evidência passou a ``captured``.
        entities: Entidades gravadas.
        keywords: Palavras-chave (temas) gravadas.
        failed: Achados que falharam ao gravar (voltam na próxima execução).
        rewritten: Gravações que sumiram do corpus e foram refeitas.
    """

    candidates: int = 0
    created: int = 0
    existing: int = 0
    evidence_updated: int = 0
    entities: int = 0
    keywords: int = 0
    failed: int = 0
    rewritten: int = 0


@dataclass(frozen=True, slots=True)
class CorpusRun:
    """Uma execução da gravação no corpus.

    Attributes:
        id: Identificador sequencial.
        started_at: Início (UTC).
        totals: Totais.
        finished_at: Fim (UTC), ou ``None`` enquanto não terminou.
    """

    id: int
    started_at: datetime
    totals: CorpusTotals
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RecentFinding:
    """Um achado recente da análise de contexto, com o estado da sua evidência.

    Attributes:
        analyzed_at: Quando foi analisado (UTC).
        url: URL da matéria.
        site_key: Fonte.
        headline: Título extraído da página (ou o do sitemap).
        themes: Temas apontados.
        adverse_entities: Entidades com papel adverso (acusado, investigado, condenado...).
        adverse_fact: Fato adverso em uma frase.
        summary: Resumo curto.
        confidence: Confiança da análise.
        basis: ``text`` ou ``headline``.
        evidence_status: ``captured``/``failed``, ou ``None`` se ainda não houve tentativa.
        evidence_provider: ``direct`` ou ``brightdata`` (``None`` sem evidência).
        evidence_path: Caminho relativo da evidência gravada.
    """

    analyzed_at: datetime
    url: str
    site_key: str
    headline: str | None
    themes: tuple[str, ...]
    adverse_entities: tuple[str, ...]
    adverse_fact: str
    summary: str
    confidence: float
    basis: str
    evidence_status: str | None
    evidence_provider: str | None
    evidence_path: str | None


@dataclass(frozen=True, slots=True)
class LabelCandidate:
    """Uma URL candidata à rotulagem humana, com o que o modelo decidiu (que fica oculto no CSV).

    Attributes:
        url_hash: Hash canônico.
        group: ``in_corpus`` (a análise achou entidade identificável: vai ao corpus) ou
            ``no_entity`` (lida como matéria e sem entidade).
        url: URL.
        site_key: Fonte.
        headline: Título (da página ou do sitemap).
        excerpt: Início do texto da matéria (``None`` se não foi baixado).
        themes: Temas apontados pela análise.
        entity_names: Entidades nomeadas apontadas pela análise (qualquer papel).
        adverse_fact: Fato adverso apontado pela análise (vazio se não houve).
        evidence_provider: Origem da evidência (``direct``/``brightdata``), se houve.
    """

    url_hash: str
    group: str
    url: str
    site_key: str
    headline: str | None
    excerpt: str | None
    themes: tuple[str, ...]
    entity_names: tuple[str, ...]
    adverse_fact: str
    evidence_provider: str | None


@dataclass(frozen=True, slots=True)
class RecentUrl:
    """Uma URL nova recém-descoberta, com a situação da análise de contexto.

    Attributes:
        first_seen_at: Quando a vimos pela primeira vez (UTC).
        url: URL.
        site_key: Fonte.
        title: Título do news sitemap, se houve.
        status: ``pending`` (ainda sem análise), ``in_corpus`` (tem entidade identificável) ou
            ``no_entity`` (lida e sem entidade: não entra).
        themes: Temas apontados pela análise.
    """

    first_seen_at: datetime
    url: str
    site_key: str
    title: str | None
    status: str
    themes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Uma execução da descoberta.

    Attributes:
        id: Identificador sequencial.
        started_at: Início (UTC).
        finished_at: Fim (UTC), ou ``None`` enquanto não terminou.
        bright_data_requests: Chamadas pagas ao Bright Data feitas na rodada.
        bright_data_hosts: Hosts que dependeram do Bright Data na rodada.
    """

    id: int
    started_at: datetime
    finished_at: datetime | None
    bright_data_requests: int = 0
    bright_data_hosts: int = 0


@dataclass(frozen=True, slots=True)
class SiteRunRecord:
    """O que a descoberta de um site produziu em uma rodada.

    Attributes:
        site_key: Fonte (``Site.key``).
        status: Desfecho (valor de ``ProbeStatus``).
        new_urls: URLs novas (candidatas à análise).
        baseline_urls: URLs registradas como histórico na linha de base do site.
        backlog_urls: URLs inéditas para nós, mas antigas demais para contar como novas.
        seen_urls: URLs do site encontradas nos sitemaps lidos (novas ou não).
        sitemaps_fetched: Sitemaps requisitados.
        sitemaps_skipped: Sitemaps filhos dispensados por ``lastmod`` inalterado.
        detail: Explicação curta de falha, quando houve.
        known_urls: URLs inéditas para nós, mas que o corpus de Mídias IA já tinha.
        sitemaps_failed: Sitemaps que falharam em um site ``ok`` (leitura parcial); o motivo da
            primeira falha fica em ``detail``.
        filtered_urls: URLs inéditas e frescas descartadas pela forma da URL (L0).
        source: Origem (``sitemap``, ``feed``, ``sitemap_bd`` ou ``combined``);
            vazio em falha.
    """

    site_key: str
    status: str
    new_urls: int
    baseline_urls: int
    backlog_urls: int
    seen_urls: int
    sitemaps_fetched: int
    sitemaps_skipped: int
    detail: str
    known_urls: int = 0
    sitemaps_failed: int = 0
    filtered_urls: int = 0
    source: str = ""


def _utc(value: datetime | None) -> datetime | None:
    """Datas vão para o banco em UTC (``timestamptz``)."""
    return value.astimezone(UTC) if value is not None else None


def _text(value: str | None) -> str | None:
    """Texto de terceiros sem o caractere NUL, que o Postgres não aceita em campos de texto."""
    return value.replace("\x00", "") if value is not None else None


_STRATEGY_SELECT = (
    "SELECT site_key, url, origin, verdict, kind, role, score, items, dated_items,"
    " recent_items, titled_items, newest_at, size_bytes, requests, detail, coverage,"
    " union_recent, cover_share, section_count, top_sections, probed_at"
    " FROM midias_crawler_source_strategies"
)


def _strategy_from_row(row: Sequence[Any]) -> CandidateReport:
    """Monta um candidato a partir de uma linha de ``_STRATEGY_SELECT`` (sem a fonte)."""
    (url, origin, verdict, kind, role, value, items, dated, recent, titled, newest, size) = row[:12]
    requests, detail, coverage, union_recent, cover_share, section_count, top = row[12:19]
    strategy_kind = StrategyKind(kind) if kind else None
    measure = (
        Measure(
            strategy_kind,
            items,
            dated,
            recent,
            titled,
            newest,
            size,
            sections=section_count,
            top_sections=tuple((name, count) for name, count in top),
        )
        if strategy_kind
        else None
    )
    return CandidateReport(
        url=url,
        origin=CandidateOrigin(origin),
        verdict=Verdict(verdict),
        kind=strategy_kind,
        measure=measure,
        score=value,
        detail=detail,
        requests=requests,
        role=Role(role) if role else None,
        coverage=coverage,
        union_recent=union_recent,
        cover_share=cover_share,
    )


def _chunks[T](items: Sequence[T], size: int = _CHUNK) -> Iterator[Sequence[T]]:
    """Pedaços de ``size`` itens, para não montar consultas gigantes."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


class StateStore:
    """Estado do buscador no Postgres (tabelas ``midias_crawler_*``), seguro para várias threads.

    A conexão vem das variáveis ``DB_MIDIAS_IA_*`` (o mesmo banco do corpus). Uma única conexão em
    autocommit, com uma trava que serializa o uso; gravações de mais de uma linha rodam numa
    transação. As tabelas são criadas pelos arquivos ``sql/postgres/00*.sql``
    (ou pela migration equivalente no powerdd-backend): este objeto nunca cria nem altera esquema.
    """

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        read_only: bool = False,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        """Conecta e confere que as tabelas existem.

        Args:
            env: Variáveis de ambiente (padrão: ``os.environ``). ``BUSCADOR_STATE_SCHEMA``
                opcional escolhe o schema (os testes usam um por teste).
            read_only: Sessão somente leitura (``default_transaction_read_only=on``): o painel e a
                rotulagem nunca gravam, mesmo que um bug tente.
            connect: Função de conexão (padrão: ``psycopg.connect``); injetável para testes.

        Raises:
            StateConfigError: Se faltar variável de conexão (a mensagem nunca traz a senha).
            StateSchemaError: Se as tabelas não existirem no banco.
        """
        values = os.environ if env is None else env
        missing = [name for name in STATE_ENV_VARS if not values.get(name)]
        if missing:
            raise StateConfigError(f"variáveis ausentes para o estado: {', '.join(missing)}")
        options = ["-c timezone=UTC"]
        schema = values.get(SCHEMA_ENV)
        if schema:
            if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema):
                raise StateConfigError(f"{SCHEMA_ENV} inválido")
            options.append(f"-c search_path={schema}")
        if read_only:
            options.append("-c default_transaction_read_only=on")
        if connect is None:
            import psycopg

            connect = psycopg.connect
        self._lock = threading.RLock()
        self._mapping_classification_ready = False
        self._conn = connect(
            host=values["DB_MIDIAS_IA_HOST"],
            port=values["DB_MIDIAS_IA_PORT"],
            dbname=values["DB_MIDIAS_IA_DATABASE"],
            user=values["DB_MIDIAS_IA_USERNAME"],
            password=values["DB_MIDIAS_IA_PASSWORD"],
            options=" ".join(options),
            autocommit=True,
            connect_timeout=10,
        )
        missing_tables = self._missing_tables()
        if missing_tables:
            self._conn.close()
            raise StateSchemaError(
                f"o banco não tem as tabelas do estado ({', '.join(missing_tables)}): "
                "rode os arquivos de sql/postgres (001_create_midias_crawler_tables.sql e os "
                "seguintes)"
            )

    def _missing_tables(self) -> list[str]:
        """Tabelas esperadas que não existem no schema em uso."""
        names = (
            "midias_crawler_runs",
            "midias_crawler_urls",
            "midias_crawler_analyses",
            "midias_crawler_corpus_writes",
            "midias_crawler_source_strategies",
            "midias_crawler_source_probe_state",
        )
        missing = []
        for name in names:
            row = self._conn.execute("SELECT to_regclass(%s)", (name,)).fetchone()
            if row is None or row[0] is None:
                missing.append(name)
        return missing

    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """Executa uma consulta e devolve todas as linhas."""
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: Sequence[Any] = ()) -> tuple[Any, ...] | None:
        """Executa uma consulta e devolve a primeira linha (ou ``None``)."""
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _run(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Executa uma gravação e devolve quantas linhas ela afetou."""
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Agrupa gravações numa transação única (tudo ou nada); mantém a trava durante o bloco."""
        with self._lock, self._conn.transaction():
            yield

    def is_empty(self) -> bool:
        """Se o estado ainda não tem nenhuma rodada nem URL (destino válido para a migração)."""
        row = self._one(
            "SELECT (SELECT COUNT(*) FROM midias_crawler_runs)"
            " + (SELECT COUNT(*) FROM midias_crawler_urls)"
        )
        return row is not None and int(row[0]) == 0

    def bulk_load(
        self,
        table: str,
        columns: Sequence[str],
        rows: Iterable[Sequence[Any]],
        *,
        keep_ids: bool = False,
    ) -> int:
        """Carrega muitas linhas de uma vez (usado pela migração do estado antigo).

        Args:
            table: Tabela ``midias_crawler_*`` de destino.
            columns: Colunas, na ordem dos valores de cada linha.
            rows: Linhas já convertidas para os tipos do Postgres.
            keep_ids: Grava também a coluna ``id`` (identidade), em vez de deixar o banco gerá-la.

        Returns:
            Quantas linhas foram gravadas.
        """
        if not re.fullmatch(r"midias_crawler_[a-z_]+", table):
            raise ValueError(f"tabela inválida: {table!r}")
        names = ", ".join(columns)
        count = 0
        with self.transaction():
            if keep_ids:
                marks = ", ".join(["%s"] * len(columns))
                sql = f"INSERT INTO {table} ({names}) OVERRIDING SYSTEM VALUE VALUES ({marks})"
                for row in rows:
                    self._conn.execute(sql, row)
                    count += 1
            else:
                with (
                    self._conn.cursor() as cursor,
                    cursor.copy(f"COPY {table} ({names}) FROM STDIN") as copy,
                ):
                    for row in rows:
                        copy.write_row(row)
                        count += 1
        return count

    def restart_identities(self) -> None:
        """Põe os contadores automáticos de id logo depois do maior id existente."""
        for table in (
            "midias_crawler_runs",
            "midias_crawler_enrich_runs",
            "midias_crawler_corpus_runs",
            "midias_crawler_urls",
        ):
            self._run(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'),"
                f" COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
            )

    def close(self) -> None:
        """Fecha a conexão."""
        with self._lock:
            self._conn.close()

    # --- rodadas ---

    def start_run(self, started_at: datetime) -> int:
        """Registra o início de uma rodada e devolve o seu id."""
        row = self._one(
            "INSERT INTO midias_crawler_runs (started_at) VALUES (%s) RETURNING id",
            (_utc(started_at),),
        )
        return int(row[0]) if row else 0

    def finish_run(self, run_id: int, finished_at: datetime) -> None:
        """Registra o fim de uma rodada."""
        self._run(
            "UPDATE midias_crawler_runs SET finished_at = %s WHERE id = %s",
            (_utc(finished_at), run_id),
        )

    def record_run_usage(
        self, run_id: int, *, bright_data_requests: int, bright_data_hosts: int
    ) -> None:
        """Grava o consumo pago (Bright Data) de uma rodada."""
        self._run(
            "UPDATE midias_crawler_runs SET bright_data_requests = %s, bright_data_hosts = %s"
            " WHERE id = %s",
            (bright_data_requests, bright_data_hosts, run_id),
        )

    def list_runs(self) -> list[RunRecord]:
        """Todas as rodadas, da mais antiga para a mais nova."""
        rows = self._all(
            "SELECT id, started_at, finished_at, bright_data_requests, bright_data_hosts"
            " FROM midias_crawler_runs ORDER BY id"
        )
        return [
            RunRecord(
                id=row[0],
                started_at=row[1],
                finished_at=row[2],
                bright_data_requests=row[3],
                bright_data_hosts=row[4],
            )
            for row in rows
        ]

    # --- URLs vistas ---

    def seen_hashes(self, hashes: Iterable[str]) -> set[str]:
        """Dos hashes informados, quais já estão registrados."""
        found: set[str] = set()
        for chunk in _chunks(list(hashes)):
            rows = self._all(
                "SELECT url_hash FROM midias_crawler_urls WHERE url_hash = ANY(%s)", (list(chunk),)
            )
            found.update(row[0] for row in rows)
        return found

    def record_urls(
        self,
        urls: Sequence[DiscoveredUrl],
        *,
        run_id: int,
        now: datetime,
        kind: UrlKind,
        reasons: Mapping[str, str] | None = None,
    ) -> int:
        """Registra URLs; as já conhecidas são ignoradas (a primeira classificação vence).

        Args:
            urls: URLs a registrar.
            run_id: Rodada em curso.
            now: Instante gravado como "primeira vez visto".
            kind: Classificação aplicada a todas.
            reasons: Motivo do descarte por ``url_hash`` (para ``UrlKind.FILTERED``).

        Returns:
            Quantas URLs eram de fato inéditas.
        """
        added: Counter[str] = Counter()
        with self._lock, self._conn.transaction():
            for chunk in _chunks(list(urls), 200):
                marks = ",".join(["(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"] * len(chunk))
                params: list[Any] = []
                for u in chunk:
                    params += [
                        u.url_hash,
                        _text(u.url),
                        u.site_key,
                        _text(u.source_sitemap),
                        kind.value,
                        run_id,
                        _utc(now),
                        _utc(u.lastmod),
                        _text(u.title),
                        _utc(u.publication_date),
                        (reasons or {}).get(u.url_hash),
                    ]
                rows = self._conn.execute(
                    "INSERT INTO midias_crawler_urls (url_hash, url, site_key, source_sitemap,"
                    " kind, first_seen_run, first_seen_at, lastmod, title, publication_date,"
                    f" filter_reason) VALUES {marks} ON CONFLICT (url_hash) DO NOTHING"
                    " RETURNING site_key",
                    params,
                ).fetchall()
                added.update(row[0] for row in rows)
            for site, count in added.items():
                self._conn.execute(
                    "INSERT INTO midias_crawler_site_kind_counts (site_key, kind, n)"
                    " VALUES (%s, %s, %s) ON CONFLICT (site_key, kind)"
                    " DO UPDATE SET n = midias_crawler_site_kind_counts.n + EXCLUDED.n",
                    (site, kind.value, count),
                )
        return sum(added.values())

    def count_urls(self, *, kind: UrlKind | None = None) -> int:
        """Total de URLs registradas, opcionalmente de um só tipo."""
        if kind is None:
            row = self._one("SELECT COUNT(*) FROM midias_crawler_urls")
        else:
            row = self._one(
                "SELECT COUNT(*) FROM midias_crawler_urls WHERE kind = %s", (kind.value,)
            )
        return int(row[0]) if row else 0

    def count_urls_by_kind(self) -> dict[str, int]:
        """Quantas URLs há de cada tipo (``new``, ``baseline``, ``backlog``...).

        Lê os contadores mantidos em ``record_urls``: varrer ``midias_crawler_urls`` a cada consulta
        custaria segundos com milhões de linhas.
        """
        rows = self._all("SELECT kind, SUM(n) FROM midias_crawler_site_kind_counts GROUP BY kind")
        return {kind: int(count) for kind, count in rows}

    def new_urls_per_hour(self, *, since: datetime) -> dict[str, int]:
        """URLs ``new`` descobertas por hora (``AAAA-MM-DDTHH``, UTC) desde ``since``."""
        rows = self._all(
            "SELECT to_char(first_seen_at, 'YYYY-MM-DD\"T\"HH24') AS hour, COUNT(*)"
            " FROM midias_crawler_urls WHERE kind = %s AND first_seen_at >= %s"
            " GROUP BY 1 ORDER BY 1",
            (UrlKind.NEW.value, _utc(since)),
        )
        return {hour: int(count) for hour, count in rows}

    def analyzed_per_hour(self, *, since: datetime) -> dict[str, dict[str, int]]:
        """Análises por hora (UTC) desde ``since``: as que atendem ao critério e as que não."""
        rows = self._all(
            "SELECT to_char(a.analyzed_at, 'YYYY-MM-DD\"T\"HH24') AS hour,"
            f" CASE WHEN {MEETS_CRITERIA_SQL} THEN 'meets' ELSE 'not_meets' END, COUNT(*)"
            " FROM midias_crawler_analyses a WHERE a.analyzed_at >= %s GROUP BY 1, 2 ORDER BY 1",
            (_utc(since),),
        )
        hours: dict[str, dict[str, int]] = {}
        for hour, verdict, count in rows:
            hours.setdefault(hour, {})[verdict] = int(count)
        return hours

    def recent_new_urls(self, limit: int) -> list[RecentUrl]:
        """Últimas URLs ``new`` descobertas, da mais recente para a mais antiga."""
        rows = self._all(
            "SELECT u.first_seen_at, u.url, u.site_key, u.title, a.themes,"
            " CASE WHEN a.url_hash IS NULL THEN 'pending'"
            f" WHEN {MEETS_CRITERIA_SQL} THEN 'in_corpus' ELSE 'no_entity' END"
            " FROM midias_crawler_urls u"
            " LEFT JOIN midias_crawler_analyses a ON a.url_hash = u.url_hash"
            " WHERE u.kind = %s ORDER BY u.first_seen_at DESC, u.id DESC LIMIT %s",
            (UrlKind.NEW.value, limit),
        )
        return [
            RecentUrl(
                first_seen_at=row[0],
                url=row[1],
                site_key=row[2],
                title=row[3],
                status=row[5],
                themes=tuple(row[4] or ()),
            )
            for row in rows
        ]

    def url_counts_by_site(self) -> dict[str, dict[str, int]]:
        """Contagem de URLs por fonte e tipo (dos contadores mantidos em ``record_urls``)."""
        rows = self._all("SELECT site_key, kind, n FROM midias_crawler_site_kind_counts")
        counts: dict[str, dict[str, int]] = {}
        for site, kind, count in rows:
            counts.setdefault(site, {})[kind] = int(count)
        return counts

    def last_new_by_site(self) -> dict[str, datetime]:
        """Quando cada fonte trouxe a sua URL ``new`` mais recente."""
        rows = self._all(
            "SELECT site_key, MAX(first_seen_at) FROM midias_crawler_urls WHERE kind = %s"
            " GROUP BY site_key",
            (UrlKind.NEW.value,),
        )
        return {site: stamp for site, stamp in rows}

    def sites_done(self, run_id: int) -> int:
        """Quantos sites já têm resultado gravado na rodada (progresso de uma rodada em curso)."""
        row = self._one(
            "SELECT COUNT(*) FROM midias_crawler_site_runs WHERE run_id = %s", (run_id,)
        )
        return int(row[0]) if row else 0

    def filtered_by_reason(self) -> dict[str, int]:
        """Quantas URLs a camada L0 descartou, por motivo."""
        rows = self._all(
            "SELECT filter_reason, COUNT(*) FROM midias_crawler_urls"
            " WHERE kind = %s AND filter_reason IS NOT NULL GROUP BY filter_reason",
            (UrlKind.FILTERED.value,),
        )
        return {reason: int(count) for reason, count in rows}

    def new_url_latencies_hours(self) -> list[float]:
        """Horas entre a publicação (news sitemap) e a primeira vez que vimos cada URL nova."""
        rows = self._all(
            "SELECT EXTRACT(EPOCH FROM (first_seen_at - publication_date)) / 3600"
            " FROM midias_crawler_urls WHERE kind = %s AND publication_date IS NOT NULL",
            (UrlKind.NEW.value,),
        )
        return [float(row[0]) for row in rows]

    # --- linha de base por site ---

    def site_has_baseline(self, site_key: str) -> bool:
        """Se a linha de base do site já foi feita."""
        row = self._one(
            "SELECT 1 FROM midias_crawler_site_baselines WHERE site_key = %s", (site_key,)
        )
        return row is not None

    def mark_baseline(self, site_key: str, run_id: int) -> None:
        """Marca a linha de base do site como feita (idempotente: a primeira rodada vence)."""
        self._run(
            "INSERT INTO midias_crawler_site_baselines (site_key, run_id) VALUES (%s, %s)"
            " ON CONFLICT (site_key) DO NOTHING",
            (site_key, run_id),
        )

    # --- sitemaps ---

    def sitemap_lastmod(self, sitemap_url: str) -> datetime | None:
        """Último ``lastmod`` declarado que vimos para este sitemap, se houver."""
        row = self._one(
            "SELECT lastmod FROM midias_crawler_sitemaps WHERE sitemap_url = %s", (sitemap_url,)
        )
        return row[0] if row else None

    def record_sitemap(
        self, sitemap_url: str, site_key: str, lastmod: datetime | None, fetched_at: datetime
    ) -> None:
        """Registra que o sitemap foi lido, com o ``lastmod`` que o índice declarava."""
        self._run(
            "INSERT INTO midias_crawler_sitemaps (sitemap_url, site_key, lastmod, fetched_at)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT (sitemap_url) DO UPDATE SET"
            " lastmod = EXCLUDED.lastmod, fetched_at = EXCLUDED.fetched_at",
            (_text(sitemap_url), site_key, _utc(lastmod), _utc(fetched_at)),
        )

    # --- resultado por site e rodada ---

    def record_site_run(self, run_id: int, record: SiteRunRecord) -> None:
        """Grava (ou substitui) o resultado de um site em uma rodada."""
        self._run(
            "INSERT INTO midias_crawler_site_runs (run_id, site_key, status, new_urls,"
            " baseline_urls, backlog_urls, seen_urls, sitemaps_fetched, sitemaps_skipped,"
            " detail, known_urls, sitemaps_failed, filtered_urls, source)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (run_id, site_key) DO UPDATE SET status = EXCLUDED.status,"
            " new_urls = EXCLUDED.new_urls, baseline_urls = EXCLUDED.baseline_urls,"
            " backlog_urls = EXCLUDED.backlog_urls, seen_urls = EXCLUDED.seen_urls,"
            " sitemaps_fetched = EXCLUDED.sitemaps_fetched,"
            " sitemaps_skipped = EXCLUDED.sitemaps_skipped, detail = EXCLUDED.detail,"
            " known_urls = EXCLUDED.known_urls, sitemaps_failed = EXCLUDED.sitemaps_failed,"
            " filtered_urls = EXCLUDED.filtered_urls, source = EXCLUDED.source",
            (
                run_id,
                record.site_key,
                record.status,
                record.new_urls,
                record.baseline_urls,
                record.backlog_urls,
                record.seen_urls,
                record.sitemaps_fetched,
                record.sitemaps_skipped,
                _text(record.detail),
                record.known_urls,
                record.sitemaps_failed,
                record.filtered_urls,
                record.source,
            ),
        )

    def site_runs(self, run_id: int) -> list[SiteRunRecord]:
        """Resultados por site de uma rodada, em ordem alfabética."""
        rows = self._all(
            "SELECT site_key, status, new_urls, baseline_urls, backlog_urls, seen_urls,"
            " sitemaps_fetched, sitemaps_skipped, detail, known_urls, sitemaps_failed,"
            " filtered_urls, source FROM midias_crawler_site_runs"
            " WHERE run_id = %s ORDER BY site_key",
            (run_id,),
        )
        return [SiteRunRecord(*row) for row in rows]

    def all_site_runs(self) -> list[tuple[int, SiteRunRecord]]:
        """Resultados por site de todas as rodadas, por rodada e depois por site."""
        rows = self._all(
            "SELECT run_id, site_key, status, new_urls, baseline_urls, backlog_urls,"
            " seen_urls, sitemaps_fetched, sitemaps_skipped, detail, known_urls,"
            " sitemaps_failed, filtered_urls, source FROM midias_crawler_site_runs"
            " ORDER BY run_id, site_key"
        )
        return [(row[0], SiteRunRecord(*row[1:])) for row in rows]

    # --- enriquecimento: conteúdo, análise de contexto e evidência ---

    def start_enrich_run(
        self, started_at: datetime, *, model: str, prompt_version: str, evidence_mode: str
    ) -> int:
        """Registra o início de uma execução do enriquecimento e devolve o seu id."""
        row = self._one(
            "INSERT INTO midias_crawler_enrich_runs (started_at, model, prompt_version,"
            " evidence_mode) VALUES (%s, %s, %s, %s) RETURNING id",
            (_utc(started_at), model, prompt_version, evidence_mode),
        )
        return int(row[0]) if row else 0

    def finish_enrich_run(
        self, enrich_run_id: int, finished_at: datetime, totals: EnrichTotals
    ) -> None:
        """Grava o fim de uma execução do enriquecimento, com totais e consumo."""
        self._run(
            "UPDATE midias_crawler_enrich_runs SET finished_at = %s, urls = %s, fetched_ok = %s,"
            " fetched_blocked = %s, fetched_other = %s, analyzed = %s, findings = %s,"
            " evidence_reused = %s, evidence_brightdata = %s, evidence_failed = %s,"
            " bright_data_requests = %s, prompt_tokens = %s, completion_tokens = %s,"
            " cost_usd = %s, retries = %s, errors = %s WHERE id = %s",
            (
                _utc(finished_at),
                totals.urls,
                totals.fetched_ok,
                totals.fetched_blocked,
                totals.fetched_other,
                totals.analyzed,
                totals.findings,
                totals.evidence_reused,
                totals.evidence_brightdata,
                totals.evidence_failed,
                totals.bright_data_requests,
                totals.prompt_tokens,
                totals.completion_tokens,
                totals.cost_usd,
                totals.retries,
                totals.errors,
                enrich_run_id,
            ),
        )

    def enrich_runs(self) -> list[EnrichRun]:
        """Todas as execuções do enriquecimento, da mais antiga para a mais nova."""
        rows = self._all(
            "SELECT id, started_at, finished_at, model, prompt_version, evidence_mode, urls,"
            " fetched_ok, fetched_blocked, fetched_other, analyzed, findings, evidence_reused,"
            " evidence_brightdata, evidence_failed, bright_data_requests, prompt_tokens,"
            " completion_tokens, cost_usd, retries, errors FROM midias_crawler_enrich_runs"
            " ORDER BY id"
        )
        return [
            EnrichRun(
                id=row[0],
                started_at=row[1],
                finished_at=row[2],
                model=row[3],
                prompt_version=row[4],
                evidence_mode=row[5],
                totals=EnrichTotals(*row[6:]),
            )
            for row in rows
        ]

    def pending_enrichment(self, limit: int | None = None) -> list[EnrichPending]:
        """URLs ``new`` que ainda não têm análise de contexto.

        A ordem intercala as fontes (a 1ª de cada uma, depois a 2ª de cada uma...) e, dentro de
        cada fonte, segue a ordem em que foram vistas. Um bloco só com URLs de um site ficaria
        parado no intervalo mínimo entre requisições ao mesmo host.
        """
        sql = (
            "SELECT url_hash, url, title, site_key, status FROM ("
            " SELECT u.url_hash, u.url, u.title, u.site_key, c.status, u.id AS pos,"
            " ROW_NUMBER() OVER (PARTITION BY u.site_key ORDER BY u.id) AS turn"
            " FROM midias_crawler_urls u"
            " LEFT JOIN midias_crawler_contents c ON c.url_hash = u.url_hash"
            " LEFT JOIN midias_crawler_analyses a ON a.url_hash = u.url_hash"
            " WHERE u.kind = %s AND a.url_hash IS NULL"
            ") pending ORDER BY turn, pos"
        )
        params: list[Any] = [UrlKind.NEW.value]
        if limit is not None:
            sql += " LIMIT %s"
            params.append(limit)
        return [EnrichPending(*row) for row in self._all(sql, params)]

    def fetch_attempts(self, url_hash: str) -> int:
        """Quantas vezes tentamos baixar a URL."""
        row = self._one(
            "SELECT attempts FROM midias_crawler_contents WHERE url_hash = %s", (url_hash,)
        )
        return int(row[0]) if row else 0

    def record_content(self, record: ContentRecord, *, now: datetime) -> None:
        """Grava (ou atualiza) o resultado do download e da extração; conta a tentativa."""
        self._run(
            "INSERT INTO midias_crawler_contents (url_hash, status, http_status, final_url,"
            " title, description, lede, text_chars, published_at, language, error, attempts,"
            " fetched_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s)"
            " ON CONFLICT (url_hash) DO UPDATE SET status = EXCLUDED.status,"
            " http_status = EXCLUDED.http_status, final_url = EXCLUDED.final_url,"
            " title = EXCLUDED.title, description = EXCLUDED.description,"
            " lede = EXCLUDED.lede, text_chars = EXCLUDED.text_chars,"
            " published_at = EXCLUDED.published_at, language = EXCLUDED.language,"
            " error = EXCLUDED.error, attempts = midias_crawler_contents.attempts + 1,"
            " fetched_at = EXCLUDED.fetched_at",
            (
                record.url_hash,
                record.status,
                record.http_status,
                _text(record.final_url),
                _text(record.title),
                _text(record.description),
                _text(record.lede),
                record.text_chars,
                _utc(record.published_at),
                record.language,
                _text(record.error),
                _utc(now),
            ),
        )

    def get_content(self, url_hash: str) -> ContentRecord | None:
        """Conteúdo extraído de uma URL, se já houve download."""
        row = self._one(
            "SELECT url_hash, status, http_status, final_url, title, description, lede,"
            " text_chars, published_at, language, error FROM midias_crawler_contents"
            " WHERE url_hash = %s",
            (url_hash,),
        )
        return ContentRecord(*row) if row else None

    def record_analysis(
        self,
        record: AnalysisRecord,
        *,
        enrich_run_id: int,
        model: str,
        prompt_version: str,
        now: datetime,
    ) -> None:
        """Grava a análise de contexto; a primeira análise de uma URL vence."""
        self._run(
            "INSERT INTO midias_crawler_analyses (url_hash, finding, themes, entities,"
            " adverse_fact, summary, confidence, basis, model, prompt_version, enrich_run_id,"
            " analyzed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (url_hash) DO NOTHING",
            (
                record.url_hash,
                record.finding,
                list(record.themes),
                Jsonb(_clean_json(list(record.entities))),
                _text(record.adverse_fact),
                _text(record.summary),
                record.confidence,
                record.basis,
                model,
                prompt_version,
                enrich_run_id,
                _utc(now),
            ),
        )

    def get_analysis(self, url_hash: str) -> AnalysisRecord | None:
        """Análise de contexto de uma URL, se existir."""
        row = self._one(
            "SELECT url_hash, finding, themes, entities, adverse_fact, summary, confidence,"
            " basis, model, prompt_version FROM midias_crawler_analyses WHERE url_hash = %s",
            (url_hash,),
        )
        if row is None:
            return None
        return AnalysisRecord(
            url_hash=row[0],
            finding=row[1],
            themes=tuple(row[2]),
            entities=tuple(row[3]),
            adverse_fact=row[4],
            summary=row[5],
            confidence=row[6],
            basis=row[7],
            model=row[8],
            prompt_version=row[9],
        )

    def pending_evidence(
        self, limit: int | None = None, *, max_attempts: int = 3
    ) -> list[FindingUrl]:
        """Achados cuja evidência ainda não foi capturada (e que não esgotaram as tentativas)."""
        sql = (
            "SELECT u.url_hash, u.url, u.title, u.site_key, a.entities, a.finding"
            " FROM midias_crawler_analyses a JOIN midias_crawler_urls u ON u.url_hash = a.url_hash"
            " LEFT JOIN midias_crawler_evidences e ON e.url_hash = a.url_hash"
            f" WHERE {MEETS_CRITERIA_SQL} AND (e.url_hash IS NULL OR"
            " (e.status != 'captured' AND e.attempts < %s)) ORDER BY a.analyzed_at, a.url_hash"
        )
        params: list[Any] = [max_attempts]
        if limit is not None:
            sql += " LIMIT %s"
            params.append(limit)
        return [
            FindingUrl(
                url_hash=row[0],
                url=row[1],
                title=row[2],
                site_key=row[3],
                entity_names=tuple(
                    str(entity["name"])
                    for entity in row[4]
                    if entity.get("name")
                    and (not row[5] or is_adverse_role(str(entity.get("role", ""))))
                ),
                adverse=bool(row[5]),
            )
            for row in self._all(sql, params)
        ]

    def count_pending_evidence(self, *, max_attempts: int = 3) -> int:
        """Quantas matérias esperam evidência (contagem direta, sem juntar a tabela grande)."""
        row = self._one(
            "SELECT COUNT(*) FROM midias_crawler_analyses a LEFT JOIN midias_crawler_evidences e"
            " ON e.url_hash = a.url_hash"
            f" WHERE {MEETS_CRITERIA_SQL} AND (e.url_hash IS NULL OR"
            " (e.status != 'captured' AND e.attempts < %s))",
            (max_attempts,),
        )
        return int(row[0]) if row else 0

    def count_pending_enrichment(self) -> int:
        """Quantas URLs ``new`` ainda não têm análise de contexto."""
        row = self._one(
            "SELECT COUNT(*) FROM midias_crawler_urls u LEFT JOIN midias_crawler_analyses a"
            " ON a.url_hash = u.url_hash WHERE u.kind = %s AND a.url_hash IS NULL",
            (UrlKind.NEW.value,),
        )
        return int(row[0]) if row else 0

    def record_evidence(self, record: EvidenceRecord, *, now: datetime) -> None:
        """Grava (ou atualiza) o estado da evidência; cada chamada conta uma tentativa."""
        self._run(
            "INSERT INTO midias_crawler_evidences (url_hash, status, provider, path, sha256,"
            " size_bytes, content_type, http_status, final_url, rejected_reasons, error,"
            " attempts, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s)"
            " ON CONFLICT (url_hash) DO UPDATE SET status = EXCLUDED.status,"
            " provider = EXCLUDED.provider, path = EXCLUDED.path, sha256 = EXCLUDED.sha256,"
            " size_bytes = EXCLUDED.size_bytes, content_type = EXCLUDED.content_type,"
            " http_status = EXCLUDED.http_status, final_url = EXCLUDED.final_url,"
            " rejected_reasons = EXCLUDED.rejected_reasons, error = EXCLUDED.error,"
            " attempts = midias_crawler_evidences.attempts + 1, updated_at = EXCLUDED.updated_at",
            (
                record.url_hash,
                record.status,
                record.provider,
                _text(record.path),
                record.sha256,
                record.size_bytes,
                record.content_type,
                record.http_status,
                _text(record.final_url),
                list(record.rejected_reasons),
                _text(record.error),
                _utc(now),
            ),
        )

    def get_evidence(self, url_hash: str) -> EvidenceRecord | None:
        """Estado da evidência de uma URL, se houve tentativa."""
        row = self._one(
            "SELECT url_hash, status, provider, path, sha256, size_bytes, content_type,"
            " http_status, final_url, rejected_reasons, error, attempts"
            " FROM midias_crawler_evidences WHERE url_hash = %s",
            (url_hash,),
        )
        if row is None:
            return None
        return EvidenceRecord(
            url_hash=row[0],
            status=row[1],
            provider=row[2],
            path=row[3],
            sha256=row[4],
            size_bytes=row[5],
            content_type=row[6],
            http_status=row[7],
            final_url=row[8],
            rejected_reasons=tuple(row[9]),
            error=row[10],
            attempts=row[11],
        )

    def label_pool(self, group: str) -> list[LabelCandidate]:
        """Todas as URLs de um grupo de rotulagem, na ordem em que foram vistas.

        Args:
            group: ``in_corpus`` ou ``no_entity``.

        Raises:
            ValueError: Se o grupo for desconhecido.
        """
        if group not in LABEL_GROUPS:
            raise ValueError(f"grupo de rotulagem desconhecido: {group!r}")
        condition = (
            MEETS_CRITERIA_SQL
            if group == "in_corpus"
            else f"NOT {MEETS_CRITERIA_SQL} AND a.basis = 'text'"
        )
        rows = self._all(
            "SELECT u.url_hash, u.url, u.site_key, COALESCE(c.title, u.title), c.lede,"
            " a.themes, a.entities, a.adverse_fact, e.provider FROM midias_crawler_analyses a"
            " JOIN midias_crawler_urls u ON u.url_hash = a.url_hash"
            " LEFT JOIN midias_crawler_contents c ON c.url_hash = a.url_hash"
            " LEFT JOIN midias_crawler_evidences e ON e.url_hash = a.url_hash"
            f" WHERE {condition} ORDER BY u.id"
        )
        return [
            LabelCandidate(
                url_hash=row[0],
                group=group,
                url=row[1],
                site_key=row[2],
                headline=row[3],
                excerpt=row[4],
                themes=tuple(row[5] or ()),
                entity_names=tuple(str(entity["name"]) for entity in row[6] if entity.get("name")),
                adverse_fact=row[7] or "",
                evidence_provider=row[8],
            )
            for row in rows
        ]

    def label_population(self) -> dict[str, int]:
        """Tamanho de cada grupo de rotulagem (base das estimativas de recall)."""
        return {group: len(self.label_pool(group)) for group in LABEL_GROUPS}

    def evidence_rejection_reasons(self) -> dict[str, int]:
        """Quantas evidências tiveram o HTML direto rejeitado por cada motivo."""
        rows = self._all(
            "SELECT reason, COUNT(*) FROM midias_crawler_evidences,"
            " unnest(rejected_reasons) AS reason GROUP BY reason"
        )
        return {reason: int(count) for reason, count in rows}

    def recent_findings(self, limit: int) -> list[RecentFinding]:
        """Últimos achados da análise de contexto, do mais recente ao mais antigo."""
        rows = self._all(
            "SELECT a.analyzed_at, u.url, u.site_key, COALESCE(c.title, u.title), a.themes,"
            " a.entities, a.adverse_fact, a.summary, a.confidence, a.basis, e.status,"
            " e.provider, e.path FROM midias_crawler_analyses a"
            " JOIN midias_crawler_urls u ON u.url_hash = a.url_hash"
            " LEFT JOIN midias_crawler_contents c ON c.url_hash = a.url_hash"
            " LEFT JOIN midias_crawler_evidences e ON e.url_hash = a.url_hash"
            " WHERE a.finding ORDER BY a.analyzed_at DESC, u.id DESC LIMIT %s",
            (limit,),
        )
        return [
            RecentFinding(
                analyzed_at=row[0],
                url=row[1],
                site_key=row[2],
                headline=row[3],
                themes=tuple(row[4]),
                adverse_entities=tuple(
                    str(entity["name"])
                    for entity in row[5]
                    if entity.get("name") and is_adverse_role(str(entity.get("role", "")))
                ),
                adverse_fact=row[6],
                summary=row[7],
                confidence=row[8],
                basis=row[9],
                evidence_status=row[10],
                evidence_provider=row[11],
                evidence_path=row[12],
            )
            for row in rows
        ]

    # --- gravação no corpus ---

    def start_corpus_run(self, started_at: datetime) -> int:
        """Registra o início de uma execução da gravação no corpus e devolve o seu id."""
        row = self._one(
            "INSERT INTO midias_crawler_corpus_runs (started_at) VALUES (%s) RETURNING id",
            (_utc(started_at),),
        )
        return int(row[0]) if row else 0

    def finish_corpus_run(
        self, corpus_run_id: int, finished_at: datetime, totals: CorpusTotals
    ) -> None:
        """Grava o fim de uma execução da gravação no corpus."""
        self._run(
            "UPDATE midias_crawler_corpus_runs SET finished_at = %s, candidates = %s,"
            " created = %s, existing = %s, evidence_updated = %s, entities = %s, keywords = %s,"
            " failed = %s, rewritten = %s WHERE id = %s",
            (
                _utc(finished_at),
                totals.candidates,
                totals.created,
                totals.existing,
                totals.evidence_updated,
                totals.entities,
                totals.keywords,
                totals.failed,
                totals.rewritten,
                corpus_run_id,
            ),
        )

    def corpus_runs(self) -> list[CorpusRun]:
        """Todas as execuções da gravação no corpus, da mais antiga para a mais nova."""
        rows = self._all(
            "SELECT id, started_at, finished_at, candidates, created, existing,"
            " evidence_updated, entities, keywords, failed, rewritten"
            " FROM midias_crawler_corpus_runs ORDER BY id"
        )
        return [
            CorpusRun(
                id=row[0], started_at=row[1], finished_at=row[2], totals=CorpusTotals(*row[3:])
            )
            for row in rows
        ]

    def pending_corpus_sync(self, *, limit: int | None = None) -> list[CorpusCandidate]:
        """Matérias que atendem ao critério e ainda não estão no corpus.

        Inclui as já gravadas cuja evidência mudou desde a gravação.

        Args:
            limit: Máximo de achados.
        """
        sql = (
            "SELECT u.url_hash, u.url, u.site_key, COALESCE(c.title, u.title), c.description,"
            " c.lede, c.published_at, c.language, a.themes, a.entities, a.adverse_fact,"
            " a.summary, a.confidence, a.basis, a.model, e.status, e.path,"
            " COALESCE(e.attempts, 0), e.provider, a.finding FROM midias_crawler_analyses a"
            " JOIN midias_crawler_urls u ON u.url_hash = a.url_hash"
            " LEFT JOIN midias_crawler_contents c ON c.url_hash = a.url_hash"
            " LEFT JOIN midias_crawler_evidences e ON e.url_hash = a.url_hash"
            " LEFT JOIN midias_crawler_corpus_writes w ON w.url_hash = a.url_hash"
            f" WHERE {MEETS_CRITERIA_SQL}"
            " AND (w.url_hash IS NULL OR w.evidence_state != COALESCE(e.status, 'pending'))"
            " ORDER BY a.analyzed_at, a.url_hash"
        )
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT %s"
            params.append(limit)
        return [
            CorpusCandidate(
                url_hash=row[0],
                url=row[1],
                site_key=row[2],
                title=row[3],
                description=row[4],
                lede=row[5],
                published_at=row[6],
                language=row[7],
                themes=tuple(row[8]),
                entities=tuple(row[9]),
                adverse_fact=row[10],
                summary=row[11],
                confidence=row[12],
                basis=row[13],
                model=row[14],
                evidence_status=row[15],
                evidence_path=row[16],
                evidence_attempts=row[17],
                evidence_provider=row[18],
                adverse=bool(row[19]),
            )
            for row in self._all(sql, params)
        ]

    def record_corpus_write(
        self,
        url_hash: str,
        *,
        article_id: int | None,
        created: bool,
        evidence_state: str,
        corpus_run_id: int,
        now: datetime,
    ) -> None:
        """Registra que o achado foi gravado (ou reconhecido) no corpus e o estado da evidência."""
        self._run(
            "INSERT INTO midias_crawler_corpus_writes (url_hash, article_id, created,"
            " evidence_state, written_at, corpus_run_id) VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (url_hash) DO UPDATE SET article_id = EXCLUDED.article_id,"
            " evidence_state = EXCLUDED.evidence_state, written_at = EXCLUDED.written_at,"
            " corpus_run_id = EXCLUDED.corpus_run_id,"
            " created = midias_crawler_corpus_writes.created OR EXCLUDED.created",
            (url_hash, article_id, created, evidence_state, _utc(now), corpus_run_id),
        )

    def corpus_written_hashes(self) -> list[str]:
        """Hashes já gravados no corpus por este sistema."""
        return [row[0] for row in self._all("SELECT url_hash FROM midias_crawler_corpus_writes")]

    def forget_corpus_writes(self, url_hashes: Sequence[str]) -> None:
        """Esquece gravações (ex.: sumiram do corpus), para o achado ser gravado de novo."""
        for chunk in _chunks(list(url_hashes)):
            self._run(
                "DELETE FROM midias_crawler_corpus_writes WHERE url_hash = ANY(%s)", (list(chunk),)
            )

    def corpus_write_counts(self) -> dict[str, int]:
        """Quantos achados foram gravados (``written``), criados por nós (``created``)."""
        row = self._one(
            "SELECT COUNT(*), COALESCE(SUM(created::int), 0) FROM midias_crawler_corpus_writes"
        )
        return (
            {"written": int(row[0]), "created": int(row[1])}
            if row
            else {"written": 0, "created": 0}
        )

    def enrichment_counts(self) -> dict[str, Any]:
        """Contagens para o painel: conteúdo, análises, evidências e pendências."""
        content = self._all("SELECT status, COUNT(*) FROM midias_crawler_contents GROUP BY status")
        analysis = self._one(
            "SELECT COALESCE(SUM(a.finding::int), 0), COALESCE(SUM((NOT a.finding)::int), 0),"
            " COALESCE(SUM((a.basis = 'headline')::int), 0),"
            f" COALESCE(SUM(({MEETS_CRITERIA_SQL})::int), 0) FROM midias_crawler_analyses a"
        )
        evidence = self._all(
            "SELECT status || ':' || COALESCE(provider, '-'), COUNT(*)"
            " FROM midias_crawler_evidences GROUP BY 1"
        )
        assert analysis is not None
        return {
            "content": {status: int(count) for status, count in content},
            "analysis": {
                "findings": int(analysis[0]),
                "no_finding": int(analysis[1]),
                "headline_only": int(analysis[2]),
                "relevant": int(analysis[3]),
            },
            "evidence": {key: int(count) for key, count in evidence},
            "pending_enrichment": self.count_pending_enrichment(),
            "pending_evidence": self.count_pending_evidence(),
        }

    # --- inventário estrutural de seções ---

    def mapping_pages(self, site_key: str) -> list[MapPage]:
        """Páginas da fonte em ordem de descoberta, sem truncar o inventário.

        O DDL novo só é exigido por quem usa o mapeamento: os demais comandos
        continuam compatíveis com bancos que ainda não receberam os arquivos 004 e 006.
        """
        row = self._one("SELECT to_regclass('midias_crawler_section_pages')")
        if row is None or row[0] is None:
            raise StateSchemaError(
                "rode sql/postgres/004_create_midias_crawler_section_pages.sql"
            )
        if not self._mapping_classification_ready:
            exists = self._one(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()"
                " AND table_name = 'midias_crawler_section_pages' AND column_name = 'page_type'"
            )
            if exists is None:
                raise StateSchemaError(
                    'rode sql/postgres/006_add_midias_crawler_page_classification.sql'
                )
            self._mapping_classification_ready = True
        rows = self._all(
            "SELECT url, found_on, kind, status, title, article_urls, feeds, feed_indexes,"
            " detail, attempts, checked_at, page_type, type_basis, discovery_basis"
            " FROM midias_crawler_section_pages"
            " WHERE site_key = %s ORDER BY id", (site_key,),
        )
        return [MapPage(
            url=r[0], found_on=r[1], kind=r[2], status=PageStatus(r[3]), title=r[4],
            article_urls=tuple(r[5]), feeds=tuple(r[6]), feed_indexes=tuple(r[7]),
            detail=r[8], attempts=r[9], checked_at=r[10], page_type=r[11], type_basis=r[12],
            discovery_basis=r[13],
        ) for r in rows]

    def mapping_mechanism_rows(self, site_key: str | None = None) -> list[dict[str, Any]]:
        """Metadados dos mecanismos, sem transportar listas grandes de notícias."""
        where = " WHERE site_key=%s" if site_key is not None else ""
        rows = self._all(
            "SELECT site_key,url,kind,verdict,checked_at,partial,detail,"
            "jsonb_array_length(listed_urls),listed_urls->>0,listed_urls->>1,"
            "listed_urls->>2,listed_urls->>3,listed_urls->>4"
            " FROM midias_crawler_section_sources" + where + " ORDER BY site_key,url",
            (site_key,) if site_key is not None else (),
        )
        fields = ("site_key", "url", "kind", "verdict", "checked_at", "partial", "detail",
                  "listed_count")
        return [{**dict(zip(fields, row[:8], strict=True)), "samples": list(row[8:])}
                for row in rows]

    def mapping_inventory_pages(self, site_key: str) -> list[MapPage]:
        """Páginas estruturais com até cinco exemplos para associação offline limitada."""
        rows = self._all(
            "SELECT url,title,kind,status,page_type,checked_at,feeds,discovery_basis,"
            "article_urls->>0,article_urls->>1,article_urls->>2,article_urls->>3,article_urls->>4"
            " FROM midias_crawler_section_pages WHERE site_key=%s AND status<>'excluded'"
            " AND (kind='root' OR (kind='section'"
            " AND page_type NOT IN ('article','service','taxonomy'))) ORDER BY id",
            (site_key,),
        )
        return [MapPage(
            url=r[0], found_on="", title=r[1], kind=r[2], status=PageStatus(r[3]),
            page_type=r[4], checked_at=r[5], feeds=tuple(r[6]), discovery_basis=r[7],
            article_urls=tuple(u for u in r[8:] if u),
        ) for r in rows]

    def mapping_overview(self) -> dict[str, dict[str, int]]:
        """Contagens estruturais por fonte sem transportar os arrays de notícias ao painel."""
        rows = self._all(
            "SELECT site_key, count(*),"
            " count(*) FILTER (WHERE status='ok'),"
            " count(*) FILTER (WHERE status='ok' AND kind='section' AND page_type='section'),"
            " count(*) FILTER (WHERE status='ok' AND jsonb_array_length(article_urls)>0),"
            " count(*) FILTER (WHERE status='pending'),"
            " count(*) FILTER (WHERE status IN ('error','invalid','blocked','disallowed'))"
            " FROM midias_crawler_section_pages"
            " WHERE kind='root' OR (kind='section'"
            " AND page_type NOT IN ('article','service','taxonomy') AND status<>'excluded')"
            " GROUP BY site_key"
        )
        fields = ("pages", "read", "confirmed", "with_examples", "pending", "failures")
        return {row[0]: dict(zip(fields, row[1:], strict=True)) for row in rows}

    def mapping_section_rows(
        self, site_key: str, *, mode: str, search: str, offset: int, limit: int,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Página de seções com exemplos observados; lê somente as primeiras cinco amostras."""
        filters = {
            "examples": "status='ok' AND jsonb_array_length(article_urls)>0",
            "read": "status='ok'",
            "gaps": "status IN ('pending','error','invalid','blocked','disallowed')",
            "all": "TRUE",
        }
        if mode not in filters:
            raise ValueError("filtro de seções inválido")
        where = (
            "site_key=%s AND (kind='root' OR (kind='section'"
            " AND page_type NOT IN ('article','service','taxonomy'))) AND status<>'excluded'"
            " AND " + filters[mode]
            + " AND strpos(lower(title || ' ' || url), lower(%s))>0"
        )
        rows = self._all(
            "SELECT url,title,kind,status,page_type,type_basis,discovery_basis,checked_at,"
            " detail,feeds,"
            " article_urls->>0,article_urls->>1,article_urls->>2,article_urls->>3,"
            " article_urls->>4,count(*) OVER() FROM midias_crawler_section_pages WHERE "
            + where + " ORDER BY (kind<>'root'),(page_type<>'section'),url LIMIT %s OFFSET %s",
            (site_key, search, limit, offset),
        )
        if rows:
            total = rows[0][-1]
        else:
            count = self._one(
                "SELECT count(*) FROM midias_crawler_section_pages WHERE " + where,
                (site_key, search),
            )
            total = count[0] if count else 0
        fields = ("url", "title", "kind", "status", "page_type", "type_basis",
                  "discovery_basis", "checked_at", "detail", "feeds")
        return total, [
            {**dict(zip(fields, row[:10], strict=True)), "samples": list(row[10:15])}
            for row in rows
        ]

    def save_mapping_pages(self, site_key: str, pages: Sequence[MapPage]) -> None:
        """Grava o checkpoint e as páginas descobertas atomicamente, sem apagar outras."""
        with self.transaction():
            for p in pages:
                self._run(
                    "INSERT INTO midias_crawler_section_pages"
                    " (site_key, url, found_on, kind, status, title, article_urls, feeds,"
                    " feed_indexes, detail, attempts, checked_at, page_type, type_basis,"
                    " discovery_basis)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (site_key, url) DO UPDATE SET status = EXCLUDED.status,"
                    " title = EXCLUDED.title, article_urls = EXCLUDED.article_urls,"
                    " feeds = EXCLUDED.feeds, feed_indexes = EXCLUDED.feed_indexes,"
                    " detail = EXCLUDED.detail, attempts = EXCLUDED.attempts,"
                    " checked_at = EXCLUDED.checked_at, page_type = EXCLUDED.page_type,"
                    " type_basis = EXCLUDED.type_basis, discovery_basis = EXCLUDED.discovery_basis",
                    (site_key, p.url, p.found_on, p.kind, p.status.value,
                     _text(p.title) or "", Jsonb(_clean_json(list(p.article_urls))),
                     Jsonb(_clean_json(list(p.feeds))), Jsonb(_clean_json(list(p.feed_indexes))),
                     _text(p.detail) or "", p.attempts, p.checked_at, p.page_type, p.type_basis,
                     p.discovery_basis),
                )

    def mapping_sources(self, site_key: str) -> list[MapSource]:
        """Leituras dos mecanismos usadas para conferir as amostras das seções."""
        row = self._one("SELECT to_regclass('midias_crawler_section_sources')")
        if row is None or row[0] is None:
            raise StateSchemaError(
                "rode sql/postgres/005_create_midias_crawler_section_sources.sql"
            )
        rows = self._all(
            "SELECT url, verdict, kind, listed_urls, checked_at, requests, partial, detail"
            " FROM midias_crawler_section_sources WHERE site_key = %s ORDER BY url", (site_key,),
        )
        return [MapSource(r[0], r[1], r[2], tuple(r[3]), r[4], r[5], r[6], r[7]) for r in rows]

    def save_mapping_sources(self, site_key: str, sources: Sequence[MapSource]) -> None:
        """Persiste as leituras sem alterar as estratégias de descoberta existentes."""
        with self.transaction():
            for s in sources:
                self._run(
                    "INSERT INTO midias_crawler_section_sources"
                    " (site_key, url, verdict, kind, listed_urls, checked_at, requests, partial,"
                    " detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (site_key, url) DO UPDATE SET verdict = EXCLUDED.verdict,"
                    " kind = EXCLUDED.kind, listed_urls = EXCLUDED.listed_urls,"
                    " checked_at = EXCLUDED.checked_at, requests = EXCLUDED.requests,"
                    " partial = EXCLUDED.partial, detail = EXCLUDED.detail",
                    (site_key, s.url, s.verdict, s.kind, Jsonb(list(s.listed_urls)), s.checked_at,
                     s.requests, s.partial, _text(s.detail) or ''),
                )

    # --- estratégia de descoberta por fonte ---

    def replace_site_strategies(
        self, site_key: str, reports: Sequence[CandidateReport], probed_at: datetime
    ) -> None:
        """Substitui, numa transação, tudo o que se sabia dos candidatos de uma fonte.

        Args:
            site_key: A fonte.
            reports: Os candidatos sondados (vazio limpa a fonte); no máximo um principal e uma
                reserva, o que o banco garante.
            probed_at: Quando a sondagem aconteceu.
        """
        with self.transaction():
            self._run(
                "DELETE FROM midias_crawler_source_strategies WHERE site_key = %s", (site_key,)
            )
            for r in reports:
                m = r.measure
                self._run(
                    "INSERT INTO midias_crawler_source_strategies (site_key, url, origin,"
                    " verdict, kind, role, score, items, dated_items, recent_items,"
                    " titled_items, newest_at, size_bytes, requests, detail, probed_at,"
                    " coverage, union_recent, cover_share, section_count, top_sections)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                    " %s, %s, %s, %s, %s)",
                    (
                        site_key,
                        r.url,
                        r.origin.value,
                        r.verdict.value,
                        r.kind.value if r.kind else None,
                        r.role.value if r.role else None,
                        r.score,
                        m.items if m else 0,
                        m.dated if m else 0,
                        m.recent if m else 0,
                        m.titled if m else 0,
                        _utc(m.newest) if m else None,
                        m.size_bytes if m else 0,
                        r.requests,
                        _text(r.detail) or "",
                        _utc(probed_at),
                        r.coverage,
                        r.union_recent,
                        r.cover_share,
                        m.sections if m else 0,
                        Jsonb([[name, count] for name, count in m.top_sections] if m else []),
                    ),
                )

    def site_strategies(self, site_key: str) -> list[CandidateReport]:
        """Os candidatos sondados de uma fonte: os escolhidos primeiro, depois por nota."""
        rows = self._all(
            _STRATEGY_SELECT
            + " WHERE site_key = %s ORDER BY (role IS NULL), role, score DESC, url",
            (site_key,),
        )
        return [_strategy_from_row(row[1:]) for row in rows]

    def chosen_strategies(self) -> list[tuple[str, CandidateReport]]:
        """O principal e a reserva de todas as fontes, por fonte (o principal antes).

        Mantém a visão principal/reserva dos relatórios existentes. A execução usa
        ``discovery_strategies``, que também inclui os complementos escolhidos.
        """
        rows = self._all(
            _STRATEGY_SELECT + " WHERE role IN ('primary', 'backup')"
            " ORDER BY site_key, (role <> 'primary')"
        )
        return [(row[0], _strategy_from_row(row[1:])) for row in rows]

    def strategy_rows(self) -> list[tuple[str, CandidateReport, datetime]]:
        """Todos os candidatos gravados, com a fonte e o instante da sondagem."""
        rows = self._all(_STRATEGY_SELECT + " ORDER BY site_key, score DESC, url")
        return [(row[0], _strategy_from_row(row[1:]), row[-1]) for row in rows]

    def discovery_strategies(self) -> list[tuple[str, CandidateReport]]:
        """Principal, reserva e complementos escolhidos, ordenados para a descoberta."""
        rows = self._all(
            _STRATEGY_SELECT + " WHERE role IN ('primary', 'backup', 'complement')"
            " ORDER BY site_key, CASE role WHEN 'primary' THEN 0 WHEN 'backup' THEN 1 ELSE 2 END,"
            " score DESC, url"
        )
        return [(row[0], _strategy_from_row(row[1:])) for row in rows]

    def require_combined_discovery(self) -> None:
        """Confere a migração da origem combinada antes de iniciar uma rodada com complementos."""
        row = self._one(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conrelid = 'midias_crawler_site_runs'::regclass"
            " AND conname = 'midias_crawler_site_runs_source_check'"
        )
        if not row or "'combined'" not in str(row[0]):
            raise StateSchemaError(
                "aplique sql/postgres/007_allow_combined_discovery.sql antes de usar complementos"
            )

    def strategy_probed_at(self, site_key: str) -> datetime | None:
        """Quando a fonte foi sondada pela última vez, ou ``None`` se nunca foi."""
        row = self._one(
            "SELECT max(probed_at) FROM midias_crawler_source_strategies WHERE site_key = %s",
            (site_key,),
        )
        return row[0] if row else None

    def strategy_probe_times(self) -> dict[str, datetime]:
        """Quando cada fonte com estratégia gravada foi sondada pela última vez."""
        rows = self._all(
            "SELECT site_key, max(probed_at) FROM midias_crawler_source_strategies"
            " GROUP BY site_key"
        )
        return {row[0]: row[1] for row in rows}

    def probe_states(self) -> dict[str, ProbeState]:
        """O estado de reavaliação de cada fonte já sondada."""
        rows = self._all(
            "SELECT site_key, probed_at, found, streak, cooldown_until, refresh_at, detail"
            " FROM midias_crawler_source_probe_state"
        )
        return {row[0]: ProbeState(*row) for row in rows}

    def record_probe_state(self, state: ProbeState, reason: str) -> None:
        """Grava (ou substitui) o estado de reavaliação de uma fonte.

        Args:
            state: O estado depois da sondagem.
            reason: Por que a sondagem aconteceu (vazio: manual).
        """
        self._run(
            "INSERT INTO midias_crawler_source_probe_state (site_key, probed_at, found, streak,"
            " cooldown_until, refresh_at, reason, detail)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (site_key) DO UPDATE SET probed_at = EXCLUDED.probed_at,"
            " found = EXCLUDED.found, streak = EXCLUDED.streak,"
            " cooldown_until = EXCLUDED.cooldown_until, refresh_at = EXCLUDED.refresh_at,"
            " reason = EXCLUDED.reason, detail = EXCLUDED.detail",
            (
                state.site_key,
                _utc(state.probed_at),
                state.found,
                state.streak,
                _utc(state.cooldown_until),
                _utc(state.refresh_at),
                reason,
                _text(state.detail) or "",
            ),
        )

    def recent_rounds(self, per_site: int) -> dict[str, list[RoundHealth]]:
        """As últimas ``per_site`` rodadas de cada fonte, da mais nova para a mais antiga."""
        rows = self._all(
            "SELECT site_key, started_at, status, new_urls, seen_urls, sitemaps_skipped FROM ("
            " SELECT sr.site_key, r.started_at, sr.status, sr.new_urls, sr.seen_urls,"
            " sr.sitemaps_skipped, ROW_NUMBER() OVER ("
            " PARTITION BY sr.site_key ORDER BY sr.run_id DESC) AS n"
            " FROM midias_crawler_site_runs sr JOIN midias_crawler_runs r ON r.id = sr.run_id"
            ") ranked WHERE n <= %s ORDER BY site_key, n",
            (per_site,),
        )
        rounds: dict[str, list[RoundHealth]] = {}
        for site_key, *fields in rows:
            rounds.setdefault(site_key, []).append(RoundHealth(*fields))
        return rounds

    # --- fallback ---

    def fallback_hosts(self) -> frozenset[str]:
        """Hosts que já nos bloquearam e devem ir direto ao fallback."""
        return frozenset(
            row[0] for row in self._all("SELECT host FROM midias_crawler_fallback_hosts")
        )

    def add_fallback_hosts(self, hosts: Iterable[str], now: datetime) -> None:
        """Marca hosts como dependentes do fallback (idempotente)."""
        for host in hosts:
            self._run(
                "INSERT INTO midias_crawler_fallback_hosts (host, first_marked_at)"
                " VALUES (%s, %s) ON CONFLICT (host) DO NOTHING",
                (host, _utc(now)),
            )
