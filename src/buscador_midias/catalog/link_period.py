"""Período que o endereço de um link cobre: fatia de dia ou de mês que já fechou é histórica.

Muitos sites publicam um sitemap por dia (``/2026/10/19_1.xml``, ``?date=2026-05-28``) ou por mês
(``sitemap-2026-09.xml``). Depois que o período passa, nada novo entra nele: manter o link só gasta
leitura. Só se lê a data do endereço (caminho e consulta, nunca o domínio) e só em formatos sem
ambiguidade; sem data reconhecível o link não tem período e nunca é tratado como histórico.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

# Notícia do dia pode cair na fatia da véspera por fuso; é a mesma folga do roteador temporal.
OVERLAP = timedelta(days=2)
ZONE = ZoneInfo("America/Sao_Paulo")
# Links desse modelo são datados de propósito: o mecanismo troca a data sozinho a cada dia.
DATED_BY_DESIGN = ("g1_daily",)

_MONTH = r"(0[1-9]|1[0-2])"
_DAY = r"(0[1-9]|[12]\d|3[01])"
_DAY_PATH = re.compile(rf"(?<!\d)(20\d{{2}})[/_-]{_MONTH}[/_-]{_DAY}(?!\d)")
_MONTH_PATH = re.compile(rf"(?<!\d)(20\d{{2}})[/_-]{_MONTH}(?!\d)")
_MONTH_COMPACT = re.compile(rf"(?<![\d])(20\d{{2}}){_MONTH}(?!\d)")
_YEAR_KEYS = ("year", "yyyy", "y")
_MONTH_KEYS = ("month", "mm", "m")
_DAY_KEYS = ("day", "dd", "d")


@dataclass(frozen=True)
class Period:
    """Primeiro e último dia coberto pelo endereço."""

    start: date
    end: date


def _day(year: int, month: int, day: int) -> Period | None:
    try:
        moment = date(year, month, day)
    except ValueError:
        return None
    return Period(moment, moment)


def _month(year: int, month: int) -> Period:
    return Period(date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1]))


def _from_query(query: str) -> Period | None:
    values = {key.lower(): value[0] for key, value in parse_qs(query).items() if value}

    def first(keys: tuple[str, ...]) -> str | None:
        return next((values[k] for k in keys if values.get(k, "").isdigit()), None)

    year, month, day = first(_YEAR_KEYS), first(_MONTH_KEYS), first(_DAY_KEYS)
    if not (year and len(year) == 4 and year.startswith("20") and month and 1 <= int(month) <= 12):
        return None
    if day:
        return _day(int(year), int(month), int(day))
    return _month(int(year), int(month))


def url_period(url: str) -> Period | None:
    """Período lido do caminho e da consulta; vários dias (``from…to…``) valem pelo intervalo."""
    parts = urlsplit(url)
    text = f"{parts.path}?{parts.query}"
    found = [p for m in _DAY_PATH.finditer(text) if (p := _day(int(m[1]), int(m[2]), int(m[3])))]
    if not found:
        found = [_month(int(m[1]), int(m[2])) for m in _MONTH_PATH.finditer(text)]
    if not found:
        found = [_month(int(m[1]), int(m[2])) for m in _MONTH_COMPACT.finditer(text)]
    if not found and (query := _from_query(parts.query)):
        found = [query]
    if not found:
        return None
    return Period(min(p.start for p in found), max(p.end for p in found))


def is_historical(url: str, model: str, now: datetime) -> bool:
    """Verdadeiro se todo o período do endereço terminou há mais que a folga de sobreposição."""
    if model in DATED_BY_DESIGN:
        return False
    period = url_period(url)
    if period is None:
        return False
    today = now.astimezone(ZONE).date()
    return period.end + OVERLAP < today
