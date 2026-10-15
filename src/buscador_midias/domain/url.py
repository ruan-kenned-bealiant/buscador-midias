"""Canonicalização de URL e ``url_hash``, em paridade com o ``MidiasIaUrlCanonicalizer`` do KyX.

O corpus de Mídias IA foi hasheado por essa regra, então ela é reproduzida byte a byte, inclusive
nas esquisitices do PHP (``urlencode`` escapa ``~`` e usa ``+`` para espaço; ``strtolower`` só
mexe em ASCII; ``ksort`` ordena chaves puramente numéricas como número). A paridade é verificada
contra o código PHP real por um golden file (``tests/fixtures/canonical_golden.json``).
"""

from __future__ import annotations

import hashlib
import re
from functools import cmp_to_key
from urllib.parse import quote_from_bytes, unquote_to_bytes, urlsplit

# Mesmo conjunto do PHP; comparado em minúsculas ASCII com a chave já decodificada.
_TRACKING_PARAMS = frozenset(
    {
        b"utm_source",
        b"utm_medium",
        b"utm_campaign",
        b"utm_content",
        b"utm_term",
        b"fbclid",
        b"gclid",
        b"ref",
        b"source",
        b"s",
        b"via",
    }
)
_WWW_PREFIX = re.compile(r"^www\d*\.")
# Regra do KyX antes de 2026-10-01: só o ``www.`` literal.
_LEGACY_WWW_PREFIX = re.compile(r"^www\.")
# O PHP chaveia arrays com inteiros canônicos (sem zero à esquerda, sem "-0").
_PHP_INT_KEY = re.compile(rb"^(?:0|-?[1-9]\d*)$")
_PHP_TRIM = " \t\n\r\0\x0b"


def url_hash(url: str) -> str:
    """SHA-256 (hex, 64 caracteres) da URL canonicalizada; é o ``url_hash`` do corpus."""
    return hashlib.sha256(canonicalize_url(url).encode("utf-8")).hexdigest()


def canonicalize_url(url: str) -> str:
    """Normaliza a URL para que variações da mesma notícia colapsem em uma só.

    Regras: esquema sempre ``https``; host em minúsculas (só ASCII), sem ponto final e sem
    prefixo ``www``/``www2``...; porta, usuário e fragmento descartados; caminho sem ``/`` final
    (``/`` se vazio); query sem parâmetros de rastreio, sem pares vazios ou sem ``=``, ordenada
    por chave e reescrita no estilo ``urlencode`` do PHP.

    Args:
        url: URL a normalizar (espaços nas pontas são ignorados).

    Returns:
        A URL canônica. Se não for interpretável, devolve a entrada sem os espaços das pontas.
    """
    return _canonicalize(url, _WWW_PREFIX)


def legacy_url_hash(url: str) -> str | None:
    """Hash que o KyX gerava até 2026-10-01, quando só o prefixo literal ``www.`` era removido.

    O commit 413fc198f do KyX passou a remover também ``www1.``, ``www2.``... Linhas do corpus
    gravadas antes disso (hoje, as 1.007 de ``www1.folha.uol.com.br``) ficaram com o hash antigo.

    Args:
        url: URL a hashear.

    Returns:
        O hash legado, ou ``None`` quando coincide com o atual (hosts sem ``www`` numerado).
    """
    legacy = _canonicalize(url, _LEGACY_WWW_PREFIX)
    if legacy == canonicalize_url(url):
        return None
    return hashlib.sha256(legacy.encode("utf-8")).hexdigest()


def url_hash_candidates(url: str) -> tuple[str, ...]:
    """Todos os hashes sob os quais esta URL pode estar gravada no corpus.

    Returns:
        O hash atual e, se for diferente, o legado — nessa ordem.
    """
    legacy = legacy_url_hash(url)
    return (url_hash(url),) if legacy is None else (url_hash(url), legacy)


def _canonicalize(url: str, www_prefix: re.Pattern[str]) -> str:
    """Canonicaliza com a regra de remoção do prefixo ``www`` informada."""
    raw = url.strip(_PHP_TRIM)
    parts = _split(raw)
    if parts is None:
        return raw

    host, path, query = parts
    host = www_prefix.sub("", _ascii_lower(host).rstrip("."), count=1)
    path = path.rstrip("/") or "/"
    canonical_query = _canonical_query(query)
    return f"https://{host}{path}" + (f"?{canonical_query}" if canonical_query else "")


def url_domain(url: str) -> str | None:
    """Domínio da URL (minúsculo, sem ``www``/``www2``...), ou ``None`` se não houver host.

    Ao contrário de ``canonicalize_url``, não ignora espaços nas pontas: uma URL com espaço no
    início não tem host reconhecível (mesmo comportamento do ``parse_url`` do PHP).
    """
    if url[:1] in _PHP_TRIM and url[:1]:
        return None
    parts = _split(url)
    if parts is None or not parts[0]:
        return None
    return _WWW_PREFIX.sub("", _ascii_lower(parts[0]).rstrip("."), count=1)


def _split(url: str) -> tuple[str, str, str] | None:
    """Separa ``(host, caminho, query)``; ``None`` quando o PHP trataria como não interpretável."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme and url[len(parts.scheme) :].startswith("://") and not parts.netloc:
        return None
    return _host_of(parts.netloc), parts.path, parts.query


def _host_of(netloc: str) -> str:
    """Host sem usuário nem porta e sem alterar a caixa; IPv6 mantém os colchetes."""
    host_port = netloc.rpartition("@")[2]
    if host_port.startswith("["):
        end = host_port.find("]")
        return host_port if end == -1 else host_port[: end + 1]
    return host_port.split(":", 1)[0]


def _ascii_lower(text: str) -> str:
    """Minúsculas só para A-Z, como o ``strtolower`` do PHP 8 (acentos ficam como estão)."""
    return "".join(char.lower() if "A" <= char <= "Z" else char for char in text)


def _php_urldecode(text: str) -> bytes:
    """``urldecode`` do PHP: ``+`` vira espaço e ``%XX`` vira o byte, sem validar UTF-8."""
    return unquote_to_bytes(text.replace("+", " "))


def _php_urlencode(data: bytes) -> str:
    """``urlencode`` do PHP: espaço vira ``+`` e ``~`` é escapado (``%7E``)."""
    return quote_from_bytes(data, safe="").replace("%20", "+").replace("~", "%7E")


def _canonical_query(query: str) -> str:
    """Filtra, agrupa por chave, ordena e reescreve a query string."""
    grouped: dict[bytes, list[bytes]] = {}
    for part in query.split("&"):
        key, separator, value = part.partition("=")
        if not separator or value == "":
            continue
        decoded_key = _php_urldecode(key)
        if decoded_key.lower() in _TRACKING_PARAMS:
            continue
        grouped.setdefault(decoded_key, []).append(_php_urldecode(value))

    pairs = [
        f"{_php_urlencode(key)}={_php_urlencode(value)}"
        for key in sorted(grouped, key=cmp_to_key(_compare_keys))
        for value in grouped[key]
    ]
    return "&".join(pairs)


def _compare_keys(left: bytes, right: bytes) -> int:
    """Ordem do ``ksort`` do PHP: inteiros canônicos por valor, o resto byte a byte."""
    if _PHP_INT_KEY.match(left) and _PHP_INT_KEY.match(right):
        return (int(left) > int(right)) - (int(left) < int(right))
    return (left > right) - (left < right)
