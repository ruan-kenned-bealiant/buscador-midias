"""Interface de linha de comando do buscador."""

from __future__ import annotations

import argparse
import csv
import fcntl
import os
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

import httpx
import psycopg

from buscador_midias.analysis.context import DEFAULT_BATCH_SIZE as ANALYSIS_BATCH_SIZE
from buscador_midias.analysis.context import ContextAnalyzer, OpenRouterContextAnalyzer
from buscador_midias.analysis.openrouter import (
    DEFAULT_BASE_URL as OPENROUTER_BASE_URL,
)
from buscador_midias.analysis.openrouter import (
    DEFAULT_MODEL,
    MissingOpenRouterKeyError,
    OpenRouterChat,
)
from buscador_midias.analysis.prompt import PROMPT_VERSION as ANALYSIS_PROMPT_VERSION
from buscador_midias.corpus.index import CorpusConfigError, CorpusIndex, PostgresCorpusIndex
from buscador_midias.corpus.sync import DEFAULT_BATCH_SIZE as CORPUS_BATCH_SIZE
from buscador_midias.corpus.sync import sync_corpus
from buscador_midias.corpus.writer import CorpusSchemaError, CorpusWriter, PostgresCorpusWriter
from buscador_midias.dashboard.server import make_server
from buscador_midias.discovery.explore import DEFAULT_MAX_SITEMAP_FETCHES, ProbeStatus
from buscador_midias.discovery.incremental import DEFAULT_FRESH_DAYS, RunResult, discover_all
from buscador_midias.discovery.probe import SiteProbe
from buscador_midias.discovery.report import probe_all, summarize, write_probe_report
from buscador_midias.discovery.sources import DiscoverySource, cascade_for
from buscador_midias.discovery.stats import build_report, format_report
from buscador_midias.discovery.url_filter import DEFAULT_OFF_TOPIC_SEGMENTS
from buscador_midias.domain.site import Site
from buscador_midias.domain.strategy import CandidateReport, ProbeState, Role, Verdict
from buscador_midias.enrich.pipeline import ArticleSource, enrich_pending
from buscador_midias.evidence.capture import EvidenceCapturer
from buscador_midias.evidence.s3_storage import StorageConfigError, make_evidence_store
from buscador_midias.evidence.storage import EvidenceStore
from buscador_midias.extraction.fetch import ArticleFetcher
from buscador_midias.labeling.export import KEY_FILE, export_batch
from buscador_midias.labeling.score import format_label_report, score_labels
from buscador_midias.labeling.server import make_label_server
from buscador_midias.net.fetcher import USER_AGENT, Fetcher, ThrottledFetcher
from buscador_midias.net.memoizing import MemoizingFetcher
from buscador_midias.net.stack import (
    FetchStack,
    MissingCredentialsError,
    build_fetcher,
    build_unlocker,
)
from buscador_midias.sites.loader import load_sites
from buscador_midias.state.migrate import MigrationError, migrate_sqlite
from buscador_midias.state.store import (
    CorpusTotals,
    EnrichTotals,
    SiteRunRecord,
    StateConfigError,
    StateSchemaError,
    StateStore,
)
from buscador_midias.strategy.discovery_plan import load_discovery_plan
from buscador_midias.strategy.export import build_tables, write_csv, write_xlsx
from buscador_midias.strategy.health import (
    Condition,
    DueReason,
    HealthFinding,
    HealthPolicy,
    adopt,
    diagnose,
    due_reason,
    next_state,
)
from buscador_midias.strategy.hints import load_hints
from buscador_midias.strategy.listing import DEFAULT_MAX_PAGES as DEFAULT_LISTING_PAGES
from buscador_midias.strategy.manual import ManualRulesBook, load_manual_rules
from buscador_midias.strategy.mapping import drain_site, map_site, sections_from_sources
from buscador_midias.strategy.mapping_budget import MappingBudget, MappingBudgetExceededError
from buscador_midias.strategy.mapping_coverage import (
    audit_sources,
    normalize_source_audits,
    plan_source_audit,
)
from buscador_midias.strategy.mapping_export import mapping_tables, write_mapping
from buscador_midias.strategy.mapping_priorities import priority_row, priority_table
from buscador_midias.strategy.probe import (
    DEFAULT_MAX_REQUESTS,
    DEFAULT_SECTION_REQUESTS,
    SiteStrategy,
    probe_site,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SITES = _REPO_ROOT / "data" / "sites.csv"
DEFAULT_HINTS = _REPO_ROOT / "data" / "discovery_hints.csv"
DEFAULT_MANUAL_HINTS = _REPO_ROOT / "data" / "hints_manuais.csv"
DEFAULT_MANUAL_RULES = _REPO_ROOT / "data" / "estrategias_manuais.csv"
DEFAULT_DISCOVERY_PLAN = _REPO_ROOT / "data" / "plano_descoberta.json"
# Rodadas lidas de cada fonte para diagnosticar (o histórico mostrado é menor).
HEALTH_ROUNDS_ANALYSED = 30
DEFAULT_EXPORT = _REPO_ROOT / "var" / "estrategias.xlsx"
DEFAULT_REPORT = _REPO_ROOT / "var" / "probe-report.csv"
DEFAULT_LOCK = _REPO_ROOT / "var" / "run.lock"
DEFAULT_SCHEDULE = _REPO_ROOT / "var" / "schedule.json"
DEFAULT_EVIDENCE_DIR = _REPO_ROOT / "var" / "evidence"
DEFAULT_LABELS = _REPO_ROOT / "var" / "labels"
ARTICLE_MAX_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class EnrichPorts:
    """Dependências do ``enrich`` que os testes substituem por fakes.

    Attributes:
        articles: Download direto de artigos (padrão: pilha educada com robots.txt).
        analyzer: Análise de contexto (padrão: OpenRouter com as variáveis do ambiente).
        bright_data: Fetcher do Bright Data para capturar evidência (padrão: as credenciais do
            ambiente, com teto de chamadas).
        evidence_store: Armazenamento das evidências (padrão: pasta local ``--evidence-dir``).
    """

    articles: ArticleSource | None = None
    analyzer: ContextAnalyzer | None = None
    bright_data: Fetcher | None = None
    evidence_store: EvidenceStore | None = None


_REQUEST_TIMEOUT_SECONDS = 20.0


def build_parser() -> argparse.ArgumentParser:
    """Monta o parser de argumentos, com um subcomando por operação.

    Returns:
        O parser configurado (``probe``, ``discover`` e ``report``).
    """
    parser = argparse.ArgumentParser(prog="buscador-midias", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    probe = commands.add_parser(
        "probe",
        help="Sonda robots.txt e sitemaps das fontes e gera o relatório de viabilidade.",
    )
    _add_site_args(probe)
    _add_network_args(probe)
    probe.add_argument("--out", type=Path, default=DEFAULT_REPORT, help="CSV do relatório.")

    discover = commands.add_parser(
        "discover",
        help=(
            "Rodada de descoberta incremental (sitemap, depois feed, depois Bright Data): grava no "
            "estado e lista o que é novo."
        ),
    )
    _add_site_args(discover)
    _add_network_args(discover)
    discover.add_argument(
        "--fresh-days",
        type=int,
        default=DEFAULT_FRESH_DAYS,
        help="Idade máxima, em dias, para uma URL inédita contar como nova (senão: backlog).",
    )
    discover.add_argument(
        "--max-sitemap-fetches",
        type=int,
        default=DEFAULT_MAX_SITEMAP_FETCHES,
        help="Sitemaps requisitados por mecanismo em cada rodada; zero desliga o teto.",
    )
    discover.add_argument(
        "--skip-off-topic",
        action="store_true",
        help=(
            "Descarta pela forma da URL as seções de temas sem interesse (esporte, culinária, "
            "horóscopo). "
            "Fica desligado por padrão: essas seções também trazem matérias de gestão e denúncia."
        ),
    )
    discover.add_argument(
        "--corpus",
        action="store_true",
        help=(
            "Consulta o corpus de Mídias IA (somente leitura) para marcar o que já é conhecido. "
            "Exige DB_MIDIAS_IA_HOST/PORT/DATABASE/USERNAME/PASSWORD no ambiente."
        ),
    )

    discover.add_argument(
        "--no-feed",
        action="store_true",
        help="Não usa o feed (RSS/Atom) como alternativa quando o sitemap não serve.",
    )
    discover.add_argument(
        "--no-strategy",
        action="store_true",
        help=(
            "Ignora a estratégia gravada por `probe-sources` e usa só a cascata de sempre "
            "(sitemap do robots.txt, feed, Bright Data)."
        ),
    )
    _add_discovery_plan_args(discover)

    mechanisms = commands.add_parser(
        "export-mechanisms", help="Exporta RSS/sitemaps e associações offline, sem HTTP."
    )
    mechanisms.add_argument("--sites", type=Path, default=DEFAULT_SITES)
    mechanisms.add_argument("--manual", type=Path, default=DEFAULT_MANUAL_RULES)
    mechanisms.add_argument("--out", type=Path, default=Path("var/mecanismos-busca"))

    dashboard = commands.add_parser(
        "dashboard",
        help="Painel web local (somente leitura) para acompanhar o fluxo em tempo real.",
    )
    dashboard.add_argument(
        "--host",
        default="127.0.0.1",
        help="Endereço de escuta. O painel não tem autenticação: mantenha em 127.0.0.1.",
    )
    dashboard.add_argument("--port", type=int, default=8765, help="Porta (0 escolhe uma livre).")
    dashboard.add_argument("--sites", type=Path, default=DEFAULT_SITES, help="Catálogo do mapa.")
    dashboard.add_argument(
        "--schedule",
        type=Path,
        default=DEFAULT_SCHEDULE,
        help="JSON com as execuções previstas para a agenda do painel (opcional).",
    )

    enrich = commands.add_parser(
        "enrich",
        help=(
            "Para as URLs novas: baixa o texto, analisa o contexto (OpenRouter), confere o "
            "critério e captura a evidência das que atendem (CONSOME CRÉDITOS)."
        ),
    )
    enrich.add_argument("--model", default=DEFAULT_MODEL, help="Modelo do OpenRouter.")
    enrich.add_argument(
        "--evidence-mode",
        choices=["auto", "brightdata"],
        default="auto",
        help=(
            "auto: reaproveita o HTML do download direto se passar na avaliação de confiabilidade "
            "e, senão, usa o Bright Data; brightdata: só o Bright Data."
        ),
    )
    enrich.add_argument(
        "--evidence-dir",
        type=Path,
        default=DEFAULT_EVIDENCE_DIR,
        help="Pasta das evidências no armazenamento local (midias-ia/evidencias/<url_hash>.html).",
    )
    enrich.add_argument(
        "--evidence-store",
        choices=["auto", "local", "s3"],
        default="auto",
        help=(
            "Onde gravar as evidências. auto: pelo MIDIAS_IA_STORAGE_DISK do ambiente (local ou "
            "s3; padrão local); s3 usa AWS_BUCKET, AWS_ENDPOINT, AWS_DEFAULT_REGION e as chaves."
        ),
    )
    enrich.add_argument(
        "--batch-size", type=int, default=ANALYSIS_BATCH_SIZE, help="Matérias por chamada ao LLM."
    )
    enrich.add_argument("--workers", type=int, default=4, help="Operações simultâneas.")
    enrich.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Segundos mínimos entre downloads do mesmo site.",
    )
    enrich.add_argument(
        "--chunk-size", type=int, default=60, help="URLs por bloco (limita a memória)."
    )
    enrich.add_argument("--limit", type=int, default=None, help="Máximo de URLs nesta execução.")
    enrich.add_argument(
        "--bright-data-max-requests",
        type=int,
        default=100,
        help="Teto de chamadas pagas ao Bright Data nesta execução (controle de custo).",
    )

    corpus_sync = commands.add_parser(
        "corpus-sync",
        help="Grava no corpus de Mídias IA (Postgres) os achados com contexto e evidência.",
    )
    corpus_sync.add_argument("--sites", type=Path, default=DEFAULT_SITES, help="CSV de fontes.")
    corpus_sync.add_argument(
        "--batch-size", type=int, default=CORPUS_BATCH_SIZE, help="Artigos por lote."
    )
    corpus_sync.add_argument("--limit", type=int, default=None, help="Máximo de achados.")
    corpus_sync.add_argument(
        "--dry-run", action="store_true", help="Só mostra o que seria gravado; não grava nada."
    )

    labels = commands.add_parser(
        "labels",
        help="Rotulagem humana às cegas para medir a precisão da análise de contexto.",
    )
    labels_commands = labels.add_subparsers(dest="labels_command", required=True)
    labels_export = labels_commands.add_parser(
        "export", help="Gera um lote novo de rotulagem (CSV cego + chave com o veredito do modelo)."
    )
    labels_score = labels_commands.add_parser(
        "score", help="Lê os lotes preenchidos e calcula precisão e recall estimados."
    )
    labels_serve = labels_commands.add_parser(
        "serve",
        help="Tela simples para marcar se cada notícia entra no corpus (sim, não, incerto).",
    )
    for parser_ in (labels_export, labels_score, labels_serve):
        parser_.add_argument(
            "--out-dir", type=Path, default=DEFAULT_LABELS, help="Pasta dos lotes e da chave."
        )
    labels_serve.add_argument(
        "--host", default="127.0.0.1", help="Endereço (a tela grava: deixe em 127.0.0.1)."
    )
    labels_serve.add_argument("--port", type=int, default=8766, help="Porta da tela.")
    labels_export.add_argument(
        "--per-group", type=int, default=70, help="URLs por grupo (com entidade, sem entidade)."
    )
    labels_export.add_argument("--seed", type=int, default=1, help="Semente do sorteio.")

    run = commands.add_parser(
        "run",
        help=(
            "O fluxo todo, em ordem: descobrir notícias novas, analisar o contexto, conferir o "
            "critério, capturar a evidência e gravar no corpus (CONSOME CRÉDITOS). É o comando "
            "que o agendador chama."
        ),
    )
    _add_site_args(run)
    run.add_argument(
        "--lock-file", type=Path, default=DEFAULT_LOCK, help="Trava contra duas execuções."
    )
    run.add_argument("--workers", type=int, default=8, help="Fontes lidas em paralelo.")
    run.add_argument(
        "--enrich-workers", type=int, default=8, help="Downloads e chamadas ao LLM em paralelo."
    )
    run.add_argument(
        "--interval", type=float, default=1.0, help="Segundos mínimos entre pedidos ao mesmo host."
    )
    run.add_argument(
        "--fresh-days", type=int, default=DEFAULT_FRESH_DAYS, help="Janela de frescor."
    )
    run.add_argument(
        "--max-sitemap-fetches", type=int, default=DEFAULT_MAX_SITEMAP_FETCHES, help="Por site."
    )
    run.add_argument("--skip-off-topic", action="store_true", help="Veja `discover --help`.")
    run.add_argument("--no-feed", action="store_true", help="Não usa o feed como alternativa.")
    run.add_argument("--no-strategy", action="store_true", help="Veja `discover --help`.")
    _add_discovery_plan_args(run)
    run.add_argument(
        "--no-bright-data",
        action="store_true",
        help="Não usa o Bright Data (pago) em nenhuma etapa.",
    )
    run.add_argument(
        "--discovery-bright-data-max",
        type=int,
        default=60,
        help="Teto de chamadas pagas na descoberta (sitemaps de sites que bloqueiam).",
    )
    run.add_argument(
        "--enrich-bright-data-max",
        type=int,
        default=100,
        help="Teto de chamadas pagas na captura de evidência.",
    )
    run.add_argument("--model", default=DEFAULT_MODEL, help="Modelo do OpenRouter.")
    run.add_argument(
        "--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR, help="Evidências no disco local."
    )
    run.add_argument(
        "--evidence-store", choices=["auto", "local", "s3"], default="auto", help="Onde gravar."
    )
    run.add_argument("--progress-every", type=int, default=100, help="Progresso a cada N fontes.")

    probe_sources = commands.add_parser(
        "probe-sources",
        help=(
            "Sonda como obter as matérias novas de cada fonte (sitemap de notícias, sitemap, "
            "feed), escolhe a melhor estratégia e a grava no estado."
        ),
    )
    _add_site_args(probe_sources)
    probe_sources.add_argument("--workers", type=int, default=8, help="Threads em paralelo.")
    probe_sources.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Segundos mínimos entre requisições ao mesmo host.",
    )
    probe_sources.add_argument(
        "--hints",
        type=Path,
        nargs="+",
        default=[DEFAULT_HINTS, DEFAULT_MANUAL_HINTS],
        help=(
            "CSVs de dicas de endereços por fonte: o levantamento e as dicas manuais (feeds que "
            "só quem conhece o site sabe, ex.: os do g1). Só um palpite: vale o que o endereço "
            "publica."
        ),
    )
    probe_sources.add_argument(
        "--manual",
        type=Path,
        default=DEFAULT_MANUAL_RULES,
        help=(
            "CSV de correções à mão (dominio, endereco, acao, motivo): 'excluir' tira um endereço "
            "(ou um prefixo, com * no fim) da sondagem; 'forçar' manda usá-lo como principal."
        ),
    )
    probe_sources.add_argument(
        "--progress-every",
        type=int,
        default=50,
        help="Imprime o progresso (em stderr) a cada N fontes concluídas.",
    )
    probe_sources.add_argument(
        "--max-requests",
        type=int,
        default=DEFAULT_MAX_REQUESTS,
        help="Orçamento de requisições por fonte (inclui o robots.txt e a home).",
    )
    probe_sources.add_argument(
        "--due",
        action="store_true",
        help=(
            "Sonda só as fontes na vez: nunca sondadas, estratégia velha (7 dias), ou com falha, "
            "silêncio ou encolhimento nas rodadas (com espera crescente para não insistir)."
        ),
    )
    probe_sources.add_argument(
        "--listing-pages",
        type=int,
        default=DEFAULT_LISTING_PAGES,
        help=(
            "Teto de páginas da varredura de listagens (home e seções), por fonte, que roda para "
            "toda fonte e tem orçamento próprio; 0 a desliga. Mais páginas, mais seções no mapa."
        ),
    )
    probe_sources.add_argument(
        "--section-requests",
        type=int,
        default=DEFAULT_SECTION_REQUESTS,
        help=(
            "Orçamento, por fonte, da busca do feed ou sitemap de cada seção (os que as páginas "
            "apontam e os que um modelo de URL sugere); 0 a desliga."
        ),
    )
    probe_sources.add_argument(
        "--dry-run", action="store_true", help="Mostra o resultado sem gravar no estado."
    )
    export = commands.add_parser(
        "export-strategies",
        help="Exporta para planilha (.xlsx ou .csv) a estratégia gravada das fontes do catálogo.",
    )
    _add_site_args(export)
    export.add_argument(
        "--hints",
        type=Path,
        nargs="+",
        default=[DEFAULT_HINTS, DEFAULT_MANUAL_HINTS],
        help="CSVs do levantamento, só para as colunas de referência.",
    )
    export.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_EXPORT,
        help="Arquivo de saída: .xlsx (três abas) ou .csv (um arquivo por aba).",
    )
    health = commands.add_parser(
        "strategy-health",
        help=(
            "Lista as fontes cujo padrão de leitura quebrou (falha nas rodadas, ou a sondagem "
            "nova não achou nada) ou pede atenção (silêncio, lista que encolheu)."
        ),
    )
    _add_site_args(health)
    health.add_argument(
        "--rounds", type=int, default=6, help="Quantas rodadas mostrar no histórico de cada fonte."
    )
    health.add_argument("--out", type=Path, default=None, help="Também grava um CSV.")
    commands.add_parser("report", help="Estatísticas das rodadas de descoberta.")
    migrate = commands.add_parser(
        "migrate-sqlite",
        help="Copia um estado antigo em SQLite (var/state.db) para o Postgres (destino vazio).",
    )
    migrate.add_argument("sqlite", type=Path, help="Arquivo SQLite do estado antigo.")
    mapping = commands.add_parser(
        "map-sites", help="Mapeia seções em lotes retomáveis, sem coletar notícias."
    )
    _add_site_args(mapping)
    mapping.add_argument("--pages-per-site", type=int, default=50,
                         help="Páginas processadas por fonte neste lote (padrão 50).")
    mapping.add_argument("--drain", action="store_true",
                         help="Repete lotes até esgotar a fila ou não conseguir avançar.")
    mapping.add_argument("--max-site-requests", type=int, default=200,
                         help="Teto total de chamadas HTTP por fonte, entre todos os lotes.")
    mapping.add_argument("--max-site-seconds", type=float, default=180,
                         help="Tempo de exploração por fonte (padrão 180 segundos).")
    mapping.add_argument("--max-run-seconds", type=float, default=1800,
                         help="Prazo global da exploração HTTP; exportação fica à parte.")
    mapping.add_argument("--workers", type=int, default=4, help="Fontes em paralelo.")
    mapping.add_argument("--interval", type=float, default=1.0,
                         help="Intervalo mínimo por host, em segundos.")
    mapping.add_argument("--retry-failed", action="store_true",
                         help="Tenta novamente as páginas com falha ou proibidas.")
    mapping.add_argument("--report-only", action="store_true",
                         help="Só exporta o inventário existente, sem rede ou gravação no banco.")
    mapping.add_argument("--priorities-only", action="store_true",
                         help="Com --report-only, exporta somente prioridades de cobertura.")
    mapping.add_argument("--refresh-pages", action="store_true",
                         help="Relê também páginas concluídas, atualizando suas amostras.")
    mapping.add_argument("--validate-sources", action="store_true",
                         help="Confere feeds/sitemaps contra as amostras das seções.")
    mapping.add_argument("--sources-only", action="store_true",
                         help="Só confere mecanismos das páginas já mapeadas.")
    mapping.add_argument("--source-requests", type=int, default=20,
                         help="Leituras por fonte; inclui robots e filhos (retentativas à parte).")
    mapping.add_argument("--hints", type=Path, nargs='+',
                         default=[DEFAULT_HINTS, DEFAULT_MANUAL_HINTS], help="CSVs de dicas.")
    mapping.add_argument("--manual", type=Path, default=DEFAULT_MANUAL_RULES,
                         help="Correções manuais; exclusões valem também na auditoria.")
    mapping.add_argument("--out", type=Path, default=_REPO_ROOT / "var" / "mapa-sites.xlsx",
                         help="Relatório .xlsx ou .csv (um CSV por aba).")
    return parser


def _add_discovery_plan_args(parser: argparse.ArgumentParser) -> None:
    """Opções do conjunto de mecanismos, comuns a discover e run."""
    parser.add_argument("--discovery-plan", type=Path, default=None,
                        help="Plano JSON de principal/complementos; não grava escolhas no banco.")
    parser.add_argument("--max-complements", type=int, default=3,
                        help="Complementos por fonte; zero mantém a cascata sem união.")
    parser.add_argument("--max-source-requests", type=int, default=40,
                        help="Teto de chamadas gratuitas por fonte; zero desliga o teto.")
    parser.add_argument("--source-timeout", type=float, default=120,
                        help="Prazo em segundos por fonte; zero desliga o prazo total.")
    parser.add_argument("--manual", type=Path, default=DEFAULT_MANUAL_RULES,
                        help="Regras manuais; exclusões são aplicadas durante a leitura.")


def _add_site_args(parser: argparse.ArgumentParser) -> None:
    """Opções de seleção de fontes, comuns aos comandos que varrem sites."""
    parser.add_argument("--sites", type=Path, default=DEFAULT_SITES, help="CSV de fontes.")
    parser.add_argument("--limit", type=int, default=None, help="Usa só as N primeiras fontes.")
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="CHAVE",
        default=None,
        help="Usa só estas fontes, pela chave (ex.: g1.globo.com/globonews).",
    )
    parser.add_argument(
        "--only-file",
        type=Path,
        default=None,
        help="Arquivo com uma chave por linha (# comenta); soma-se a --only.",
    )


def _add_network_args(parser: argparse.ArgumentParser) -> None:
    """Opções de rede, comuns aos comandos que varrem sites."""
    parser.add_argument("--workers", type=int, default=8, help="Threads em paralelo.")
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Segundos mínimos entre requisições ao mesmo host.",
    )
    parser.add_argument(
        "--bright-data",
        action="store_true",
        help=(
            "Usa o Bright Data Web Unlocker para os sites que bloqueiam (CONSOME CRÉDITOS). "
            "Exige BRIGHTDATA_API_KEY e BRIGHTDATA_ZONE_UNLOCKER no ambiente."
        ),
    )
    parser.add_argument(
        "--bright-data-max-requests",
        type=int,
        default=500,
        help="Teto de chamadas ao Bright Data nesta execução (controle de custo).",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=50,
        help="Imprime o progresso (em stderr) a cada N fontes concluídas.",
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    fetcher: Fetcher | None = None,
    now: datetime | None = None,
    corpus: CorpusIndex | None = None,
    client: httpx.Client | None = None,
    paid_fetcher: Fetcher | None = None,
    serve: Callable[[ThreadingHTTPServer], None] | None = None,
    enrich_ports: EnrichPorts | None = None,
    corpus_writer: CorpusWriter | None = None,
) -> int:
    """Executa o comando pedido.

    Args:
        argv: Argumentos (``None`` usa ``sys.argv``).
        fetcher: Fetcher a usar; se ausente, monta o real. Existe para que os testes injetem um
            fake e nunca acessem a internet.
        now: Instante de referência; se ausente, usa o horário atual em UTC.
        corpus: Índice do corpus a usar em ``discover`` (os testes injetam um fake).
        client: Cliente httpx a usar quando ``fetcher`` não for dado (os testes injetam um com
            transporte simulado).
        paid_fetcher: Fetcher pago (Bright Data) da descoberta; se ausente e ``--bright-data`` foi
            pedido, monta o real com as credenciais do ambiente (os testes injetam um fake).
        serve: Como o painel atende as requisições (padrão: ``serve_forever``); os testes injetam
            uma função que não bloqueia.
        enrich_ports: Dependências do ``enrich``; os testes injetam fakes.
        corpus_writer: Gravador do corpus usado por ``corpus-sync`` (os testes injetam um fake).

    Returns:
        Código de saída do processo (0 em sucesso, 2 para erro de uso ou configuração).
    """
    args = build_parser().parse_args(argv)
    if args.command == "report":
        return _report()
    if args.command == "labels":
        return _labels(args)
    if args.command == "corpus-sync":
        return _corpus_sync(args, corpus_writer, now)
    if args.command == "enrich":
        return _enrich(args, enrich_ports or EnrichPorts(), now)
    if args.command == "export-mechanisms":
        from buscador_midias.strategy.mechanism_inventory import export_mechanisms

        store = _open_state(read_only=True)
        if store is None:
            return 2
        try:
            summary = export_mechanisms(
                store, load_sites(args.sites).sites, load_manual_rules(args.manual), args.out,
            )
        finally:
            store.close()
        print(f"Inventário exportado em {args.out}: "
              f"{summary['functional_plan_sources']} fontes no plano funcional")
        return 0
    if args.command == "dashboard":
        return _dashboard(args, serve)

    if args.command == "migrate-sqlite":
        return _migrate(args.sqlite)
    if args.command == "export-strategies":
        return _export_strategies(args)
    if args.command == "strategy-health":
        return _strategy_health(args)
    if args.command == "run":
        return _run(
            args,
            fetcher=fetcher,
            now=now,
            corpus=corpus,
            client=client,
            paid_fetcher=paid_fetcher,
            enrich_ports=enrich_ports,
            corpus_writer=corpus_writer,
        )

    sites = _select_sites(args)
    if sites is None:
        return 2
    reference = now or datetime.now(UTC)
    if args.command == "map-sites":
        return _map_sites(args, sites, fetcher, client, reference, live_clock=now is None)
    if args.command == "probe":
        return _probe(args, sites, fetcher, client, reference)
    if args.command == "probe-sources":
        return _probe_sources(args, sites, fetcher, client, reference)
    return _discover(args, sites, fetcher, client, corpus, now, paid_fetcher)


def _run(
    args: argparse.Namespace,
    *,
    fetcher: Fetcher | None,
    now: datetime | None,
    corpus: CorpusIndex | None,
    client: httpx.Client | None,
    paid_fetcher: Fetcher | None,
    enrich_ports: EnrichPorts | None,
    corpus_writer: CorpusWriter | None,
) -> int:
    """Subcomando ``run``: as etapas do fluxo em sequência, cada uma retomável pelo estado.

    Cada etapa é o subcomando de mesmo nome (``discover``, ``enrich``, ``corpus-sync``), então o
    que vale para elas vale aqui. Uma etapa que falha não impede as seguintes (o que ficou
    pendente é retomado na próxima execução); o código de saída é 1 se alguma falhou. Duas
    execuções ao mesmo tempo não se atropelam: a segunda sai com código 3.
    """
    args.lock_file.parent.mkdir(parents=True, exist_ok=True)
    with args.lock_file.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("já existe uma execução em andamento; esta foi dispensada", file=sys.stderr)
            return 3
        selected = _select_sites(args)
        if selected is None:
            return 2
        try:
            _discovery_configuration(args, selected)
        except ValueError as error:
            print(error, file=sys.stderr)
            return 2
        site_args = ["--sites", str(args.sites)]
        for flag, value in (("--limit", args.limit), ("--only-file", args.only_file)):
            if value is not None:
                site_args += [flag, str(value)]
        if args.only:
            site_args += ["--only", *args.only]
        discover = [
            "discover",
            *site_args,
            "--corpus",
            "--workers", str(args.workers),
            "--interval", str(args.interval),
            "--fresh-days", str(args.fresh_days),
            "--max-sitemap-fetches", str(args.max_sitemap_fetches),
            "--bright-data-max-requests", str(args.discovery_bright_data_max),
            "--progress-every", str(args.progress_every),
            "--max-complements", str(args.max_complements),
            "--max-source-requests", str(args.max_source_requests),
            "--source-timeout", str(args.source_timeout),
            "--manual", str(args.manual),
        ]  # fmt: skip
        enrich = [
            "enrich",
            "--model", args.model,
            "--workers", str(args.enrich_workers),
            "--interval", str(args.interval),
            "--evidence-dir", str(args.evidence_dir),
            "--evidence-store", args.evidence_store,
            "--bright-data-max-requests", str(args.enrich_bright_data_max),
        ]  # fmt: skip
        sync = ["corpus-sync", "--sites", str(args.sites)]
        if not args.no_bright_data:
            discover.append("--bright-data")
        discover += ["--skip-off-topic"] * args.skip_off_topic + ["--no-feed"] * args.no_feed
        discover += ["--no-strategy"] * args.no_strategy
        if args.discovery_plan is not None:
            discover += ["--discovery-plan", str(args.discovery_plan)]
        strategy = [
            "probe-sources",
            *site_args,
            "--manual", str(args.manual),
            "--due",
            "--workers", str(args.workers),
            "--interval", str(args.interval),
            "--progress-every", str(args.progress_every),
        ]  # fmt: skip
        steps = (
            *(
                (("ESTRATÉGIA", "a revisão das estratégias", strategy),)
                if not args.no_strategy
                else ()
            ),
            ("DESCOBERTA", "a descoberta", discover),
            ("ANÁLISE, CRITÉRIO E EVIDÊNCIA", "a análise", enrich),
            ("CORPUS", "a gravação no corpus", sync),
        )
        worst = 0
        for title, label, argv in steps:
            print(f"== {title} {(now or datetime.now().astimezone()):%F %T}", flush=True)
            try:
                code = main(
                    argv,
                    fetcher=fetcher,
                    now=now,
                    corpus=corpus,
                    client=client,
                    paid_fetcher=paid_fetcher,
                    enrich_ports=enrich_ports,
                    corpus_writer=corpus_writer,
                )
            except Exception as error:  # noqa: BLE001 - uma etapa não derruba as seguintes
                print(f"{type(error).__name__}: {error}", file=sys.stderr)
                code = 1
            if code != 0:
                print(f"ERRO: {label} terminou com código {code}", flush=True)
                worst = 1
        return worst


def _select_sites(args: argparse.Namespace) -> list[Site] | None:
    """Aplica ``--only``, ``--only-file`` e ``--limit`` ao catálogo; ``None`` em caso de erro."""
    sites = list(load_sites(args.sites).sites)
    wanted: set[str] = set(args.only or [])
    if args.only_file is not None:
        if not args.only_file.is_file():
            print(f"arquivo de fontes não encontrado: {args.only_file}", file=sys.stderr)
            return None
        wanted.update(_read_keys(args.only_file))

    if args.only is not None or args.only_file is not None:
        unknown = sorted(wanted - {site.key for site in sites})
        if unknown:
            print(f"fontes desconhecidas: {', '.join(unknown)}", file=sys.stderr)
            return None
        sites = [site for site in sites if site.key in wanted]
    return sites[: args.limit] if args.limit is not None else sites


def _read_keys(path: Path) -> list[str]:
    """Chaves de fontes de um arquivo: uma por linha; ignora linhas vazias e comentários."""
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _open_stack(
    args: argparse.Namespace,
    fetcher: Fetcher | None,
    client: httpx.Client | None,
    exit_stack: ExitStack,
    known_hosts: frozenset[str] = frozenset(),
) -> FetchStack:
    """Pilha de fetchers: a injetada (testes) ou a real, com ``Bright Data`` opcional.

    Raises:
        MissingCredentialsError: Se ``--bright-data`` for pedido sem credenciais no ambiente.
    """
    if fetcher is not None:
        return FetchStack(fetcher, fallback=None)
    http = _open_http(client, exit_stack)
    return build_fetcher(
        client=http,
        interval=args.interval,
        bright_data=args.bright_data,
        env=os.environ,
        max_fallback_requests=args.bright_data_max_requests,
        known_fallback_hosts=known_hosts,
    )


def _open_http(client: httpx.Client | None, exit_stack: ExitStack) -> httpx.Client:
    """O cliente HTTP injetado (testes) ou um novo, fechado junto com ``exit_stack``."""
    return client or exit_stack.enter_context(
        httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=True)
    )


def _probe(
    args: argparse.Namespace,
    sites: Sequence[Site],
    fetcher: Fetcher | None,
    client: httpx.Client | None,
    now: datetime,
) -> int:
    """Sonda as fontes, grava o relatório e imprime o resumo."""
    with ExitStack() as exit_stack:
        try:
            stack = _open_stack(args, fetcher, client, exit_stack)
        except MissingCredentialsError as error:
            print(error, file=sys.stderr)
            return 2

        statuses: Counter[str] = Counter()

        def report_progress(done: int, total: int, probe: SiteProbe) -> None:
            statuses[probe.status.value] += 1
            _print_progress(done, total, statuses, args.progress_every)

        probes = probe_all(
            sites,
            stack.fetcher,
            now=now,
            user_agent=USER_AGENT,
            workers=args.workers,
            on_progress=report_progress,
        )
        write_probe_report(probes, args.out)

        print(f"relatório: {args.out}")
        for name, count in summarize(probes).items():
            print(f"{name:>20}: {count}")
        if stack.fallback is not None:
            print(f"{'bright_data_requests':>20}: {stack.fallback.fallback_requests}")
            print(f"{'bright_data_hosts':>20}: {len(stack.fallback.fallback_hosts)}")
    return 0


def _enrich(args: argparse.Namespace, ports: EnrichPorts, now: datetime | None) -> int:
    """Subcomando ``enrich``: texto, análise de contexto e evidência das URLs mantidas."""
    reference = now or datetime.now(UTC)
    with ExitStack() as exit_stack:
        http = exit_stack.enter_context(
            httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=True)
        )
        try:
            analyzer = ports.analyzer or _open_analyzer(args, http)
            unlocker = ports.bright_data or _open_unlocker(args, http)
            evidence_store = ports.evidence_store or make_evidence_store(
                os.environ,
                args.evidence_dir,
                kind=None if args.evidence_store == "auto" else args.evidence_store,
            )
        except (MissingOpenRouterKeyError, MissingCredentialsError, StorageConfigError) as error:
            print(error, file=sys.stderr)
            return 2
        if unlocker is None:
            print(
                "aviso: sem Bright Data; achados cujo HTML direto não for confiável ficam sem "
                "evidência até haver credenciais",
                file=sys.stderr,
            )
        store = _open_state()
        if store is None:
            return 2
        exit_stack.callback(store.close)
        articles = ports.articles or ArticleFetcher(
            build_fetcher(
                client=http,
                interval=args.interval,
                bright_data=False,
                env=os.environ,
                max_fallback_requests=0,
                max_bytes=ARTICLE_MAX_BYTES,
            ).fetcher,
            user_agent=USER_AGENT,
        )
        capturer = EvidenceCapturer(
            evidence_store,
            bright_data=unlocker,
            reuse=args.evidence_mode == "auto",
            now=lambda: reference,
        )
        paid = getattr(unlocker, "requests_made", None)
        summary = enrich_pending(
            store,
            articles=articles,
            analyzer=analyzer,
            capturer=capturer,
            now=reference,
            model=args.model,
            prompt_version=ANALYSIS_PROMPT_VERSION,
            evidence_mode=args.evidence_mode,
            batch_size=args.batch_size,
            workers=args.workers,
            chunk_size=args.chunk_size,
            limit=args.limit,
            analyzer_usage=(lambda: analyzer.usage) if hasattr(analyzer, "usage") else None,
            bright_data_requests=(lambda: unlocker.requests_made) if paid is not None else None,  # type: ignore[union-attr]
        )
        _print_enrich(summary.totals)
    return 0


def _open_analyzer(args: argparse.Namespace, http: httpx.Client) -> ContextAnalyzer:
    """Monta o analisador do OpenRouter com as variáveis do ambiente.

    Raises:
        MissingOpenRouterKeyError: Se ``OPENROUTER_API_KEY`` não estiver definida.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise MissingOpenRouterKeyError("defina OPENROUTER_API_KEY para a análise de contexto")
    chat = OpenRouterChat(
        http,
        api_key=api_key,
        model=args.model,
        base_url=os.environ.get("OPENROUTER_BASE_URL") or OPENROUTER_BASE_URL,
    )
    return OpenRouterContextAnalyzer(chat)


def _open_unlocker(args: argparse.Namespace, http: httpx.Client) -> Fetcher | None:
    """Bright Data com teto de chamadas; sem credenciais só é erro no modo ``brightdata``.

    Raises:
        MissingCredentialsError: No modo ``brightdata`` sem credenciais.
    """
    try:
        return build_unlocker(http, os.environ, max_requests=args.bright_data_max_requests)
    except MissingCredentialsError:
        if args.evidence_mode == "brightdata":
            raise
        return None


def _print_enrich(totals: EnrichTotals) -> None:
    """Resumo de uma execução do enriquecimento."""
    cost = f"US$ {totals.cost_usd:.4f}" if totals.cost_usd is not None else "não informado"
    rows = {
        "enriquecimento: URLs": totals.urls,
        "baixadas ok": totals.fetched_ok,
        "bloqueadas": totals.fetched_blocked,
        "outras falhas": totals.fetched_other,
        "analisadas": totals.analyzed,
        "achados": totals.findings,
        "evidências reaproveitadas": totals.evidence_reused,
        "evidências Bright Data": totals.evidence_brightdata,
        "evidências com falha": totals.evidence_failed,
        "Bright Data (chamadas)": totals.bright_data_requests,
    }
    for name, count in rows.items():
        print(f"{name:>26}: {count}")
    print(
        f"{'custo da análise':>26}: {cost} "
        f"({totals.prompt_tokens} tokens de entrada, {totals.completion_tokens} de saída)"
    )


def _corpus_sync(
    args: argparse.Namespace, writer: CorpusWriter | None, now: datetime | None
) -> int:
    """Subcomando ``corpus-sync``: grava no corpus os achados pendentes e imprime o resumo."""
    if writer is None:
        try:
            writer = PostgresCorpusWriter.from_env(os.environ)
        except CorpusConfigError as error:
            print(error, file=sys.stderr)
            return 2
        except Exception as error:  # noqa: BLE001 - qualquer falha de conexão encerra com mensagem
            print(f"não consegui conectar ao corpus: {type(error).__name__}", file=sys.stderr)
            return 2
    store = _open_state()
    if store is None:
        return 2
    try:
        check = getattr(writer, "check_schema", None)
        if callable(check):
            try:
                check()
            except CorpusSchemaError as error:
                print(error, file=sys.stderr)
                return 2
        sites = {site.key: site for site in load_sites(args.sites).sites}
        summary = sync_corpus(
            store,
            writer,
            sites=sites,
            now=now or datetime.now(UTC),
            batch_size=args.batch_size,
            limit=args.limit,
            dry_run=args.dry_run,
        )
        _print_corpus(summary.totals, dry_run=args.dry_run)
        return 0
    finally:
        store.close()
        close = getattr(writer, "close", None)
        if callable(close):
            close()


def _print_corpus(totals: CorpusTotals, *, dry_run: bool) -> None:
    """Resumo de uma execução da gravação no corpus."""
    if dry_run:
        print("simulação: nada foi gravado")
    rows = {
        "corpus: candidatos": totals.candidates,
        "criados": totals.created,
        "já existiam": totals.existing,
        "evidências atualizadas": totals.evidence_updated,
        "entidades": totals.entities,
        "temas": totals.keywords,
        "falhas": totals.failed,
        "refeitos (sumiram)": totals.rewritten,
    }
    for name, count in rows.items():
        print(f"{name:>24}: {count}")


def _labels(args: argparse.Namespace) -> int:
    """Subcomando ``labels``: exporta um lote cego, pontua os preenchidos ou abre a tela."""
    if args.labels_command == "serve":
        return _labels_serve(args)
    store = _open_state(read_only=True)
    if store is None:
        return 2
    try:
        if args.labels_command == "export":
            batch = export_batch(store, args.out_dir, per_group=args.per_group, seed=args.seed)
            total = sum(batch.counts.values())
            print(f"lote {batch.number}: {total} URLs {batch.counts}")
            print(f"arquivo para preencher: {batch.path}")
            print(
                "preencha a coluna 'veredito' com: sim, nao ou incerto "
                "(a notícia entra no corpus?) "
                "(opcional: 'entidades_nomeadas' e 'obs'); depois rode `labels score`."
            )
            return 0
        if not (args.out_dir / KEY_FILE).is_file():
            print(f"nenhum lote em {args.out_dir}: rode `labels export` antes", file=sys.stderr)
            return 2
        print(format_label_report(score_labels(store, args.out_dir)))
        return 0
    finally:
        store.close()


def _labels_serve(args: argparse.Namespace) -> int:
    """Sobe a tela de rotulagem; ``Ctrl+C`` encerra. Não lê o estado, só os CSVs dos lotes."""
    if not any(args.out_dir.glob("rotulagem-*.csv")):
        print(f"nenhum lote em {args.out_dir}: rode `labels export` antes", file=sys.stderr)
        return 2
    try:
        server = make_label_server(args.out_dir, args.host, args.port)
    except OSError as error:
        print(f"não consegui abrir a porta {args.port}: {error.strerror}", file=sys.stderr)
        return 2
    port = server.server_address[1]
    print(f"tela de rotulagem em http://{args.host}:{port}  (Ctrl+C para encerrar)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("encerrado")
    finally:
        server.server_close()
    return 0


def _dashboard(
    args: argparse.Namespace, serve: Callable[[ThreadingHTTPServer], None] | None
) -> int:
    """Sobe o painel local e bloqueia até Ctrl+C."""
    probe = _open_state(read_only=True)
    if probe is None:
        return 2
    probe.close()
    server = make_server(
        lambda: StateStore(read_only=True), args.host, args.port, schedule_path=args.schedule,
        sites_path=args.sites,
    )
    port = server.server_address[1]
    print(f"painel em http://{args.host}:{port}  (Ctrl+C para sair)", flush=True)
    try:
        (serve or (lambda srv: srv.serve_forever()))(server)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _discovery_configuration(
    args: argparse.Namespace, sites: Sequence[Site],
) -> tuple[ManualRulesBook, dict[str, list[CandidateReport]]]:
    """Valida limites, plano e regras antes de iniciar HTTP ou demais etapas do run."""
    if (args.max_complements < 0 or args.max_source_requests < 0
            or args.max_sitemap_fetches < 0
            or not 0 <= args.source_timeout < float("inf")
            or (args.discovery_plan is not None and args.no_strategy)):
        raise ValueError("limites inválidos ou --discovery-plan incompatível com --no-strategy")
    manual = load_manual_rules(args.manual)
    plan_path = args.discovery_plan
    if (plan_path is None and not args.no_strategy and DEFAULT_DISCOVERY_PLAN.is_file()
            and args.sites.resolve() == DEFAULT_SITES.resolve()):
        plan_path = DEFAULT_DISCOVERY_PLAN
    plan = load_discovery_plan(
        plan_path, {s.key: s for s in load_sites(args.sites).sites},
    ) if plan_path is not None else {}
    for site in sites:
        forced = manual.for_site(site).force
        if site.key in plan and forced and plan[site.key][0].url not in forced:
            if args.discovery_plan is None:
                del plan[site.key]
                continue
            raise ValueError(f"plano conflita com principal forçado à mão: {site.key}")
    return manual, plan


def _discover(
    args: argparse.Namespace,
    sites: Sequence[Site],
    fetcher: Fetcher | None,
    client: httpx.Client | None,
    corpus: CorpusIndex | None,
    now: datetime | None,
    paid_fetcher: Fetcher | None,
) -> int:
    """Roda uma rodada de descoberta incremental e imprime o resumo."""
    try:
        manual, plan = _discovery_configuration(args, sites)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    with ExitStack() as exit_stack:
        if corpus is None and args.corpus:
            try:
                owned = PostgresCorpusIndex.from_env(os.environ)
            except CorpusConfigError as error:
                print(error, file=sys.stderr)
                return 2
            except Exception as error:  # noqa: BLE001 - corpus fora do ar não pode parar a coleta
                # Sem o corpus só se perde a marca "já conhecida": o que ele já tem é reconhecido
                # na gravação (idempotente). Parar a coleta perderia notícias que saem do feed.
                print(
                    f"aviso: corpus indisponível ({type(error).__name__}); seguindo sem marcar "
                    "o que já é conhecido",
                    file=sys.stderr,
                )
            else:
                exit_stack.callback(owned.close)
                corpus = owned

        store = _open_state()
        if store is None:
            return 2
        exit_stack.callback(store.close)
        try:
            fetchers = _open_discovery_fetchers(args, fetcher, paid_fetcher, client, exit_stack)
        except MissingCredentialsError as error:
            print(error, file=sys.stderr)
            return 2
        known_blocked = store.fallback_hosts()
        strategies = _strategies_by_site(store) if not args.no_strategy else {}
        for key, reports in plan.items():
            addresses = {r.url for r in reports}
            backups = [r for r in strategies.get(key, ()) if r.role is Role.BACKUP
                       and r.url not in addresses]
            strategies[key] = [*reports, *backups]
        if args.max_complements and any(
            r.role is Role.COMPLEMENT for site in sites for r in strategies.get(site.key, ())
        ):
            try:
                store.require_combined_discovery()
            except StateSchemaError as error:
                print(error, file=sys.stderr)
                return 2

        def sources_for(site: Site) -> list[DiscoverySource]:
            return cascade_for(
                site,
                free=fetchers.free,
                paid=fetchers.paid,
                user_agent=USER_AGENT,
                known_blocked=site.host in known_blocked,
                max_sitemap_fetches=args.max_sitemap_fetches,
                use_feed=not args.no_feed,
                strategies=strategies.get(site.key, ()),
                now=now,
                max_complements=args.max_complements,
                max_source_requests=args.max_source_requests,
                source_timeout=args.source_timeout,
                exclude=manual.for_site(site).exclude,
            )

        statuses: Counter[str] = Counter()

        def report_progress(done: int, total: int, record: SiteRunRecord) -> None:
            statuses[record.status] += 1
            _print_progress(done, total, statuses, args.progress_every)

        result = discover_all(
            sites,
            sources_for,
            store,
            workers=args.workers,
            fresh_days=args.fresh_days,
            corpus=corpus,
            off_topic_segments=DEFAULT_OFF_TOPIC_SEGMENTS if args.skip_off_topic else frozenset(),
            clock=(lambda: now) if now is not None else None,
            on_progress=report_progress,
        )
        paid_requests = fetchers.paid_requests()
        paid_sites = sum(1 for record in result.records if record.source == "sitemap_bd")
        if fetchers.paid is not None:
            store.record_run_usage(
                result.run_id, bright_data_requests=paid_requests, bright_data_hosts=paid_sites
            )
        _print_run(result)
        if fetchers.paid is not None:
            print(f"{'bright_data_requests':>20}: {paid_requests}")
            print(f"{'bright_data_hosts':>20}: {paid_sites}")
    return 0


def _strategies_by_site(store: StateStore) -> dict[str, list[CandidateReport]]:
    """Principal, reserva e complementos escolhidos por fonte."""
    grouped: dict[str, list[CandidateReport]] = {}
    for site_key, chosen in store.discovery_strategies():
        grouped.setdefault(site_key, []).append(chosen)
    return grouped


@dataclass(frozen=True, slots=True)
class DiscoveryFetchers:
    """Os fetchers da cascata de descoberta.

    Attributes:
        free: Acesso gratuito (sitemap e feed).
        paid: Acesso pago (Bright Data), ou ``None`` se não está habilitado.
        paid_requests: Quantas chamadas pagas foram feitas até agora.
    """

    free: Fetcher
    paid: Fetcher | None
    paid_requests: Callable[[], int]


def _open_discovery_fetchers(
    args: argparse.Namespace,
    fetcher: Fetcher | None,
    paid_fetcher: Fetcher | None,
    client: httpx.Client | None,
    exit_stack: ExitStack,
) -> DiscoveryFetchers:
    """Fetchers da cascata: o gratuito e, se pedido, o pago (Bright Data, com teto de chamadas).

    Raises:
        MissingCredentialsError: Se ``--bright-data`` for pedido sem credenciais no ambiente.
    """

    def counter(paid: Fetcher | None) -> Callable[[], int]:
        return lambda: int(getattr(paid, "requests_made", 0))

    if fetcher is not None:
        paid = paid_fetcher if args.bright_data else None
        return DiscoveryFetchers(fetcher, paid, counter(paid))
    http = _open_http(client, exit_stack)
    free = build_fetcher(
        client=http,
        interval=args.interval,
        bright_data=False,
        env=os.environ,
        max_fallback_requests=0,
    ).fetcher
    if not args.bright_data:
        return DiscoveryFetchers(free, None, counter(None))
    if paid_fetcher is not None:
        return DiscoveryFetchers(free, paid_fetcher, counter(paid_fetcher))
    limited = build_unlocker(http, os.environ, max_requests=args.bright_data_max_requests)
    paid = ThrottledFetcher(limited, min_interval=args.interval)
    return DiscoveryFetchers(free, paid, counter(limited))


def _map_sites(
    args: argparse.Namespace,
    sites: Sequence[Site],
    fetcher: Fetcher | None,
    client: httpx.Client | None,
    now: datetime,
    *,
    live_clock: bool = False,
) -> int:
    """Mapeia a estrutura com checkpoints e exporta o inventário inteiro."""
    if (args.pages_per_site < 1 or args.workers < 1 or args.interval < 0
            or args.source_requests < 1 or args.max_site_requests < 1
            or not 0 < args.max_site_seconds < float("inf")
            or not 0 < args.max_run_seconds < float("inf")):
        print("orçamentos/workers precisam ser positivos e intervalo não negativo", file=sys.stderr)
        return 2
    if args.out.suffix.lower() not in (".xlsx", ".csv"):
        print("a saída precisa terminar em .xlsx ou .csv", file=sys.stderr)
        return 2
    if args.priorities_only and not args.report_only:
        print("--priorities-only exige --report-only", file=sys.stderr)
        return 2
    failed = False
    validating = args.validate_sources or args.sources_only
    clock: Callable[[], datetime] = (lambda: datetime.now(UTC)) if live_clock else (lambda: now)
    try:
        manual = load_manual_rules(args.manual)
        hints = load_hints(args.hints)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    with ExitStack() as stack:
        store = _open_state(read_only=args.report_only)
        if store is None:
            return 2
        stack.callback(store.close)
        try:
            # Confere o DDL antes da primeira requisição.
            pages = {site.key: store.mapping_pages(site.key) for site in sites}
            candidates = store.strategy_rows()
            checks = {site.key: store.mapping_sources(site.key) for site in sites}
            for key, saved_checks in checks.items():
                normalized = normalize_source_audits(saved_checks)
                if not args.report_only:
                    corrected = [a for a, b in zip(normalized, saved_checks, strict=True) if a != b]
                    if corrected:
                        store.save_mapping_sources(key, corrected)
                checks[key] = normalized
            previous = {key: r for key, r in store.chosen_strategies() if r.role is Role.PRIMARY}
            if args.priorities_only:
                write_mapping(priority_table([
                    priority_row(site, pages[site.key], checks[site.key], now=now)
                    for site in sites
                ]), args.out)
                return 0
            if not args.report_only:
                free = fetcher or build_fetcher(
                    client=_open_http(client, stack), interval=args.interval,
                    bright_data=False, env=os.environ, max_fallback_requests=0,
                ).fetcher
                if not args.drain:
                    free = MemoizingFetcher(free, hosts=_shared_hosts(sites))
                deadline = monotonic() + args.max_run_seconds

                def visit_pass(
                    site: Site, *, first: bool, suspended_pages: dict[str, str],
                    suspended_sources: dict[str, str], bounded: MappingBudget,
                ) -> None:
                    if not args.sources_only:
                        if args.drain:
                            result = drain_site(
                                site, bounded, store, user_agent=USER_AGENT,
                                clock=clock, batch_pages=args.pages_per_site,
                                retry_failed=args.retry_failed and first,
                                refresh_pages=args.refresh_pages and first,
                                suspended_origins=suspended_pages,
                                on_batch=lambda batch: print(
                                    f"{site.key}: lote lidas={batch.visited} "
                                    f"pendentes={batch.pending}", flush=True,
                                ),
                            )
                        else:
                            result = map_site(
                                site, bounded, store, user_agent=USER_AGENT, now=now,
                                max_pages=args.pages_per_site, retry_failed=args.retry_failed,
                                refresh_pages=args.refresh_pages,
                                suspended_origins=suspended_pages,
                            )
                        print(f"{site.key}: seções={result.sections} lidas={result.visited} "
                              f"pendentes={result.pending} falhas={result.issues} {result.detail}",
                              flush=True)
                    if validating:
                        known = store.mapping_pages(site.key)
                        attempted: set[str] = set()
                        while True:
                            audit_time = clock() if args.drain else now
                            queue = plan_source_audit(
                                site, known, store.mapping_sources(site.key),
                                [r for key, r, _ in candidates if key == site.key], now=audit_time,
                                hints=hints.for_site(site), forced=manual.for_site(site).force,
                                retry_failed=args.retry_failed and first,
                            )
                            queue = [
                                url for url in queue if url not in attempted
                                and urlsplit(url).netloc not in suspended_sources
                            ]
                            if not queue:
                                break
                            audited = audit_sources(
                                site, bounded, queue, user_agent=USER_AGENT, now=audit_time,
                                max_requests=args.source_requests,
                                exclude=manual.for_site(site).exclude,
                                suspended_origins=suspended_sources,
                                on_source=lambda source: store.save_mapping_sources(
                                    site.key, [source]
                                ),
                            )
                            print(f'{site.key}: mecanismos conferidos={len(audited)}', flush=True)
                            attempted.update(source.url for source in audited)
                            if not args.drain or not audited:
                                break

                def visit_site(site: Site, bounded: MappingBudget) -> None:
                    first = True
                    suspended_pages: dict[str, str] = {}
                    suspended_sources: dict[str, str] = {}
                    while True:
                        visit_pass(
                            site, first=first, suspended_pages=suspended_pages,
                            suspended_sources=suspended_sources, bounded=bounded,
                        )
                        first = False
                        if not args.drain or args.sources_only:
                            break
                        additional = sections_from_sources(
                            site, store.mapping_pages(site.key), store.mapping_sources(site.key)
                        )
                        if not additional:
                            break
                        store.save_mapping_pages(site.key, additional)
                        print(f"{site.key}: candidatas em mecanismos={len(additional)}", flush=True)

                def visit(site: Site) -> None:
                    bounded = MappingBudget(
                        free, max_requests=args.max_site_requests,
                        max_seconds=args.max_site_seconds, deadline=deadline, clock=monotonic,
                    )
                    try:
                        visit_site(site, bounded)
                    except MappingBudgetExceededError as error:
                        roots = [p for p in store.mapping_pages(site.key) if p.kind == "root"]
                        if roots:
                            root = roots[0]
                            detail = "; ".join(filter(None, (root.detail, str(error))))
                            store.save_mapping_pages(site.key, [replace(root, detail=detail)])
                        print(f"{site.key}: {error}", flush=True)

                with ThreadPoolExecutor(max_workers=args.workers) as pool:
                    jobs = {pool.submit(visit, site): site.key for site in sites}
                    for future in as_completed(jobs):
                        try:
                            future.result()
                        except Exception as error:  # noqa: BLE001 - preserva os demais mapas
                            failed = True
                            print(f"{jobs[future]}: {type(error).__name__}; checkpoint preservado",
                                  file=sys.stderr)
                pages = {site.key: store.mapping_pages(site.key) for site in sites}
                checks = {site.key: store.mapping_sources(site.key) for site in sites}
            write_mapping(mapping_tables(sites, pages, previous, candidates, checks), args.out)
        except (StateSchemaError, RuntimeError, OSError, psycopg.Error) as error:
            message = str(error) if isinstance(error, (StateSchemaError, RuntimeError)) else (
                type(error).__name__
            )
            print(message, file=sys.stderr)
            return 2
    print(f"Inventário de {len(sites)} fontes em {args.out}; cobertura de notícias não validada.")
    return 1 if failed else 0


def _probe_sources(
    args: argparse.Namespace,
    sites: Sequence[Site],
    fetcher: Fetcher | None,
    client: httpx.Client | None,
    now: datetime,
) -> int:
    """Subcomando ``probe-sources``: sonda as fontes e grava a estratégia de cada uma.

    Com ``--due`` só entram as fontes na vez (veja ``strategy/health.py``). Só grava a estratégia da
    fonte em que a sondagem achou um principal: uma falha passageira (bloqueio, rede) não apaga a
    que já servia. O estado de reavaliação é gravado sempre, para a espera crescer sem achar nada.
    Uma fonte com defeito não derruba as outras.
    """
    hints = load_hints(args.hints)
    try:
        manual = load_manual_rules(args.manual)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    policy = HealthPolicy()
    with ExitStack() as exit_stack:
        store = _open_state(read_only=args.dry_run)
        if store is None:
            return 2
        exit_stack.callback(store.close)
        states = store.probe_states()
        for key, probed_at in store.strategy_probe_times().items():
            states.setdefault(key, adopt(key, probed_at, policy))  # gravadas antes do controle
        previous_primary = {
            key: chosen.url
            for key, chosen in store.chosen_strategies()
            if chosen.role is Role.PRIMARY
        }
        reasons: dict[str, DueReason | None] = {}
        selected = list(sites)
        if args.due:
            rounds = store.recent_rounds(per_site=30)
            reasons = {
                site.key: due_reason(
                    states.get(site.key), rounds.get(site.key, []), now=now, policy=policy
                )
                for site in sites
            }
            selected = [site for site in sites if reasons[site.key] is not None]
        free = (
            fetcher
            or build_fetcher(
                client=_open_http(client, exit_stack),
                interval=args.interval,
                bright_data=False,
                env=os.environ,
                max_fallback_requests=0,
            ).fetcher
        )
        free = MemoizingFetcher(free, hosts=_shared_hosts(selected))

        def probe(site: Site) -> SiteStrategy:
            try:
                return probe_site(
                    site,
                    free,
                    user_agent=USER_AGENT,
                    now=now,
                    hints=hints.for_site(site),
                    exclude=manual.for_site(site).exclude,
                    forced=manual.for_site(site).force,
                    listing_pages=args.listing_pages,
                    section_requests=args.section_requests,
                    max_requests=args.max_requests,
                )
            except Exception as error:  # noqa: BLE001 - uma fonte não derruba as outras
                return SiteStrategy(site.key, ProbeStatus.ERROR, f"{type(error).__name__}", (), 0)

        results: list[SiteStrategy] = []
        statuses: Counter[str] = Counter()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = as_completed(pool.submit(probe, s) for s in selected)
            for done, future in enumerate(futures, 1):
                result = future.result()
                results.append(result)
                statuses["ok" if result.primary else result.status.value] += 1
                if not args.dry_run:
                    _record_probe(store, result, previous_primary, states, reasons, now, policy)
                _print_progress(done, len(selected), statuses, args.progress_every)
    _print_strategies(results, reasons, up_to_date=len(sites) - len(selected) if args.due else None)
    return 0


def _shared_hosts(sites: Sequence[Site]) -> frozenset[str]:
    """Hosts com mais de uma fonte (o g1 tem dez): a sondagem baixa cada arquivo deles uma vez."""
    counts = Counter(site.host for site in sites)
    return frozenset(host for host, count in counts.items() if count > 1)


def _record_probe(
    store: StateStore,
    result: SiteStrategy,
    previous_primary: dict[str, str],
    states: dict[str, ProbeState],
    reasons: dict[str, DueReason | None],
    now: datetime,
    policy: HealthPolicy,
) -> None:
    """Grava a estratégia (se achou principal) e o estado de reavaliação da fonte."""
    primary = result.primary
    if primary is not None:
        store.replace_site_strategies(result.site_key, result.reports, now)
    reason = reasons.get(result.site_key)
    state = next_state(
        states.get(result.site_key),
        result.site_key,
        now=now,
        found=primary is not None,
        changed=primary is not None and primary.url != previous_primary.get(result.site_key),
        reason=reason,
        policy=policy,
    )
    why = "" if primary is not None else f"{result.status.value}: {result.detail}"
    state = replace(state, detail=why)
    store.record_probe_state(state, reason.value if reason else "")


def _export_strategies(args: argparse.Namespace) -> int:
    """Subcomando ``export-strategies``: planilha de conferência das estratégias gravadas."""
    suffix = args.out.suffix.lower()
    if suffix not in (".xlsx", ".csv"):
        print("a saída precisa terminar em .xlsx ou .csv", file=sys.stderr)
        return 2
    sites = _select_sites(args)
    if sites is None:
        return 2
    store = _open_state(read_only=True)
    if store is None:
        return 2
    try:
        keys = {site.key for site in sites}
        rows = [row for row in store.strategy_rows() if row[0] in keys]
        tables = build_tables(sites, rows, store.probe_states(), load_hints(args.hints))
    finally:
        store.close()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    try:
        (write_xlsx if suffix == ".xlsx" else write_csv)(tables, args.out)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 2
    probed = sum(1 for r in tables.summary.rows if r[5] != "não sondada")
    ok = sum(1 for r in tables.summary.rows if r[5] in ("ok", "lenta"))
    print(f"{len(sites)} fontes ({probed} sondadas, {ok} com estratégia) em {args.out}")
    return 0


def _strategy_health(args: argparse.Namespace) -> int:
    """Subcomando ``strategy-health``: o que quebrou e o que pede atenção, sem tocar em nada."""
    sites = _select_sites(args)
    if sites is None:
        return 2
    store = _open_state(read_only=True)
    if store is None:
        return 2
    try:
        states = store.probe_states()
        rounds = store.recent_rounds(per_site=HEALTH_ROUNDS_ANALYSED)
        with_strategy = set(store.strategy_probe_times())
        primaries = {
            key: chosen.url
            for key, chosen in store.chosen_strategies()
            if chosen.role is Role.PRIMARY
        }
    finally:
        store.close()
    if not states and not with_strategy:
        print("nenhuma fonte com padrão gravado: rode probe-sources primeiro")
        return 0
    policy = HealthPolicy()
    findings: list[tuple[Site, HealthFinding]] = []
    counts: Counter[str] = Counter()
    for site in sites:
        state = states.get(site.key)
        finding = diagnose(
            site.key,
            state,
            rounds.get(site.key, []),
            has_strategy=site.key in with_strategy,
            policy=policy,
        )
        if finding is not None:
            findings.append((site, finding))
            counts[finding.condition.value] += 1
        elif state is None:
            counts["nunca sondada"] += 1
        elif site.key in with_strategy:
            counts["em dia"] += 1
        else:
            counts["sem estratégia"] += 1
    for condition in (Condition.BROKEN, Condition.WATCH):
        group = [(s, f) for s, f in findings if f.condition is condition]
        print(f"{condition.value.upper()} ({len(group)})")
        for site, finding in group:
            recent = ", ".join(finding.history.split(", ")[: args.rounds])
            print(f"  {site.key}: {finding.symptom} [{recent}]")
    print(
        f"resumo: {counts['quebrou']} quebrou, {counts['atenção']} atenção, "
        f"{counts['em dia']} em dia, {counts['sem estratégia']} sem estratégia, "
        f"{counts['nunca sondada']} nunca sondadas"
    )
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["Chave", "Fonte", "Situação", "Sintoma", "Últimas rodadas", "Estratégia atual"]
            )
            for site, finding in findings:
                writer.writerow(
                    [
                        site.key,
                        site.name,
                        finding.condition.value,
                        finding.symptom,
                        finding.history,
                        primaries.get(site.key, ""),
                    ]
                )
    return 0


def _print_strategies(
    results: Sequence[SiteStrategy],
    reasons: dict[str, DueReason | None] | None = None,
    up_to_date: int | None = None,
) -> None:
    """Uma linha por fonte sondada (principal e reserva, ou o motivo de não haver) e o resumo."""
    for result in sorted(results, key=lambda r: r.site_key):
        primary, backup = result.primary, result.backup
        reason = (reasons or {}).get(result.site_key)
        label = f"{result.site_key:<36}" + (f" [{reason.value}]" if reason else "")
        if primary is None:
            print(f"{label} sem estratégia ({result.status.value}: {result.detail})")
            continue
        print(
            f"{label} {_describe(primary)}"
            + (f"  |  reserva: {_describe(backup)}" if backup else "")
        )
    chosen = [r.primary for r in results if r.primary is not None]
    kinds = Counter(c.kind.value for c in chosen if c.kind is not None)
    print(f"{'fontes':>20}: {len(results)}")
    print(f"{'com estratégia':>20}: {len(chosen)}")
    for kind, count in sorted(kinds.items()):
        print(f"{kind:>20}: {count}")
    print(f"{'sem estratégia':>20}: {len(results) - len(chosen)}")
    print(f"{'requisições':>20}: {sum(r.requests for r in results)}")
    if up_to_date is not None:
        print(f"{'em dia':>20}: {up_to_date}")


def _describe(report: CandidateReport) -> str:
    """Um candidato em uma frase: tipo, o que publica (recentes ou o veredito), nota e endereço."""
    kind = report.kind.value if report.kind else "?"
    recent = report.measure.recent if report.measure else 0
    newest = report.measure.newest if report.measure else None
    if report.verdict is Verdict.FRESH:
        what = f"{recent} recentes"
    elif report.verdict is Verdict.SLOW and newest is not None:
        what = f"lenta, última matéria em {newest:%Y-%m-%d}"
    else:
        what = report.verdict.value
    points = f"{report.score:.1f}" if 0 < report.score < 10 else f"{report.score:.0f}"
    return f"{kind} ({what}, nota {points}) {report.url}"


def _report() -> int:
    """Imprime as estatísticas do estado."""
    store = _open_state(read_only=True)
    if store is None:
        return 2
    try:
        print(format_report(build_report(store)))
    finally:
        store.close()
    return 0


def _migrate(path: Path) -> int:
    """Subcomando ``migrate-sqlite``: copia o estado antigo para o Postgres e imprime o resumo."""
    store = _open_state()
    if store is None:
        return 2
    try:
        counts = migrate_sqlite(path, store)
    except MigrationError as error:
        print(error, file=sys.stderr)
        return 2
    finally:
        store.close()
    for name, count in counts.items():
        print(f"{name:>20}: {count}")
    return 0


def _open_state(*, read_only: bool = False) -> StateStore | None:
    """Abre o estado no Postgres; em falha imprime o motivo (nunca a senha) e devolve ``None``."""
    try:
        return StateStore(read_only=read_only)
    except (StateConfigError, StateSchemaError) as error:
        print(error, file=sys.stderr)
    except psycopg.Error as error:
        print(f"não consegui conectar ao estado: {type(error).__name__}", file=sys.stderr)
    return None


def _print_progress(done: int, total: int, statuses: Counter[str], every: int) -> None:
    """Escreve o progresso em stderr a cada ``every`` sites e ao concluir."""
    if done % every == 0 or done == total:
        counts = " ".join(f"{name}={count}" for name, count in sorted(statuses.items()))
        print(f"{done}/{total}  {counts}", file=sys.stderr, flush=True)


def _print_run(result: RunResult) -> None:
    """Resumo de uma rodada de descoberta, somando todos os sites."""
    records = result.records
    ok = sum(1 for r in records if r.status == "ok")
    totals = {
        "sites": len(records),
        "ok": ok,
        "falhas": len(records) - ok,
        "novas": sum(r.new_urls for r in records),
        "linha de base": sum(r.baseline_urls for r in records),
        "backlog": sum(r.backlog_urls for r in records),
        "no corpus": sum(r.known_urls for r in records),
        "filtradas (forma)": sum(r.filtered_urls for r in records),
        "sitemaps lidos": sum(r.sitemaps_fetched for r in records),
        "sitemaps pulados": sum(r.sitemaps_skipped for r in records),
    }
    print(f"rodada {result.run_id}")
    for name, count in totals.items():
        print(f"{name:>20}: {count}")
    sources = Counter(r.source for r in records if r.source)
    print(
        f"{'fontes':>20}: sitemap {sources['sitemap']}, feed {sources['feed']}, "
        f"sitemap pago {sources['sitemap_bd']}, conjunto {sources['combined']}"
    )
