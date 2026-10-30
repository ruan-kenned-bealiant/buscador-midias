"""Limites de trabalho em trânsito, independentes do número total de links."""

from __future__ import annotations

import hashlib
import re
import threading
import time as monotonic_time
from collections import deque
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

QUEUE_LIMITS = dict(discovery=64, dedup=1000, fetch=64, context=32, evidence=32, persist=32)
# Evidência pode acumular no PostgreSQL sem pausar a análise de contexto.
# QUEUE_LIMITS continua limitando apenas o lote publicado no RabbitMQ.
# Descoberta e dedup produzem metadados leves no PostgreSQL, sem aguardar downloads.
BACKLOG_LIMITS = dict(context=200, persist=200)
DEDUP_BATCH = 500
RESERVE_BYTES = 256 * 1024 * 1024


def recovery_workers(workers: int) -> int:
    """Workers da fila de recuperação: um para cada oito principais, nunca menos de um.

    Os principais só ajudam a recuperação quando sua fila esvazia, o que não ocorre enquanto a
    etapa anterior a alimenta; com um único worker dedicado, as retentativas se acumulavam.
    """
    return max(1, workers // 8)


class ProviderBreaker:
    """Pausa uma etapa só quando o provedor falha de forma repetida, não em erro isolado.

    Com dezenas de workers na mesma etapa, uma resposta vazia ou um HTTP 502 por minuto é
    normal; pausar todos por causa de um deixava a etapa parada por minutos seguidos. A etapa
    abre após ``threshold`` falhas em ``window`` segundos (somadas entre os workers), pausa por
    ``pause`` segundos e recomeça a contar do zero. Falhas de jobs que já estavam em andamento
    durante a pausa não a prolongam.
    """

    def __init__(
        self,
        threshold: int = 5,
        window: float = 60,
        pause: float = 60,
        clock: Callable[[], float] = monotonic_time.monotonic,
    ) -> None:
        """``clock`` é injetável para testar a janela sem esperar."""
        self.threshold = threshold
        self.window = window
        self.pause = pause
        self.clock = clock
        self.lock = threading.Lock()
        self.failures: dict[str, deque[float]] = {}
        self.open_until: dict[str, tuple[float, str]] = {}

    def failure(self, stage: str, reason: str) -> bool:
        """Registra a falha; devolve ``True`` apenas quando ela abre a etapa."""
        with self.lock:
            now = self.clock()
            if self._reason(stage, now) is not None:
                return False
            recent = self.failures.setdefault(stage, deque())
            recent.append(now)
            while recent and now - recent[0] > self.window:
                recent.popleft()
            if len(recent) < self.threshold:
                return False
            recent.clear()
            self.open_until[stage] = (now + self.pause, reason)
            return True

    def paused(self, stage: str) -> str | None:
        """Motivo da pausa em vigor para a etapa, ou ``None`` quando ela pode trabalhar."""
        with self.lock:
            return self._reason(stage, self.clock())

    def _reason(self, stage: str, now: float) -> str | None:
        until, reason = self.open_until.get(stage, (0.0, ""))
        return reason if until > now else None


def is_last_attempt(failures: int, max_attempts: int) -> bool:
    """A tentativa em curso é a última: se falhar, o job vira ``failed`` em vez de ``retry``."""
    return failures + 1 >= min(3, max_attempts)


def today_start(now: datetime, timezone: str = "America/Sao_Paulo") -> datetime:
    """Calcula a meia-noite civil, sem confundir dia atual com últimas 24 horas."""
    if now.tzinfo is None:
        raise ValueError("O instante precisa conter fuso horário.")
    return now.astimezone(ZoneInfo(timezone)).replace(hour=0, minute=0, second=0, microsecond=0)


MAX_DAY_AGE = 30


def day_window(
    day: str, now: datetime, timezone: str = "America/Sao_Paulo"
) -> tuple[datetime, datetime]:
    """Janela de um dia civil inteiro; o dia corrente termina agora, nunca no futuro.

    O limite de 30 dias evita varrer arquivos antigos, cujos feeds já não listam o dia.
    """
    try:
        civil = date.fromisoformat(day)
    except (TypeError, ValueError):
        raise ValueError("Informe o dia no formato AAAA-MM-DD.") from None
    zone = ZoneInfo(timezone)
    today = now.astimezone(zone).date()
    if civil > today:
        raise ValueError("O dia não pode estar no futuro.")
    if (today - civil).days > MAX_DAY_AGE:
        raise ValueError(f"O dia deve estar nos últimos {MAX_DAY_AGE} dias.")
    since = datetime.combine(civil, time.min, zone)
    return since, min(datetime.combine(civil + timedelta(days=1), time.min, zone), now)


def clock_slot(now: datetime, timezone: str, hours: list[int]) -> datetime | None:
    """Retorna o horário atual com tolerância de 15 minutos, sem repor rodadas passadas."""
    midnight = today_start(now, timezone)
    due = [
        midnight + timedelta(hours=hour)
        for hour in hours
        if midnight + timedelta(hours=hour) <= now
    ]
    slot = max(due) if due else None
    return slot if slot and now - slot < timedelta(minutes=15) else None


def next_slot(now: datetime, timezone: str, hours: list[int]) -> datetime | None:
    """Próximo horário fixo estritamente depois de ``now`` (hoje ou amanhã), no fuso dado."""
    if not hours:
        return None
    midnight = today_start(now, timezone)
    candidates = [
        midnight + timedelta(days=day, hours=hour) for day in (0, 1) for hour in sorted(hours)
    ]
    return next(slot for slot in candidates if slot > now)


def is_boundary_slot(slot: datetime, now: datetime, timezone: str, hours: list[int]) -> bool:
    """Se ``slot`` é o primeiro ou o último horário fixo do dia.

    A última rodada tem a janela mais larga do dia; a primeira, com ``close_previous_day``, cobre
    o dia anterior inteiro. Um link lido só nessas duas não perde notícia.
    """
    if not hours:
        return False
    midnight = today_start(now, timezone)
    return slot in (midnight + timedelta(hours=min(hours)), midnight + timedelta(hours=max(hours)))


# Falhas que repetir não resolve: bloqueio do site, página inexistente, destino proibido ou
# credencial do provedor pago. Uma nova rodada só tenta de novo o que pode passar (timeout,
# conexão, 5xx, análise sem resposta). Expressão regular do PostgreSQL (operador ``~``).
PERMANENT_FAILURE = (
    "HTTP 40[1-4]|HTTP 410|redirect_cycle|fora dos dom|robots_unavailable|bright_data_api_error"
)


# Folga contra fuso mal rotulado em ``lastmod``: o instante real pode ser até 12 h posterior.
LASTMOD_MARGIN = timedelta(hours=12)


def lastmod_outside(stamp: str | None, since: datetime) -> dict[str, Any] | None:
    """Exclui só quando ``lastmod`` prova que a publicação foi anterior à janela.

    Uma página não é publicada depois da última modificação, então ``lastmod`` é um limite
    superior da publicação: serve para excluir, nunca para datar a notícia. Data pura
    (meia-noite exata ou sem fuso) vale o dia inteiro, porque o dia pode terminar em outro fuso.
    """
    if not stamp:
        return None
    try:
        modified = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    whole_day = modified.tzinfo is None or modified.timetz().replace(tzinfo=None) == time.min
    if modified.tzinfo is None:
        modified = modified.replace(tzinfo=UTC)
    bound = modified + (timedelta(days=1) if whole_day else timedelta()) + LASTMOD_MARGIN
    if bound >= since:
        return None
    return dict(rule="lastmod_before_window", lastmod=stamp, bound=bound.isoformat())


# Dia no caminho da URL (/AAAA/MM/DD), sem aceitar dígitos colados que formariam outro número.
PATH_DATE = re.compile(r"/(20\d{2})/(\d{2})/(\d{2})(?!\d)")
# Primeiro segmento que identifica listagem de assunto, tag ou autor, nunca uma notícia.
INDEX_SEGMENTS = frozenset({"topic", "topics", "tag", "tags", "author", "authors", "taxonomy"})
# Seções que o site lista no próprio feed, mas que redirecionam para outro domínio (a leitura é
# recusada por sair do domínio da fonte): por host sem ``www``, o primeiro segmento do caminho.
EXTERNAL_SECTIONS = {
    "scmp.com": frozenset({"yp", "spark", "posties", "young-post", "goldthread"}),
}


def path_date_outside(
    url: str, since: datetime, timezone: str = "America/Sao_Paulo"
) -> dict[str, Any] | None:
    """Exclui quando o dia do caminho terminou antes do início da janela, no fuso da rodada.

    O dia do caminho é uma pista, não uma data de publicação: serve só para excluir, e só
    quando o dia inteiro já passou. Dias iguais ou posteriores ao início da janela, datas
    impossíveis e URLs sem data nunca são excluídos.
    """
    match = PATH_DATE.search(urlsplit(url).path) if isinstance(url, str) else None
    if not match:
        return None
    try:
        day = date(*map(int, match.groups()))
        end = datetime.combine(day + timedelta(days=1), time.min, ZoneInfo(timezone))
    except (ValueError, KeyError):
        return None
    if end > since:
        return None
    return dict(rule="path_date_before_window", path_date=day.isoformat(), bound=end.isoformat())


def index_page(url: str) -> dict[str, Any] | None:
    """Reconhece página de índice pelo primeiro segmento do caminho, nunca por posição solta.

    ``/native/negocios/topics/x/article/123`` é uma notícia que só contém a palavra mais
    adiante; por isso só o primeiro segmento decide.
    """
    if not isinstance(url, str):
        return None
    segment = urlsplit(url).path.lstrip("/").split("/", 1)[0].lower()
    return dict(rule="index_page", segment=segment) if segment in INDEX_SEGMENTS else None


def excluded_section(url: str) -> dict[str, Any] | None:
    """Reconhece a seção do site que vive em outro domínio (``EXTERNAL_SECTIONS``).

    Decide pelo host (com ou sem ``www``) e pelo primeiro segmento inteiro do caminho: a seção
    ``yp`` do scmp.com é excluída, mas ``ypsilon`` ou ``yp.scmp.com`` não.
    """
    if not isinstance(url, str):
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    segment = parts.path.lstrip("/").split("/", 1)[0].lower()
    if segment in EXTERNAL_SECTIONS.get(host, ()):
        return dict(rule="excluded_section", host=host, segment=segment)
    return None


def url_outside(
    url: str, since: datetime, timezone: str = "America/Sao_Paulo", *, explicit_date: bool = False
) -> dict[str, Any] | None:
    """Decide pela forma da URL.

    Índice e seção externa valem sempre; a data do caminho só sem publicação explícita.
    """
    return (
        index_page(url)
        or excluded_section(url)
        or (None if explicit_date else path_date_outside(url, since, timezone))
    )


def publication_outside(job: dict[str, Any]) -> dict[str, Any] | None:
    """Filtra apenas publicação explícita de RSS/News, nunca lastmod ou data do caminho."""
    stamp = job["article"]["payload"].get("published_hint")
    basis = job["article"]["payload"].get("published_hint_basis")
    if not stamp or basis != "xml_publication":
        return None
    try:
        published = datetime.fromisoformat(stamp)
        if published.tzinfo is None:
            return None
        if job["run"]["since"] <= published <= job["run"]["until"]:
            return None
    except (ValueError, TypeError):
        return None
    return dict(
        outcome="fora_da_janela_no_xml",
        status="outside_window",
        article=dict(published_at=published.isoformat(), date_basis="xml_publication"),
        metrics=dict(avoided_article_requests=1),
    )


def audit_sampled(run_id: str, link_id: int, rate: float) -> bool:
    """Se este link entra na amostra de auditoria do corte pelo topo nesta rodada.

    Determinística por rodada e link (um reinício ou retentativa não troca a amostra) e sem
    estado: a fração de links sorteados é ``rate``.
    """
    if rate <= 0:
        return False
    if rate >= 1:
        return True
    digest = hashlib.sha256(f"{run_id}:{link_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2**32 < rate


def head_outside(read_order: str, top: str | None, since: datetime) -> dict[str, Any] | None:
    """Corta o documento inteiro quando o topo, o mais novo, já está fora da janela.

    Só vale com ``read_order == newest_first`` comprovado no link. O topo é tratado como o
    ``lastmod`` é: limite superior da publicação de todas as entradas, com a mesma margem de fuso
    e o mesmo cuidado com data pura; qualquer dúvida mantém a leitura completa.
    """
    if read_order != "newest_first":
        return None
    found = lastmod_outside(top, since)
    if not found:
        return None
    return dict(rule="head_before_window", top=top, bound=found["bound"])
