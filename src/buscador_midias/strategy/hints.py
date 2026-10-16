"""Dicas de endereços por fonte, vindas de um levantamento em CSV.

A dica é um palpite: vem de uma fotografia (uma data, uma rede) e envelhece. Ela só ganha a
primeira tentativa da sondagem; quem decide é o que o endereço publica agora.
"""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from pathlib import Path

from buscador_midias.domain.site import Site

# Colunas do levantamento, na ordem em que as dicas são tentadas.
_COLUMNS = ("news_sitemap_url", "rss_url", "sitemap_url")
_DOMAIN_COLUMN = "dominio"
# Links assinados (S3 etc.) carregam credencial temporária: não se guardam nem se repetem.
_SIGNED_MARKERS = ("x-amz-", "signature=", "x-goog-signature")


class SourceHints:
    """As dicas de cada fonte, pela chave dela (host + seção)."""

    def __init__(self, by_key: Mapping[str, Sequence[str]]) -> None:
        """Cria o índice.

        Args:
            by_key: Endereços candidatos por chave de fonte (``host`` ou ``host/seção``).
        """
        self._by_key = {key: tuple(urls) for key, urls in by_key.items()}

    def for_site(self, site: Site) -> tuple[str, ...]:
        """Os endereços candidatos da fonte; só os do próprio domínio e sem credencial.

        Um endereço de outro domínio (levantamento errado, domínio sequestrado) nem é baixado.

        Args:
            site: A fonte.

        Returns:
            Os endereços, sem repetição; vazio se a fonte não tem dica.
        """
        return tuple(
            url for url in self._by_key.get(site.key, ()) if site.owns(url) and not _is_signed(url)
        )


def load_hints(path: Path) -> SourceHints:
    """Lê as dicas de um CSV do levantamento.

    O CSV precisa das colunas ``dominio``, ``news_sitemap_url``, ``rss_url`` e ``sitemap_url``; as
    demais são ignoradas.

    Args:
        path: O CSV; se não existir, não há dicas.

    Returns:
        As dicas por fonte. Linhas repetidas de um mesmo domínio somam-se, sem repetir endereços.
    """
    if not path.is_file():
        return SourceHints({})
    by_key: dict[str, list[str]] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            key = _normalize(row.get(_DOMAIN_COLUMN) or "")
            urls = by_key.setdefault(key, [])
            for column in _COLUMNS:
                url = (row.get(column) or "").strip()
                if url and url not in urls:
                    urls.append(url)
    return SourceHints(by_key)


def _normalize(domain: str) -> str:
    """Chave de fonte do domínio do levantamento: minúsculas, sem ``www.`` e sem ``/`` final."""
    return domain.strip().lower().removeprefix("www.").rstrip("/")


def _is_signed(url: str) -> bool:
    """Se o endereço carrega assinatura ou credencial temporária."""
    lowered = url.lower()
    return any(marker in lowered for marker in _SIGNED_MARKERS)
