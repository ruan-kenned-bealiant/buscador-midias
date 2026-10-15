"""Interface de linha de comando do buscador."""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

from buscador_midias.analysis.context import DEFAULT_BATCH_SIZE as ANALYSIS_BATCH_SIZE
from buscador_midias.analysis.context import ContextAnalyzer, OpenRouterContextAnalyzer
from buscador_midias.analysis.prompt import PROMPT_VERSION as ANALYSIS_PROMPT_VERSION
from buscador_midias.corpus.index import CorpusConfigError, CorpusIndex, PostgresCorpusIndex
from buscador_midias.corpus.sync import DEFAULT_BATCH_SIZE as CORPUS_BATCH_SIZE
from buscador_midias.corpus.sync import sync_corpus
from buscador_midias.corpus.writer import CorpusSchemaError, CorpusWriter, PostgresCorpusWriter
from buscador_midias.dashboard.server import make_server
from buscador_midias.discovery.explore import DEFAULT_MAX_SITEMAP_FETCHES
from buscador_midias.discovery.incremental import DEFAULT_FRESH_DAYS, RunResult, discover_all
from buscador_midias.discovery.probe import SiteProbe
from buscador_midias.discovery.report import probe_all, summarize, write_probe_report
from buscador_midias.discovery.stats import build_report, format_report
from buscador_midias.domain.site import Site
from buscador_midias.enrich.pipeline import ArticleSource, enrich_pending
from buscador_midias.evidence.capture import EvidenceCapturer
from buscador_midias.evidence.s3_storage import StorageConfigError, make_evidence_store
from buscador_midias.evidence.storage import EvidenceStore
from buscador_midias.extraction.fetch import ArticleFetcher
from buscador_midias.labeling.export import KEY_FILE, export_batch
from buscador_midias.labeling.score import format_label_report, score_labels
from buscador_midias.labeling.server import make_label_server
from buscador_midias.net.fetcher import USER_AGENT, Fetcher
from buscador_midias.net.stack import (
    FetchStack,
    MissingCredentialsError,
    build_fetcher,
    build_unlocker,
)
from buscador_midias.sites.loader import load_sites
from buscador_midias.state.store import CorpusTotals, EnrichTotals, SiteRunRecord, StateStore
from buscador_midias.triage.l0 import DEFAULT_OFF_TOPIC_SEGMENTS
from buscador_midias.triage.l1 import L1Filter
from buscador_midias.triage.openrouter import (
    DEFAULT_BASE_URL as OPENROUTER_BASE_URL,
)
from buscador_midias.triage.openrouter import (
    DEFAULT_MODEL,
    MissingOpenRouterKeyError,
    OpenRouterChat,
    classifier_from_env,
)
from buscador_midias.triage.pipeline import triage_pending
from buscador_midias.triage.prompt import Policy, prompt_version
from buscador_midias.triage.relevance import DEFAULT_BATCH_SIZE, HeadlineClassifier

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SITES = _REPO_ROOT / "data" / "sites.csv"
DEFAULT_REPORT = _REPO_ROOT / "var" / "probe-report.csv"
DEFAULT_DB = _REPO_ROOT / "var" / "state.db"
DEFAULT_SCHEDULE = _REPO_ROOT / "var" / "schedule.json"
DEFAULT_BACKUPS = _REPO_ROOT / "var" / "backups"
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
        help="Rodada de descoberta incremental: grava no estado e lista o que é novo.",
    )
    _add_site_args(discover)
    _add_network_args(discover)
    discover.add_argument("--db", type=Path, default=DEFAULT_DB, help="Arquivo SQLite do estado.")
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
        help="Sitemaps requisitados por site em cada rodada.",
    )
    discover.add_argument(
        "--skip-off-topic",
        action="store_true",
        help=(
            "Descarta no L0 as seções de temas sem interesse (esporte, culinária, horóscopo). "
            "Fica desligado por padrão: essas seções também trazem matérias de gestão e denúncia."
        ),
    )
    discover.add_argument(
        "--l1-model",
        type=Path,
        default=None,
        help=(
            "Filtro por manchete (gerado por scripts/train_l1.py): descarta, entre as URLs novas, "
            "as de baixo sinal de notícia adversa. Só vale para os idiomas que o modelo cobre."
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

    triage = commands.add_parser(
        "triage",
        help="Triagem por LLM das URLs novas ainda sem veredito (CONSOME CRÉDITOS do OpenRouter).",
    )
    triage.add_argument("--db", type=Path, default=DEFAULT_DB, help="Arquivo SQLite do estado.")
    triage.add_argument(
        "--retriage-discarded",
        action="store_true",
        help=(
            "Antes de triar, devolve à fila os descartes feitos com outra versão do prompt "
            "(use ao mudar o critério). Repetir o comando não reabre nada de novo."
        ),
    )
    _add_triage_options(triage, prefix="")

    discover.add_argument(
        "--triage",
        action="store_true",
        help=(
            "Depois da descoberta, classifica as URLs novas com o LLM (OpenRouter, CONSOME "
            "CRÉDITOS). Exige OPENROUTER_API_KEY no ambiente."
        ),
    )
    _add_triage_options(discover, prefix="triage-")

    dashboard = commands.add_parser(
        "dashboard",
        help="Painel web local (somente leitura) para acompanhar o fluxo em tempo real.",
    )
    dashboard.add_argument("--db", type=Path, default=DEFAULT_DB, help="Arquivo SQLite do estado.")
    dashboard.add_argument(
        "--host",
        default="127.0.0.1",
        help="Endereço de escuta. O painel não tem autenticação: mantenha em 127.0.0.1.",
    )
    dashboard.add_argument("--port", type=int, default=8765, help="Porta (0 escolhe uma livre).")
    dashboard.add_argument(
        "--schedule",
        type=Path,
        default=DEFAULT_SCHEDULE,
        help="JSON com as execuções previstas para a agenda do painel (opcional).",
    )

    enrich = commands.add_parser(
        "enrich",
        help=(
            "Para as URLs mantidas pela triagem: baixa o texto, analisa o contexto (OpenRouter) e "
            "captura a evidência dos achados (CONSOME CRÉDITOS)."
        ),
    )
    enrich.add_argument("--db", type=Path, default=DEFAULT_DB, help="Arquivo SQLite do estado.")
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
    corpus_sync.add_argument(
        "--db", type=Path, default=DEFAULT_DB, help="Arquivo SQLite do estado."
    )
    corpus_sync.add_argument("--sites", type=Path, default=DEFAULT_SITES, help="CSV de fontes.")
    corpus_sync.add_argument(
        "--batch-size", type=int, default=CORPUS_BATCH_SIZE, help="Artigos por lote."
    )
    corpus_sync.add_argument("--limit", type=int, default=None, help="Máximo de achados.")
    corpus_sync.add_argument(
        "--include-headline-only",
        action="store_true",
        help="Grava também achados baseados só na manchete (por padrão ficam retidos).",
    )
    corpus_sync.add_argument(
        "--min-confidence", type=float, default=0.0, help="Confiança mínima da análise."
    )
    corpus_sync.add_argument(
        "--dry-run", action="store_true", help="Só mostra o que seria gravado; não grava nada."
    )

    labels = commands.add_parser(
        "labels",
        help="Rotulagem humana às cegas para medir a precisão da triagem e da análise.",
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
    for parser_ in (labels_export, labels_score):
        parser_.add_argument(
            "--db", type=Path, default=DEFAULT_DB, help="Arquivo SQLite do estado."
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
        "--per-group", type=int, default=70, help="URLs por grupo (achados, mantidas, descartadas)."
    )
    labels_export.add_argument("--seed", type=int, default=1, help="Semente do sorteio.")

    backup = commands.add_parser(
        "backup", help="Cópia consistente do estado (SQLite), com data e hora UTC no nome."
    )
    backup.add_argument("--db", type=Path, default=DEFAULT_DB, help="Arquivo SQLite do estado.")
    backup.add_argument(
        "--out-dir", type=Path, default=DEFAULT_BACKUPS, help="Pasta onde a cópia é gravada."
    )

    report = commands.add_parser("report", help="Estatísticas das rodadas de descoberta.")
    report.add_argument("--db", type=Path, default=DEFAULT_DB, help="Arquivo SQLite do estado.")
    report.add_argument(
        "--audit",
        type=int,
        default=0,
        metavar="N",
        help="Mostra N URLs descartadas pela triagem por LLM, escolhidas ao acaso, para conferir.",
    )
    return parser


def _add_triage_options(parser: argparse.ArgumentParser, *, prefix: str) -> None:
    """Opções da triagem por LLM (``prefix`` evita colisão quando vêm junto do ``discover``)."""
    parser.add_argument(
        f"--{prefix}model",
        dest="model",
        default=DEFAULT_MODEL,
        help="Modelo do OpenRouter.",
    )
    parser.add_argument(
        f"--{prefix}policy",
        dest="policy",
        choices=[policy.value for policy in Policy],
        default=Policy.ENTITIES.value,
        help=(
            "Critério do descarte (entities: só descarta o que não tem pessoa ou organização "
            "identificável; permissive/balanced: só mantêm o que sugere fato adverso)."
        ),
    )
    parser.add_argument(
        f"--{prefix}batch-size",
        dest="batch_size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Manchetes por chamada ao LLM.",
    )
    parser.add_argument(
        f"--{prefix}limit",
        dest="triage_limit",
        type=int,
        default=None,
        help="Máximo de URLs pendentes avaliadas nesta execução.",
    )


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
    classifier: HeadlineClassifier | None = None,
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
        classifier: Classificador da triagem por LLM; se ausente, monta o do OpenRouter com as
            variáveis do ambiente (os testes injetam um fake).
        serve: Como o painel atende as requisições (padrão: ``serve_forever``); os testes injetam
            uma função que não bloqueia.
        enrich_ports: Dependências do ``enrich``; os testes injetam fakes.
        corpus_writer: Gravador do corpus usado por ``corpus-sync`` (os testes injetam um fake).

    Returns:
        Código de saída do processo (0 em sucesso, 2 para erro de uso ou configuração).
    """
    args = build_parser().parse_args(argv)
    if args.command == "report":
        return _report(args.db, args.audit)
    if args.command == "triage":
        return _triage(args, classifier, now)
    if args.command == "labels":
        return _labels(args)
    if args.command == "corpus-sync":
        return _corpus_sync(args, corpus_writer, now)
    if args.command == "enrich":
        return _enrich(args, enrich_ports or EnrichPorts(), now)
    if args.command == "dashboard":
        return _dashboard(args, serve)
    if args.command == "backup":
        return _backup(args, now or datetime.now(UTC))

    sites = _select_sites(args)
    if sites is None:
        return 2
    reference = now or datetime.now(UTC)
    if args.command == "probe":
        return _probe(args, sites, fetcher, client, reference)
    return _discover(args, sites, fetcher, client, corpus, now, classifier)


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
    http = client or exit_stack.enter_context(
        httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=True)
    )
    return build_fetcher(
        client=http,
        interval=args.interval,
        bright_data=args.bright_data,
        env=os.environ,
        max_fallback_requests=args.bright_data_max_requests,
        known_fallback_hosts=known_hosts,
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
        if not args.db.is_file():
            print(f"estado não encontrado em {args.db}: rode `discover` antes", file=sys.stderr)
            return 2
        if unlocker is None:
            print(
                "aviso: sem Bright Data; achados cujo HTML direto não for confiável ficam sem "
                "evidência até haver credenciais",
                file=sys.stderr,
            )
        store = StateStore(args.db)
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
    if not args.db.is_file():
        print(f"estado não encontrado em {args.db}: rode `discover` antes", file=sys.stderr)
        return 2
    if writer is None:
        try:
            writer = PostgresCorpusWriter.from_env(os.environ)
        except CorpusConfigError as error:
            print(error, file=sys.stderr)
            return 2
        except Exception as error:  # noqa: BLE001 - qualquer falha de conexão encerra com mensagem
            print(f"não consegui conectar ao corpus: {type(error).__name__}", file=sys.stderr)
            return 2
    store = StateStore(args.db)
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
            include_headline_only=args.include_headline_only,
            min_confidence=args.min_confidence,
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
        "retidos por regra": totals.withheld,
        "refeitos (sumiram)": totals.rewritten,
    }
    for name, count in rows.items():
        print(f"{name:>24}: {count}")


def _labels(args: argparse.Namespace) -> int:
    """Subcomando ``labels``: exporta um lote cego, pontua os preenchidos ou abre a tela."""
    if args.labels_command == "serve":
        return _labels_serve(args)
    if not args.db.is_file():
        print(f"estado não encontrado em {args.db}", file=sys.stderr)
        return 2
    store = StateStore(args.db, read_only=True)
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


def _backup(args: argparse.Namespace, now: datetime) -> int:
    """Grava uma cópia do estado em ``--out-dir``, sem sobrescrever nada."""
    if not args.db.is_file():
        print(f"estado não encontrado em {args.db}", file=sys.stderr)
        return 2
    destination = args.out_dir / f"state-{now.astimezone(UTC):%Y%m%d-%H%M%S}.db"
    store = StateStore(args.db, read_only=True)
    try:
        store.backup(destination)
        intact = StateStore(destination, read_only=True)
        try:
            ok = intact.integrity_ok()
        finally:
            intact.close()
    except FileExistsError:
        print(f"a cópia {destination} já existe; nada foi sobrescrito", file=sys.stderr)
        return 2
    finally:
        store.close()
    print(f"cópia gravada em {destination} ({destination.stat().st_size} bytes)")
    print(f"integridade: {'ok' if ok else 'FALHOU'}")
    return 0 if ok else 2


def _dashboard(
    args: argparse.Namespace, serve: Callable[[ThreadingHTTPServer], None] | None
) -> int:
    """Sobe o painel local e bloqueia até Ctrl+C."""
    if not args.db.is_file():
        print(f"estado não encontrado em {args.db}: rode `discover` antes", file=sys.stderr)
        return 2
    server = make_server(args.db, args.host, args.port, schedule_path=args.schedule)
    port = server.server_address[1]
    print(f"painel em http://{args.host}:{port}  (Ctrl+C para sair)", flush=True)
    try:
        (serve or (lambda srv: srv.serve_forever()))(server)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _open_classifier(args: argparse.Namespace, exit_stack: ExitStack) -> HeadlineClassifier:
    """Monta o classificador do OpenRouter com as variáveis do ambiente.

    Raises:
        MissingOpenRouterKeyError: Se ``OPENROUTER_API_KEY`` não estiver definida.
    """
    http = exit_stack.enter_context(httpx.Client())
    return classifier_from_env(os.environ, http, model=args.model, policy=Policy(args.policy))


def _triage(
    args: argparse.Namespace, classifier: HeadlineClassifier | None, now: datetime | None
) -> int:
    """Subcomando ``triage``: dá veredito às URLs novas pendentes e imprime o resumo."""
    with ExitStack() as exit_stack:
        try:
            classifier = classifier or _open_classifier(args, exit_stack)
        except MissingOpenRouterKeyError as error:
            print(error, file=sys.stderr)
            return 2
        if not args.db.is_file():
            print("nenhuma rodada registrada")
            return 0
        store = StateStore(args.db)
        exit_stack.callback(store.close)
        if args.retriage_discarded:
            reopened = store.reopen_discarded_triage(prompt_version(Policy(args.policy)))
            print(f"{'triagem: reabertas':>20}: {reopened}")
        _run_triage(args, store, classifier, now or datetime.now(UTC))
    return 0


def _run_triage(
    args: argparse.Namespace, store: StateStore, classifier: HeadlineClassifier, now: datetime
) -> None:
    """Executa a triagem das pendentes e imprime totais, consumo e custo."""
    usage_source = (lambda: classifier.usage) if hasattr(classifier, "usage") else None
    summary = triage_pending(
        store,
        classifier,
        now=now,
        model=args.model,
        prompt_version=prompt_version(Policy(args.policy)),
        batch_size=args.batch_size,
        limit=args.triage_limit,
        usage_source=usage_source,
    )
    totals = summary.totals
    print(f"{'triagem: manchetes':>20}: {totals.headlines}")
    print(f"{'triagem: mantidas':>20}: {totals.kept}")
    print(f"{'triagem: descartadas':>20}: {totals.discarded}")
    print(f"{'triagem: pendentes':>20}: {totals.pending}")
    cost = f"US$ {totals.cost_usd:.4f}" if totals.cost_usd is not None else "não informado"
    print(
        f"{'triagem: custo':>20}: {cost} "
        f"({totals.prompt_tokens} tokens de entrada, {totals.completion_tokens} de saída)"
    )


def _discover(
    args: argparse.Namespace,
    sites: Sequence[Site],
    fetcher: Fetcher | None,
    client: httpx.Client | None,
    corpus: CorpusIndex | None,
    now: datetime | None,
    classifier: HeadlineClassifier | None,
) -> int:
    """Roda uma rodada de descoberta incremental e imprime o resumo."""
    l1: L1Filter | None = None
    if args.l1_model is not None:
        try:
            l1 = L1Filter.load(args.l1_model)
        except (OSError, ValueError, KeyError) as error:
            print(f"não consegui ler o modelo L1 {args.l1_model}: {error}", file=sys.stderr)
            return 2

    with ExitStack() as exit_stack:
        if args.triage:
            try:
                classifier = classifier or _open_classifier(args, exit_stack)
            except MissingOpenRouterKeyError as error:
                print(error, file=sys.stderr)
                return 2

        if corpus is None and args.corpus:
            try:
                owned = PostgresCorpusIndex.from_env(os.environ)
            except CorpusConfigError as error:
                print(error, file=sys.stderr)
                return 2
            exit_stack.callback(owned.close)
            corpus = owned

        args.db.parent.mkdir(parents=True, exist_ok=True)
        store = StateStore(args.db)
        exit_stack.callback(store.close)
        try:
            stack = _open_stack(args, fetcher, client, exit_stack, store.fallback_hosts())
        except MissingCredentialsError as error:
            print(error, file=sys.stderr)
            return 2

        statuses: Counter[str] = Counter()

        def report_progress(done: int, total: int, record: SiteRunRecord) -> None:
            statuses[record.status] += 1
            _print_progress(done, total, statuses, args.progress_every)

        result = discover_all(
            sites,
            stack.fetcher,
            store,
            user_agent=USER_AGENT,
            workers=args.workers,
            max_sitemap_fetches=args.max_sitemap_fetches,
            fresh_days=args.fresh_days,
            corpus=corpus,
            off_topic_segments=DEFAULT_OFF_TOPIC_SEGMENTS if args.skip_off_topic else frozenset(),
            l1=l1,
            clock=(lambda: now) if now is not None else None,
            on_progress=report_progress,
        )
        reference = now or datetime.now(UTC)
        if stack.fallback is not None:
            store.add_fallback_hosts(stack.fallback.fallback_hosts, reference)
            store.record_run_usage(
                result.run_id,
                bright_data_requests=stack.fallback.fallback_requests,
                bright_data_hosts=len(stack.fallback.fallback_hosts),
            )
        _print_run(result)
        if stack.fallback is not None:
            print(f"{'bright_data_requests':>20}: {stack.fallback.fallback_requests}")
            print(f"{'bright_data_hosts':>20}: {len(stack.fallback.fallback_hosts)}")
        if args.triage and classifier is not None:
            _run_triage(args, store, classifier, now or datetime.now(UTC))
    return 0


def _report(db: Path, audit: int) -> int:
    """Imprime as estatísticas do estado (sem criar o arquivo se ele não existir).

    Com ``audit`` > 0 inclui uma amostra aleatória de URLs descartadas pela triagem por LLM.
    """
    if not db.is_file():
        print("nenhuma rodada registrada")
        return 0
    store = StateStore(db)
    try:
        print(format_report(build_report(store)))
        if audit > 0:
            print(f"\namostra de descartes da triagem LLM (até {audit}):")
            for row in store.sample_triaged("discard", audit):
                themes = ",".join(row.themes) or "-"
                print(f"- [{row.site_key}] {row.url} | temas: {themes} | motivo: {row.reason}")
    finally:
        store.close()
    return 0


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
        "filtradas (L0)": sum(r.filtered_urls for r in records),
        "sitemaps lidos": sum(r.sitemaps_fetched for r in records),
        "sitemaps pulados": sum(r.sitemaps_skipped for r in records),
    }
    print(f"rodada {result.run_id}")
    for name, count in totals.items():
        print(f"{name:>20}: {count}")
