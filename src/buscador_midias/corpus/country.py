"""Origem editorial: região do idioma, país do artigo e cadastro da fonte, nesta ordem."""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache
from urllib.parse import urlsplit

# ISO 3166-1 alpha-2. INT, UN, EU e códigos legados inválidos não são países.
_ISO_COUNTRIES = (
    "AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL "
    "BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV "
    "CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD "
    "GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM "
    "IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK "
    "LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW "
    "MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR "
    "PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS "
    "ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY "
    "UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW "
)
COUNTRY_CODES = frozenset(_ISO_COUNTRIES.split())


def country_code(value: object) -> str | None:
    """Aceita somente um país alfa-2 válido, sem inferir país de uma língua genérica."""
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if code in COUNTRY_CODES else None


def language_region(language: str | None) -> str | None:
    """Extrai região BCP-47, inclusive script; en-US não decide (regra do PowerDD)."""
    match = re.fullmatch(r"[a-z]{2,3}(?:[-_][a-z]{4})?[-_]([a-z]{2})", language or "", re.I)
    region = country_code(match[1]) if match else None
    return region if region != "US" else None


def registered_country(url: str, registry: Mapping[str, str]) -> str | None:
    """Prefere host mais específico e caminho mais longo, por segmentos inteiros."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").removeprefix("www.").lower()
    path = tuple(p for p in parsed.path.lower().split("/") if p)
    entries = _registry_entries(tuple(registry.items()))
    labels = host.split(".")
    for index in range(len(labels) - 1):
        candidates = entries.get(".".join(labels[index:]), [])
        covering = [(p, c) for p, c in candidates if path[: len(p)] == p]
        if covering:
            return max(covering, key=lambda item: len(item[0]))[1]
        codes = {c for _, c in candidates}
        if len(codes) == 1:
            return codes.pop()
    return None


def resolve_country(
    language: str | None, article_country: object, site_country: str | None
) -> tuple[str | None, str | None]:
    """Devolve país e método; fonte composta mantém os países e desconhecido permanece nulo."""
    region = language_region((language or "").strip())
    if region:
        return region, "language"
    saved = country_code(article_country)
    if saved:
        return saved, "article"
    codes = list(
        dict.fromkeys(
            code for part in (site_country or "").split("/") if (code := country_code(part))
        )
    )
    combined = "/".join(codes)
    if combined and len(combined) <= 10:
        return combined, "site"
    return None, None


@lru_cache(maxsize=8)
def _registry_entries(
    registry: tuple[tuple[str, str], ...],
) -> dict[str, list[tuple[tuple[str, ...], str]]]:
    """Reutiliza o índice de fontes entre artigos, sem analisar mil URLs por gravação."""
    entries: dict[str, list[tuple[tuple[str, ...], str]]] = {}
    for key, value in registry:
        registered = urlsplit(key if "://" in key else "https://" + key)
        key_host = (registered.hostname or "").removeprefix("www.").lower()
        segments = tuple(p for p in registered.path.lower().split("/") if p)
        entries.setdefault(key_host, []).append((segments, value))
    return entries
