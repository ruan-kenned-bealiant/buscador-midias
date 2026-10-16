"""URLs com data embutida: sitemaps publicados um por dia ou por mês.

Um endereço como ``.../sitemap/2026-10-15/`` só serve naquele dia. Em vez de gravá-lo como está
(e ele envelhecer sozinho), vira um modelo (``.../sitemap/{YYYY}-{MM}-{DD}/``) que se renderiza com
a data de hoje a cada uso.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

# Datas a que um sitemap "do dia" ou "do mês" pode se referir; mais antigas são arquivo de verdade.
_MAX_DAY_AGE = timedelta(days=45)
_MAX_MONTHS_BACK = 2
_DAY = re.compile(r"(?<!\d)(20\d{2})([-/]?)(0[1-9]|1[0-2])\2(0[1-9]|[12]\d|3[01])(?!\d)")
_MONTH = re.compile(r"(?<!\d)(20\d{2})([-/]?)(0[1-9]|1[0-2])(?!\d)")
_ANY_DAY = re.compile(r"(?<!\d)(\d{4})([-/]?)(\d{2})\2(\d{2})(?!\d)")
_ANY_MONTH = re.compile(r"(?<!\d)(\d{4})([-/]?)(\d{2})(?!\d)")
_YEAR_IN_NAME = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def find_date(url: str) -> datetime | None:
    """A data (dia, ou o dia 1 do mês, ou o dia 1 do ano) que a URL traz no caminho ou na consulta.

    Serve para ordenar sitemaps filhos de um índice sem ``lastmod``. Um ano sozinho só vale no
    nome do arquivo (``.../conceito/2026.xml``), onde costuma ser o ano do arquivo.

    Args:
        url: A URL (a data no host não conta).

    Returns:
        A data em UTC, ou ``None`` se a URL não traz uma.
    """
    rest = _tail(url)
    for pattern, has_day in ((_ANY_DAY, True), (_ANY_MONTH, False)):
        for match in pattern.finditer(rest):
            try:
                year, month = int(match.group(1)), int(match.group(3))
                day = int(match.group(4)) if has_day else 1
                return datetime(year, month, day, tzinfo=UTC)
            except ValueError:
                continue
    stem = urlsplit(url).path.rsplit("/", 1)[-1].split(".", 1)[0]
    named = _YEAR_IN_NAME.search(stem)
    return datetime(int(named.group(1)), 1, 1, tzinfo=UTC) if named else None


def templatize(url: str, now: datetime, *, max_day_age: timedelta = _MAX_DAY_AGE) -> str:
    """Troca a data recente da URL por um modelo (``{YYYY}``, ``{MM}``, ``{DD}``).

    Só vira modelo uma data que um sitemap "do dia" ou "do mês" teria: até 45 dias atrás (uma
    fotografia antiga de um levantamento ainda conta) ou no mês corrente e nos dois anteriores. Anos
    de arquivo (``post-sitemap-2019-04.xml``) ficam como estão.

    Args:
        url: A URL.
        now: Instante de referência.
        max_day_age: Idade máxima de uma data de dia para virar modelo.

    Returns:
        O modelo, ou a própria URL se não há data recente.
    """
    split = urlsplit(url)
    head = url[: len(url) - len(_tail(url))] if split.netloc else ""
    rest = _tail(url)
    for pattern, has_day in ((_DAY, True), (_MONTH, False)):
        for match in pattern.finditer(rest):
            sep = match.group(2)
            try:
                day = int(match.group(4)) if has_day else 1
                date = datetime(int(match.group(1)), int(match.group(3)), day, tzinfo=UTC)
            except ValueError:
                continue
            if not _recent(date, now, has_day, max_day_age):
                continue
            token = "{YYYY}" + sep + "{MM}" + (sep + "{DD}" if has_day else "")
            return head + rest[: match.start()] + token + rest[match.end() :]
    return url


def render(url: str, now: datetime) -> str:
    """Preenche o modelo com a data de ``now`` (UTC); URL sem modelo volta como está."""
    moment = now.astimezone(UTC)
    return (
        url.replace("{YYYY}", f"{moment.year:04d}")
        .replace("{MM}", f"{moment.month:02d}")
        .replace("{DD}", f"{moment.day:02d}")
    )


def render_window(url: str, now: datetime) -> tuple[str, ...]:
    """As URLs a ler agora: a de hoje e a de ontem (o dia vira em horas diferentes por fuso).

    Args:
        url: A URL ou o modelo.
        now: Instante de referência.

    Returns:
        Uma URL se não há modelo (ou se hoje e ontem coincidem, como no modelo mensal fora da
        virada do mês); senão a de hoje e a de ontem.
    """
    today = render(url, now)
    if today == url and "{" not in url:
        return (url,)
    yesterday = render(url, now - timedelta(days=1))
    return (today,) if yesterday == today else (today, yesterday)


def _tail(url: str) -> str:
    """O que vem depois do host: caminho, consulta e fragmento."""
    split = urlsplit(url)
    if not split.netloc:
        return url
    prefix = f"{split.scheme}://{split.netloc}"
    return url[len(prefix) :] if url.startswith(prefix) else url


def _recent(date: datetime, now: datetime, has_day: bool, max_day_age: timedelta) -> bool:
    """Se a data cabe num sitemap do dia (ou do mês) de agora."""
    if has_day:
        return now - max_day_age <= date <= now + timedelta(days=2)
    months_back = (now.year * 12 + now.month) - (date.year * 12 + date.month)
    return -1 <= months_back <= _MAX_MONTHS_BACK
