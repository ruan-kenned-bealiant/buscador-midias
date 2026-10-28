"""Tema inútil de um link (clima, horóscopo, receita, cinema…), pelo caminho e pelo rótulo.

Só o caminho da URL e o rótulo contam: o domínio de um veículo nunca diz o tema do feed.
Esporte é diferente: escândalo e corrupção no futebol são úteis e placar não, e o feed mistura
os dois, então o link vai para revisão em vez de ser removido.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

REMOVE = "remove"
REVIEW = "review"

# (tema, ação, palavras). As palavras já estão sem acento e em minúsculas.
THEMES = (
    (
        "horoscope",
        REMOVE,
        r"horoscop[oe]s?|horoskop\w*|astrolog\w+|astrology|zodiac\w*|signos|tarot",
    ),
    (
        "weather",
        REMOVE,
        r"weather|forecast|meteo|clima|pronostico\w*|previsao-do-tempo|previsao-tempo"
        r"|previsao do tempo|el-tiempo|el tiempo",
    ),
    (
        "cooking",
        REMOVE,
        r"receitas?|recipes?|recetas?|culinaria|gastronomia|cooking|cozinha",
    ),
    ("lottery", REMOVE, r"loterias?|lottery|mega-?sena|lotto"),
    (
        "entertainment",
        REMOVE,
        r"entretenimento|entertainment|cinema|filmes?|movies?|celebrit\w+|famosos|fofocas?"
        r"|novelas?|musica|music|series-e-tv|tv-shows?",
    ),
    (
        "lifestyle",
        REMOVE,
        r"lifestyle|estilo-de-vida|estilo de vida|moda|fashion|beleza|beauty|viagem|viagens|turismo"
        r"|travel|bem-estar|decoracao",
    ),
    ("sports", REVIEW, r"esportes?|sports?|futebol|football|soccer|basquete|basketball|tenis"),
)

_COMPILED = tuple(
    (
        theme,
        action,
        re.compile(rf"(?:^|[/_.\s-])(?:{words})(?:$|[/_.\s-])"),
    )
    for theme, action, words in THEMES
)


def _plain(text: str) -> str:
    """Minúsculas e sem acento, para que Horóscopo e horoscopo sejam a mesma palavra."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def link_theme(url: str, label: str) -> dict[str, str] | None:
    """Tema inútil do link e o que fazer (remover ou revisar), ou ``None`` se é notícia comum."""
    places = (("path", _plain(urlsplit(url).path)), ("label", _plain(label or "")))
    for theme, action, pattern in _COMPILED:
        for place, text in places:
            if text and pattern.search(text):
                return {"theme": theme, "action": action, "where": place}
    return None
