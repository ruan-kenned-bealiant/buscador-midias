"""Conteúdo rotineiro sem interesse (horóscopo, clima, loteria, receita, logística de jogo).

Três barreiras, da mais barata à mais segura:

1. ``url_exclusion``: só o caminho da URL, antes de baixar a página. Sem título para conferir,
   usa apenas seções inequívocas; seções amplas (esporte, entretenimento) nunca entram.
2. ``title_exclusion``: título extraído, com proteção contra termos de risco. Uma matéria que
   fala de fraude, morte ou processo não é excluída mesmo que o assunto pareça rotineiro.
3. ``category_exclusion``: categoria devolvida pela análise de contexto, sem custo extra.
   Matéria esportiva com fato adverso (doping, manipulação) continua no fluxo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Exclusion:
    """Por que o conteúdo foi barrado: categoria, regra e etapa que decidiu."""

    category: str
    rule: str
    stage: str


@dataclass(frozen=True)
class Rule:
    """Padrões de uma categoria; ``None`` indica que a etapa não usa esse sinal."""

    category: str
    strict_url: re.Pattern[str] | None
    loose_url: re.Pattern[str] | None
    title: re.Pattern[str] | None


def _p(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _segment(words: str) -> str:
    """Palavra inteira entre barras ou hífens do caminho, nunca no meio de outra."""
    return rf"(?:^|[/_-])(?:{words})(?:$|[/_.-])"


RULES = (
    Rule(
        "horoscope",
        _p(
            _segment(
                r"horoscop[oe]s?|horóscopo|horoskop\w*|oroscopo|astrolog\w+|astrology|zodiac\w*"
            )
        ),
        _p(_segment(r"signos|tarot")),
        _p(
            r"\bhor[óo]scopo\b|\bhoroscope\b|\bhoroskop\w*|previs[õo]es para os 12 signos"
            r"|\bsignos? do zod[ií]aco"
        ),
    ),
    Rule(
        "weather",
        _p(
            r"previs[aã]o-?do-?tempo|previsao-tempo|weather-forecast"
            r"|pronostico-del-(?:tiempo|clima)"
        ),
        _p(_segment(r"weather|el-tiempo|meteo|pronostico\w*")),
        _p(
            r"^previs[ãa]o do tempo|^weather(?: forecast| warnings?)?\b"
            r"|^pron[óo]stico del (?:tiempo|clima)|^m[ée]t[ée]o\b|^el tiempo\b"
        ),
    ),
    Rule(
        "lottery",
        _p(r"(?:^|/)(?:loterias?|lottery)(?:/|$)"),
        _p(_segment(r"mega-?sena|lotof[aá]cil|quina|lotto|powerball|euromillions")),
        _p(
            r"\b(?:mega-?sena|lotof[áa]cil|quina|dia de sorte|timemania|lotomania|powerball"
            r"|euromillions)\b.*(?:resultado|sorteio|pr[êe]mio|dezenas)"
            r"|^resultado d[ao] (?:sorteio|loteria|federal)"
        ),
    ),
    Rule(
        "cooking",
        _p(r"(?:^|/)(?:recipes?|recetas?)(?:/|$)"),
        None,
        _p(
            r"^receita de (?!r\$|\d|imposto|tributo)\w+|^como (?:fazer|preparar) \w+"
            r"|\brecipe\b|^receta de \w+"
        ),
    ),
    Rule(
        "match",
        None,
        _p(r"onde-assistir|escalac|palpite|horario-e-onde"),
        _p(
            r"\bonde assistir\b|\bescala[çc][õo]es?\b|\bpalpites?\b|\bprov[áa]veis? escala"
            r"|\bhor[áa]rio e onde\b|\bhor[áa]rio, onde assistir"
        ),
    ),
    Rule(
        "tv_recap",
        None,
        _p(r"(?:^|/)novelas?/.*(?:resumo|capitulo)|resumo-d[aeo]s?-novela"),
        _p(
            r"\bresumo d[ao]s? (?:novela|cap[íi]tulo)|\bcap[íi]tulo de hoje\b"
            r"|\bsaiba o que acontece\b.*\bnovela"
        ),
    ),
)

# Qualquer um destes termos no título indica fato adverso: a regra de assunto não se aplica.
RISK = _p(
    r"investig|fraud|corrup|preso|presa|pris[ãa]o|den[úu]ncia|acus|crime|assassin|morr"
    r"|mort[eo]s?|v[íi]tima|lavagem|propina|desvio|tr[áa]fico|abuso|estupro|doping|manipula"
    r"|protest|processo|justi[çc]a|tribunal|julg|stjd|sentenc|arrest|indict|bribe|scandal"
    r"|laundering|\bkill|\bdies\b|\bdied\b|injur|dead|death|murder|lawsuit|court|sued|esc[áa]ndalo|detenid|asesin"
    r"|desabrig|trag[ée]dia|tragedy|\bmat(?:a|am|ou|aram|ar|an|aron)\b|recorr|recurso"
    r"|colis|acident|explos|inc[êe]ndio|naufr|desabament"
)


def url_exclusion(url: str) -> Exclusion | None:
    """Exclui só por seção inequívoca do caminho, sem título para conferir o risco."""
    path = urlsplit(url).path
    for rule in RULES:
        if rule.strict_url and rule.strict_url.search(path):
            return Exclusion(rule.category, f"{rule.category}_url", "url")
    return None


def title_exclusion(url: str, title: str | None) -> Exclusion | None:
    """Usa título e pistas da URL, mas nunca exclui quando o título sugere fato adverso."""
    if not title or RISK.search(title):
        return None
    path = urlsplit(url).path
    for rule in RULES:
        if (
            (rule.title and rule.title.search(title))
            or (rule.strict_url and rule.strict_url.search(path))
            or (rule.loose_url and rule.loose_url.search(path))
        ):
            return Exclusion(rule.category, f"{rule.category}_title", "title")
    return None


def category_exclusion(category: str | None, finding: bool, blocked: list[str]) -> Exclusion | None:
    """Barra a categoria da IA só quando ela não relata fato adverso."""
    if category and category in blocked and not finding:
        return Exclusion(category, "ai_category", "ai")
    return None
