"""Estado persistente em SQLite: o que já vimos, o que mudou e como cada rodada se saiu."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from buscador_midias.criteria import MEETS_CRITERIA_SQL
from buscador_midias.domain.discovered import DiscoveredUrl
from buscador_midias.domain.roles import is_adverse_role

# O SQLite limita o número de variáveis por consulta (999 em versões antigas).
_CHUNK = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    bright_data_requests INTEGER NOT NULL DEFAULT 0,
    bright_data_hosts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS urls_seen (
    url_hash TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    site_key TEXT NOT NULL,
    source_sitemap TEXT NOT NULL,
    kind TEXT NOT NULL,
    first_seen_run INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    lastmod TEXT,
    title TEXT,
    publication_date TEXT,
    filter_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_urls_site ON urls_seen(site_key);
CREATE INDEX IF NOT EXISTS idx_urls_run ON urls_seen(first_seen_run);
CREATE INDEX IF NOT EXISTS idx_urls_kind_seen ON urls_seen(kind, first_seen_at);
CREATE TABLE IF NOT EXISTS site_kind_counts (
    site_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    n INTEGER NOT NULL,
    PRIMARY KEY (site_key, kind)
);
CREATE TABLE IF NOT EXISTS sitemap_state (
    sitemap_url TEXT PRIMARY KEY,
    site_key TEXT NOT NULL,
    lastmod TEXT,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS site_baseline (
    site_key TEXT PRIMARY KEY,
    run_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS site_runs (
    run_id INTEGER NOT NULL,
    site_key TEXT NOT NULL,
    status TEXT NOT NULL,
    new_urls INTEGER NOT NULL,
    baseline_urls INTEGER NOT NULL,
    backlog_urls INTEGER NOT NULL,
    seen_urls INTEGER NOT NULL,
    sitemaps_fetched INTEGER NOT NULL,
    sitemaps_skipped INTEGER NOT NULL,
    detail TEXT NOT NULL,
    known_urls INTEGER NOT NULL DEFAULT 0,
    sitemaps_failed INTEGER NOT NULL DEFAULT 0,
    filtered_urls INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, site_key)
);
CREATE TABLE IF NOT EXISTS url_content (
    url_hash TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    http_status INTEGER,
    final_url TEXT,
    title TEXT,
    description TEXT,
    lede TEXT,
    text_chars INTEGER NOT NULL DEFAULT 0,
    published_at TEXT,
    language TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS url_analysis (
    url_hash TEXT PRIMARY KEY,
    finding INTEGER NOT NULL,
    themes TEXT NOT NULL,
    entities TEXT NOT NULL,
    adverse_fact TEXT NOT NULL,
    summary TEXT NOT NULL,
    confidence REAL NOT NULL,
    basis TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    enrich_run_id INTEGER NOT NULL,
    analyzed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS url_evidence (
    url_hash TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    provider TEXT,
    path TEXT,
    sha256 TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    content_type TEXT,
    http_status INTEGER,
    final_url TEXT,
    rejected_reasons TEXT NOT NULL DEFAULT '',
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enrich_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    evidence_mode TEXT NOT NULL,
    urls INTEGER NOT NULL DEFAULT 0,
    fetched_ok INTEGER NOT NULL DEFAULT 0,
    fetched_blocked INTEGER NOT NULL DEFAULT 0,
    fetched_other INTEGER NOT NULL DEFAULT 0,
    analyzed INTEGER NOT NULL DEFAULT 0,
    findings INTEGER NOT NULL DEFAULT 0,
    evidence_reused INTEGER NOT NULL DEFAULT 0,
    evidence_brightdata INTEGER NOT NULL DEFAULT 0,
    evidence_failed INTEGER NOT NULL DEFAULT 0,
    bright_data_requests INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    retries INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS corpus_writes (
    url_hash TEXT PRIMARY KEY,
    article_id INTEGER,
    created INTEGER NOT NULL,
    evidence_state TEXT NOT NULL,
    written_at TEXT NOT NULL,
    corpus_run_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS corpus_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    candidates INTEGER NOT NULL DEFAULT 0,
    created INTEGER NOT NULL DEFAULT 0,
    existing INTEGER NOT NULL DEFAULT 0,
    evidence_updated INTEGER NOT NULL DEFAULT 0,
    entities INTEGER NOT NULL DEFAULT 0,
    keywords INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    withheld INTEGER NOT NULL DEFAULT 0,
    rewritten INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fallback_hosts (
    host TEXT PRIMARY KEY,
    first_marked_at TEXT NOT NULL
);
"""


# Colunas acrescentadas depois da primeira versão do esquema: (tabela, coluna, definição).
_ADDED_COLUMNS = (
    ("site_runs", "sitemaps_failed", "INTEGER NOT NULL DEFAULT 0"),
    ("site_runs", "filtered_urls", "INTEGER NOT NULL DEFAULT 0"),
    ("site_runs", "source", "TEXT NOT NULL DEFAULT ''"),
    ("urls_seen", "filter_reason", "TEXT"),
    ("runs", "bright_data_requests", "INTEGER NOT NULL DEFAULT 0"),
    ("runs", "bright_data_hosts", "INTEGER NOT NULL DEFAULT 0"),
)


LABEL_GROUPS = ("in_corpus", "no_entity")


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
        source: De onde vieram as URLs (``sitemap``, ``feed`` ou ``sitemap_bd``); vazio em falha.
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


def _to_text(value: datetime | None) -> str | None:
    """Datas são gravadas em ISO 8601 UTC."""
    return value.astimezone(UTC).isoformat() if value is not None else None


def _from_text(value: str | None) -> datetime | None:
    """Lê uma data gravada por ``_to_text``."""
    return datetime.fromisoformat(value) if value else None


class StateStore:
    """Estado da POC em SQLite, seguro para uso por várias threads (uma trava serializa tudo)."""

    def __init__(self, path: str | Path = ":memory:", *, read_only: bool = False) -> None:
        """Abre (criando, se preciso) o banco e garante o esquema.

        Args:
            path: Caminho do arquivo SQLite, ou ``":memory:"`` para um banco descartável.
            read_only: Abre um arquivo já existente só para leitura (sem criar nem migrar nada),
                para o painel acompanhar o estado enquanto outro processo grava.
        """
        self._lock = threading.RLock()
        if read_only:
            self._conn = sqlite3.connect(
                f"file:{Path(path).resolve()}?mode=ro", uri=True, check_same_thread=False
            )
            return
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        with self._lock, self._conn:
            if str(path) != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._add_missing_columns()
            self._ensure_counters()

    def _add_missing_columns(self) -> None:
        """Acrescenta colunas criadas depois do primeiro esquema a bancos já existentes."""
        for table, column, definition in _ADDED_COLUMNS:
            columns = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _ensure_counters(self) -> None:
        """Reconstrói os contadores por fonte e tipo se o banco veio de antes deles (uma vez)."""
        has_counts = self._conn.execute("SELECT 1 FROM site_kind_counts LIMIT 1").fetchone()
        if has_counts is None:
            self._conn.execute(
                "INSERT INTO site_kind_counts (site_key, kind, n)"
                " SELECT site_key, kind, COUNT(*) FROM urls_seen GROUP BY site_key, kind"
            )

    def backup(self, destination: Path) -> None:
        """Grava uma cópia consistente do estado, mesmo com outros processos escrevendo nele.

        Usa a API de backup do SQLite (uma simples cópia do arquivo poderia pegar o banco pela
        metade). A cópia é escrita num arquivo ``.partial`` e só então ganha o nome final.

        Args:
            destination: Arquivo de destino; as pastas são criadas e ele não pode já existir.

        Raises:
            FileExistsError: Se ``destination`` já existe (nada é sobrescrito).
        """
        if destination.exists():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".partial")
        partial.unlink(missing_ok=True)
        target = sqlite3.connect(str(partial))
        try:
            with self._lock:
                self._conn.backup(target)
            # A cópia vira um arquivo único (sem -wal/-shm), fácil de guardar e de abrir depois.
            target.execute("PRAGMA journal_mode=DELETE")
        except BaseException:
            target.close()
            partial.unlink(missing_ok=True)
            raise
        target.close()
        partial.rename(destination)

    def integrity_ok(self) -> bool:
        """Se o SQLite considera o arquivo íntegro (``PRAGMA integrity_check``)."""
        with self._lock:
            row = self._conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and row[0] == "ok"

    def close(self) -> None:
        """Fecha a conexão."""
        with self._lock:
            self._conn.close()

    # --- rodadas ---

    def start_run(self, started_at: datetime) -> int:
        """Registra o início de uma rodada e devolve o seu id."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO runs (started_at) VALUES (?)", (_to_text(started_at),)
            )
            return int(cursor.lastrowid or 0)

    def finish_run(self, run_id: int, finished_at: datetime) -> None:
        """Registra o fim de uma rodada."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET finished_at = ? WHERE id = ?", (_to_text(finished_at), run_id)
            )

    def record_run_usage(
        self, run_id: int, *, bright_data_requests: int, bright_data_hosts: int
    ) -> None:
        """Grava o consumo pago (Bright Data) de uma rodada."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE runs SET bright_data_requests = ?, bright_data_hosts = ? WHERE id = ?",
                (bright_data_requests, bright_data_hosts, run_id),
            )

    def list_runs(self) -> list[RunRecord]:
        """Todas as rodadas, da mais antiga para a mais nova."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, started_at, finished_at, bright_data_requests, bright_data_hosts"
                " FROM runs ORDER BY id"
            ).fetchall()
        return [
            RunRecord(
                id=row[0],
                started_at=datetime.fromisoformat(row[1]),
                finished_at=_from_text(row[2]),
                bright_data_requests=row[3],
                bright_data_hosts=row[4],
            )
            for row in rows
        ]

    # --- URLs vistas ---

    def seen_hashes(self, hashes: Iterable[str]) -> set[str]:
        """Dos hashes informados, quais já estão registrados."""
        wanted = list(hashes)
        found: set[str] = set()
        with self._lock:
            for start in range(0, len(wanted), _CHUNK):
                chunk = wanted[start : start + _CHUNK]
                marks = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT url_hash FROM urls_seen WHERE url_hash IN ({marks})", chunk
                ).fetchall()
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
        rows = [
            (
                u.url_hash,
                u.url,
                u.site_key,
                u.source_sitemap,
                kind.value,
                run_id,
                _to_text(now),
                _to_text(u.lastmod),
                u.title,
                _to_text(u.publication_date),
                (reasons or {}).get(u.url_hash),
            )
            for u in urls
        ]
        added: Counter[str] = Counter()
        with self._lock, self._conn:
            for row in rows:
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO urls_seen (url_hash, url, site_key, source_sitemap,"
                    " kind, first_seen_run, first_seen_at, lastmod, title, publication_date,"
                    " filter_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
                if cursor.rowcount == 1:
                    added[row[2]] += 1
            self._conn.executemany(
                "INSERT INTO site_kind_counts (site_key, kind, n) VALUES (?, ?, ?)"
                " ON CONFLICT(site_key, kind) DO UPDATE SET n = n + excluded.n",
                [(site, kind.value, count) for site, count in added.items()],
            )
            return sum(added.values())

    def count_urls(self, *, kind: UrlKind | None = None) -> int:
        """Total de URLs registradas, opcionalmente de um só tipo."""
        with self._lock:
            if kind is None:
                row = self._conn.execute("SELECT COUNT(*) FROM urls_seen").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM urls_seen WHERE kind = ?", (kind.value,)
                ).fetchone()
        return int(row[0])

    def count_urls_by_kind(self) -> dict[str, int]:
        """Quantas URLs há de cada tipo (``new``, ``baseline``, ``backlog``...).

        Lê os contadores mantidos em ``record_urls``: varrer ``urls_seen`` a cada consulta custaria
        segundos com milhões de linhas.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, SUM(n) FROM site_kind_counts GROUP BY kind"
            ).fetchall()
        return {kind: int(count) for kind, count in rows}

    def new_urls_per_hour(self, *, since: datetime) -> dict[str, int]:
        """URLs ``new`` descobertas por hora (``AAAA-MM-DDTHH``, UTC) desde ``since``."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT substr(first_seen_at, 1, 13), COUNT(*) FROM urls_seen"
                " WHERE kind = ? AND first_seen_at >= ? GROUP BY 1 ORDER BY 1",
                (UrlKind.NEW.value, _to_text(since)),
            ).fetchall()
        return {hour: int(count) for hour, count in rows}

    def analyzed_per_hour(self, *, since: datetime) -> dict[str, dict[str, int]]:
        """Análises por hora (UTC) desde ``since``: as que atendem ao critério e as que não."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT substr(a.analyzed_at, 1, 13),"
                f" CASE WHEN {MEETS_CRITERIA_SQL} THEN 'meets' ELSE 'not_meets' END, COUNT(*)"
                " FROM url_analysis a WHERE a.analyzed_at >= ? GROUP BY 1, 2 ORDER BY 1",
                (_to_text(since),),
            ).fetchall()
        hours: dict[str, dict[str, int]] = {}
        for hour, verdict, count in rows:
            hours.setdefault(hour, {})[verdict] = int(count)
        return hours

    def recent_new_urls(self, limit: int) -> list[RecentUrl]:
        """Últimas URLs ``new`` descobertas, da mais recente para a mais antiga."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT u.first_seen_at, u.url, u.site_key, u.title, a.themes,"
                " CASE WHEN a.url_hash IS NULL THEN 'pending'"
                f" WHEN {MEETS_CRITERIA_SQL} THEN 'in_corpus' ELSE 'no_entity' END"
                " FROM urls_seen u"
                " LEFT JOIN url_analysis a ON a.url_hash = u.url_hash"
                " WHERE u.kind = ? ORDER BY u.first_seen_at DESC, u.rowid DESC LIMIT ?",
                (UrlKind.NEW.value, limit),
            ).fetchall()
        return [
            RecentUrl(
                first_seen_at=datetime.fromisoformat(row[0]),
                url=row[1],
                site_key=row[2],
                title=row[3],
                status=row[5],
                themes=tuple(theme for theme in (row[4] or "").split(",") if theme),
            )
            for row in rows
        ]

    def url_counts_by_site(self) -> dict[str, dict[str, int]]:
        """Contagem de URLs por fonte e tipo (dos contadores mantidos em ``record_urls``)."""
        with self._lock:
            rows = self._conn.execute("SELECT site_key, kind, n FROM site_kind_counts").fetchall()
        counts: dict[str, dict[str, int]] = {}
        for site, kind, count in rows:
            counts.setdefault(site, {})[kind] = int(count)
        return counts

    def last_new_by_site(self) -> dict[str, datetime]:
        """Quando cada fonte trouxe a sua URL ``new`` mais recente."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT site_key, MAX(first_seen_at) FROM urls_seen WHERE kind = ?"
                " GROUP BY site_key",
                (UrlKind.NEW.value,),
            ).fetchall()
        return {site: datetime.fromisoformat(stamp) for site, stamp in rows}

    def sites_done(self, run_id: int) -> int:
        """Quantos sites já têm resultado gravado na rodada (progresso de uma rodada em curso)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM site_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row[0])

    def filtered_by_reason(self) -> dict[str, int]:
        """Quantas URLs a camada L0 descartou, por motivo."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT filter_reason, COUNT(*) FROM urls_seen"
                " WHERE kind = ? AND filter_reason IS NOT NULL GROUP BY filter_reason",
                (UrlKind.FILTERED.value,),
            ).fetchall()
        return {reason: int(count) for reason, count in rows}

    def new_url_latencies_hours(self) -> list[float]:
        """Horas entre a publicação (news sitemap) e a primeira vez que vimos cada URL nova."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT first_seen_at, publication_date FROM urls_seen"
                " WHERE kind = ? AND publication_date IS NOT NULL",
                (UrlKind.NEW.value,),
            ).fetchall()
        return [
            (datetime.fromisoformat(seen) - datetime.fromisoformat(published)).total_seconds()
            / 3600
            for seen, published in rows
        ]

    # --- linha de base por site ---

    def site_has_baseline(self, site_key: str) -> bool:
        """Se a linha de base do site já foi feita."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM site_baseline WHERE site_key = ?", (site_key,)
            ).fetchone()
        return row is not None

    def mark_baseline(self, site_key: str, run_id: int) -> None:
        """Marca a linha de base do site como feita (idempotente: a primeira rodada vence)."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO site_baseline (site_key, run_id) VALUES (?, ?)",
                (site_key, run_id),
            )

    # --- sitemaps ---

    def sitemap_lastmod(self, sitemap_url: str) -> datetime | None:
        """Último ``lastmod`` declarado que vimos para este sitemap, se houver."""
        with self._lock:
            row = self._conn.execute(
                "SELECT lastmod FROM sitemap_state WHERE sitemap_url = ?", (sitemap_url,)
            ).fetchone()
        return _from_text(row[0]) if row else None

    def record_sitemap(
        self, sitemap_url: str, site_key: str, lastmod: datetime | None, fetched_at: datetime
    ) -> None:
        """Registra que o sitemap foi lido, com o ``lastmod`` que o índice declarava."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sitemap_state (sitemap_url, site_key, lastmod, fetched_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(sitemap_url) DO UPDATE SET"
                " lastmod = excluded.lastmod, fetched_at = excluded.fetched_at",
                (sitemap_url, site_key, _to_text(lastmod), _to_text(fetched_at)),
            )

    # --- resultado por site e rodada ---

    def record_site_run(self, run_id: int, record: SiteRunRecord) -> None:
        """Grava (ou substitui) o resultado de um site em uma rodada."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO site_runs (run_id, site_key, status, new_urls,"
                " baseline_urls, backlog_urls, seen_urls, sitemaps_fetched, sitemaps_skipped,"
                " detail, known_urls, sitemaps_failed, filtered_urls, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    record.detail,
                    record.known_urls,
                    record.sitemaps_failed,
                    record.filtered_urls,
                    record.source,
                ),
            )

    def site_runs(self, run_id: int) -> list[SiteRunRecord]:
        """Resultados por site de uma rodada, em ordem alfabética."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT site_key, status, new_urls, baseline_urls, backlog_urls, seen_urls,"
                " sitemaps_fetched, sitemaps_skipped, detail, known_urls, sitemaps_failed,"
                " filtered_urls, source FROM site_runs"
                " WHERE run_id = ? ORDER BY site_key",
                (run_id,),
            ).fetchall()
        return [SiteRunRecord(*row) for row in rows]

    def all_site_runs(self) -> list[tuple[int, SiteRunRecord]]:
        """Resultados por site de todas as rodadas, por rodada e depois por site."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, site_key, status, new_urls, baseline_urls, backlog_urls,"
                " seen_urls, sitemaps_fetched, sitemaps_skipped, detail, known_urls,"
                " sitemaps_failed, filtered_urls, source FROM site_runs ORDER BY run_id, site_key"
            ).fetchall()
        return [(row[0], SiteRunRecord(*row[1:])) for row in rows]

    # --- enriquecimento: conteúdo, análise de contexto e evidência ---

    def start_enrich_run(
        self, started_at: datetime, *, model: str, prompt_version: str, evidence_mode: str
    ) -> int:
        """Registra o início de uma execução do enriquecimento e devolve o seu id."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO enrich_runs (started_at, model, prompt_version, evidence_mode)"
                " VALUES (?, ?, ?, ?)",
                (_to_text(started_at), model, prompt_version, evidence_mode),
            )
            return int(cursor.lastrowid or 0)

    def finish_enrich_run(
        self, enrich_run_id: int, finished_at: datetime, totals: EnrichTotals
    ) -> None:
        """Grava o fim de uma execução do enriquecimento, com totais e consumo."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE enrich_runs SET finished_at = ?, urls = ?, fetched_ok = ?,"
                " fetched_blocked = ?, fetched_other = ?, analyzed = ?, findings = ?,"
                " evidence_reused = ?, evidence_brightdata = ?, evidence_failed = ?,"
                " bright_data_requests = ?, prompt_tokens = ?, completion_tokens = ?,"
                " cost_usd = ?, retries = ?, errors = ? WHERE id = ?",
                (
                    _to_text(finished_at),
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
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, started_at, finished_at, model, prompt_version, evidence_mode, urls,"
                " fetched_ok, fetched_blocked, fetched_other, analyzed, findings, evidence_reused,"
                " evidence_brightdata, evidence_failed, bright_data_requests, prompt_tokens,"
                " completion_tokens, cost_usd, retries, errors FROM enrich_runs ORDER BY id"
            ).fetchall()
        return [
            EnrichRun(
                id=row[0],
                started_at=datetime.fromisoformat(row[1]),
                finished_at=_from_text(row[2]),
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
            " SELECT u.url_hash, u.url, u.title, u.site_key, c.status, u.rowid AS pos,"
            " ROW_NUMBER() OVER (PARTITION BY u.site_key ORDER BY u.rowid) AS turn"
            " FROM urls_seen u"
            " LEFT JOIN url_content c ON c.url_hash = u.url_hash"
            " LEFT JOIN url_analysis a ON a.url_hash = u.url_hash"
            " WHERE u.kind = ? AND a.url_hash IS NULL"
            ") ORDER BY turn, pos"
        )
        params: tuple[object, ...] = (UrlKind.NEW.value,)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [EnrichPending(*row) for row in rows]

    def fetch_attempts(self, url_hash: str) -> int:
        """Quantas vezes tentamos baixar a URL."""
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts FROM url_content WHERE url_hash = ?", (url_hash,)
            ).fetchone()
        return int(row[0]) if row else 0

    def record_content(self, record: ContentRecord, *, now: datetime) -> None:
        """Grava (ou atualiza) o resultado do download e da extração; conta a tentativa."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO url_content (url_hash, status, http_status, final_url, title,"
                " description, lede, text_chars, published_at, language, error, attempts,"
                " fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)"
                " ON CONFLICT(url_hash) DO UPDATE SET status = excluded.status,"
                " http_status = excluded.http_status, final_url = excluded.final_url,"
                " title = excluded.title, description = excluded.description,"
                " lede = excluded.lede, text_chars = excluded.text_chars,"
                " published_at = excluded.published_at, language = excluded.language,"
                " error = excluded.error, attempts = url_content.attempts + 1,"
                " fetched_at = excluded.fetched_at",
                (
                    record.url_hash,
                    record.status,
                    record.http_status,
                    record.final_url,
                    record.title,
                    record.description,
                    record.lede,
                    record.text_chars,
                    _to_text(record.published_at),
                    record.language,
                    record.error,
                    _to_text(now),
                ),
            )

    def get_content(self, url_hash: str) -> ContentRecord | None:
        """Conteúdo extraído de uma URL, se já houve download."""
        with self._lock:
            row = self._conn.execute(
                "SELECT url_hash, status, http_status, final_url, title, description, lede,"
                " text_chars, published_at, language, error FROM url_content WHERE url_hash = ?",
                (url_hash,),
            ).fetchone()
        if row is None:
            return None
        return ContentRecord(
            url_hash=row[0],
            status=row[1],
            http_status=row[2],
            final_url=row[3],
            title=row[4],
            description=row[5],
            lede=row[6],
            text_chars=row[7],
            published_at=_from_text(row[8]),
            language=row[9],
            error=row[10],
        )

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
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO url_analysis (url_hash, finding, themes, entities,"
                " adverse_fact, summary, confidence, basis, model, prompt_version, enrich_run_id,"
                " analyzed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.url_hash,
                    int(record.finding),
                    ",".join(record.themes),
                    json.dumps(record.entities, ensure_ascii=False),
                    record.adverse_fact,
                    record.summary,
                    record.confidence,
                    record.basis,
                    model,
                    prompt_version,
                    enrich_run_id,
                    _to_text(now),
                ),
            )

    def get_analysis(self, url_hash: str) -> AnalysisRecord | None:
        """Análise de contexto de uma URL, se existir."""
        with self._lock:
            row = self._conn.execute(
                "SELECT url_hash, finding, themes, entities, adverse_fact, summary, confidence,"
                " basis, model, prompt_version FROM url_analysis WHERE url_hash = ?",
                (url_hash,),
            ).fetchone()
        if row is None:
            return None
        return AnalysisRecord(
            url_hash=row[0],
            finding=bool(row[1]),
            themes=tuple(theme for theme in row[2].split(",") if theme),
            entities=tuple(json.loads(row[3])),
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
            " FROM url_analysis a JOIN urls_seen u ON u.url_hash = a.url_hash"
            " LEFT JOIN url_evidence e ON e.url_hash = a.url_hash"
            f" WHERE {MEETS_CRITERIA_SQL} AND (e.url_hash IS NULL OR"
            " (e.status != 'captured' AND e.attempts < ?)) ORDER BY a.analyzed_at, a.url_hash"
        )
        params: tuple[object, ...] = (max_attempts,)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            FindingUrl(
                url_hash=row[0],
                url=row[1],
                title=row[2],
                site_key=row[3],
                entity_names=tuple(
                    str(entity["name"])
                    for entity in json.loads(row[4])
                    if entity.get("name")
                    and (not row[5] or is_adverse_role(str(entity.get("role", ""))))
                ),
                adverse=bool(row[5]),
            )
            for row in rows
        ]

    def count_pending_evidence(self, *, max_attempts: int = 3) -> int:
        """Quantas matérias esperam evidência (contagem direta, sem juntar a tabela grande)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM url_analysis a LEFT JOIN url_evidence e"
                " ON e.url_hash = a.url_hash"
                f" WHERE {MEETS_CRITERIA_SQL} AND (e.url_hash IS NULL OR"
                " (e.status != 'captured' AND e.attempts < ?))",
                (max_attempts,),
            ).fetchone()
        return int(row[0])

    def count_pending_enrichment(self) -> int:
        """Quantas URLs ``new`` ainda não têm análise de contexto."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM urls_seen u LEFT JOIN url_analysis a"
                " ON a.url_hash = u.url_hash WHERE u.kind = ? AND a.url_hash IS NULL",
                (UrlKind.NEW.value,),
            ).fetchone()
        return int(row[0])

    def record_evidence(self, record: EvidenceRecord, *, now: datetime) -> None:
        """Grava (ou atualiza) o estado da evidência; cada chamada conta uma tentativa."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO url_evidence (url_hash, status, provider, path, sha256, size_bytes,"
                " content_type, http_status, final_url, rejected_reasons, error, attempts,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)"
                " ON CONFLICT(url_hash) DO UPDATE SET status = excluded.status,"
                " provider = excluded.provider, path = excluded.path, sha256 = excluded.sha256,"
                " size_bytes = excluded.size_bytes, content_type = excluded.content_type,"
                " http_status = excluded.http_status, final_url = excluded.final_url,"
                " rejected_reasons = excluded.rejected_reasons, error = excluded.error,"
                " attempts = url_evidence.attempts + 1, updated_at = excluded.updated_at",
                (
                    record.url_hash,
                    record.status,
                    record.provider,
                    record.path,
                    record.sha256,
                    record.size_bytes,
                    record.content_type,
                    record.http_status,
                    record.final_url,
                    ",".join(record.rejected_reasons),
                    record.error,
                    _to_text(now),
                ),
            )

    def get_evidence(self, url_hash: str) -> EvidenceRecord | None:
        """Estado da evidência de uma URL, se houve tentativa."""
        with self._lock:
            row = self._conn.execute(
                "SELECT url_hash, status, provider, path, sha256, size_bytes, content_type,"
                " http_status, final_url, rejected_reasons, error, attempts FROM url_evidence"
                " WHERE url_hash = ?",
                (url_hash,),
            ).fetchone()
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
            rejected_reasons=tuple(reason for reason in row[9].split(",") if reason),
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
        sql = (
            "SELECT u.url_hash, u.url, u.site_key, COALESCE(c.title, u.title), c.lede,"
            " a.themes, a.entities, a.adverse_fact, e.provider FROM url_analysis a"
            " JOIN urls_seen u ON u.url_hash = a.url_hash"
            " LEFT JOIN url_content c ON c.url_hash = a.url_hash"
            " LEFT JOIN url_evidence e ON e.url_hash = a.url_hash"
            f" WHERE {condition} ORDER BY u.rowid"
        )
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [
            LabelCandidate(
                url_hash=row[0],
                group=group,
                url=row[1],
                site_key=row[2],
                headline=row[3],
                excerpt=row[4],
                themes=tuple(theme for theme in (row[5] or "").split(",") if theme),
                entity_names=tuple(
                    str(entity["name"]) for entity in json.loads(row[6]) if entity.get("name")
                ),
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
        with self._lock:
            rows = self._conn.execute(
                "SELECT rejected_reasons FROM url_evidence WHERE rejected_reasons <> ''"
            ).fetchall()
        counts: dict[str, int] = {}
        for (joined,) in rows:
            for reason in joined.split(","):
                counts[reason] = counts.get(reason, 0) + 1
        return counts

    def recent_findings(self, limit: int) -> list[RecentFinding]:
        """Últimos achados da análise de contexto, do mais recente ao mais antigo."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT a.analyzed_at, u.url, u.site_key, COALESCE(c.title, u.title), a.themes,"
                " a.entities, a.adverse_fact, a.summary, a.confidence, a.basis, e.status,"
                " e.provider, e.path FROM url_analysis a"
                " JOIN urls_seen u ON u.url_hash = a.url_hash"
                " LEFT JOIN url_content c ON c.url_hash = a.url_hash"
                " LEFT JOIN url_evidence e ON e.url_hash = a.url_hash"
                " WHERE a.finding = 1 ORDER BY a.analyzed_at DESC, u.rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            RecentFinding(
                analyzed_at=datetime.fromisoformat(row[0]),
                url=row[1],
                site_key=row[2],
                headline=row[3],
                themes=tuple(theme for theme in row[4].split(",") if theme),
                adverse_entities=tuple(
                    str(entity["name"])
                    for entity in json.loads(row[5])
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
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO corpus_runs (started_at) VALUES (?)", (_to_text(started_at),)
            )
            return int(cursor.lastrowid or 0)

    def finish_corpus_run(
        self, corpus_run_id: int, finished_at: datetime, totals: CorpusTotals
    ) -> None:
        """Grava o fim de uma execução da gravação no corpus."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE corpus_runs SET finished_at = ?, candidates = ?, created = ?, existing = ?,"
                " evidence_updated = ?, entities = ?, keywords = ?, failed = ?,"
                " rewritten = ? WHERE id = ?",
                (
                    _to_text(finished_at),
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
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, started_at, finished_at, candidates, created, existing,"
                " evidence_updated, entities, keywords, failed, rewritten"
                " FROM corpus_runs ORDER BY id"
            ).fetchall()
        return [
            CorpusRun(
                id=row[0],
                started_at=datetime.fromisoformat(row[1]),
                finished_at=_from_text(row[2]),
                totals=CorpusTotals(*row[3:]),
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
            " c.lede, c.published_at, c.language, a.themes, a.entities, a.adverse_fact, a.summary,"
            " a.confidence, a.basis, a.model, e.status, e.path, COALESCE(e.attempts, 0),"
            " e.provider, a.finding FROM url_analysis a JOIN urls_seen u"
            " ON u.url_hash = a.url_hash"
            " LEFT JOIN url_content c ON c.url_hash = a.url_hash"
            " LEFT JOIN url_evidence e ON e.url_hash = a.url_hash"
            " LEFT JOIN corpus_writes w ON w.url_hash = a.url_hash"
            f" WHERE {MEETS_CRITERIA_SQL}"
            " AND (w.url_hash IS NULL OR w.evidence_state != COALESCE(e.status, 'pending'))"
            " ORDER BY a.analyzed_at, a.url_hash"
        )
        params: list[object] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            CorpusCandidate(
                url_hash=row[0],
                url=row[1],
                site_key=row[2],
                title=row[3],
                description=row[4],
                lede=row[5],
                published_at=_from_text(row[6]),
                language=row[7],
                themes=tuple(theme for theme in row[8].split(",") if theme),
                entities=tuple(json.loads(row[9])),
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
            for row in rows
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
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO corpus_writes (url_hash, article_id, created, evidence_state,"
                " written_at, corpus_run_id) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(url_hash) DO UPDATE SET article_id = excluded.article_id,"
                " evidence_state = excluded.evidence_state, written_at = excluded.written_at,"
                " corpus_run_id = excluded.corpus_run_id,"
                " created = MAX(corpus_writes.created, excluded.created)",
                (
                    url_hash,
                    article_id,
                    int(created),
                    evidence_state,
                    _to_text(now),
                    corpus_run_id,
                ),
            )

    def corpus_written_hashes(self) -> list[str]:
        """Hashes já gravados no corpus por este sistema."""
        with self._lock:
            rows = self._conn.execute("SELECT url_hash FROM corpus_writes").fetchall()
        return [row[0] for row in rows]

    def forget_corpus_writes(self, url_hashes: Sequence[str]) -> None:
        """Esquece gravações (ex.: sumiram do corpus), para o achado ser gravado de novo."""
        with self._lock, self._conn:
            for start in range(0, len(url_hashes), _CHUNK):
                chunk = list(url_hashes[start : start + _CHUNK])
                marks = ",".join("?" * len(chunk))
                self._conn.execute(f"DELETE FROM corpus_writes WHERE url_hash IN ({marks})", chunk)

    def corpus_write_counts(self) -> dict[str, int]:
        """Quantos achados foram gravados (``written``), criados por nós (``created``)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(created), 0) FROM corpus_writes"
            ).fetchone()
        return {"written": int(row[0]), "created": int(row[1])}

    def enrichment_counts(self) -> dict[str, Any]:
        """Contagens para o painel: conteúdo, análises, evidências e pendências."""
        with self._lock:
            content = self._conn.execute(
                "SELECT status, COUNT(*) FROM url_content GROUP BY status"
            ).fetchall()
            analysis = self._conn.execute(
                "SELECT COALESCE(SUM(finding = 1), 0), COALESCE(SUM(finding = 0), 0),"
                " COALESCE(SUM(basis = 'headline'), 0),"
                f" COALESCE(SUM({MEETS_CRITERIA_SQL}), 0) FROM url_analysis a"
            ).fetchone()
            evidence = self._conn.execute(
                "SELECT status || ':' || COALESCE(provider, '-'), COUNT(*) FROM url_evidence"
                " GROUP BY 1"
            ).fetchall()
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

    # --- fallback ---

    def fallback_hosts(self) -> frozenset[str]:
        """Hosts que já nos bloquearam e devem ir direto ao fallback."""
        with self._lock:
            rows = self._conn.execute("SELECT host FROM fallback_hosts").fetchall()
        return frozenset(row[0] for row in rows)

    def add_fallback_hosts(self, hosts: Iterable[str], now: datetime) -> None:
        """Marca hosts como dependentes do fallback (idempotente)."""
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO fallback_hosts (host, first_marked_at) VALUES (?, ?)",
                [(host, _to_text(now)) for host in hosts],
            )
