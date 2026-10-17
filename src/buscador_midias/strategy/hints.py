"""Dicas de endereços por fonte, vindas de um levantamento em CSV.

A dica é um palpite: vem de uma fotografia (uma data, uma rede) e envelhece. Ela só ganha a
primeira tentativa da sondagem; quem decide é o que o endereço publica agora.
"""

from __future__ import annotations

import codecs
import csv
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import TextIO

from buscador_midias.domain.site import Site

# Colunas do levantamento, na ordem em que as dicas são tentadas.
_COLUMNS = ("news_sitemap_url", "rss_url", "sitemap_url")
_DOMAIN_COLUMN = "dominio"
# Colunas do levantamento que só servem de referência na conferência (nome interno -> coluna).
_REFERENCE_COLUMNS = {
    "categoria": "categoria",
    "estrategia": "estrategia_recomendada",
    "itens_24h_feed": "itens_24h_estimado_feed",
    "gdelt_24h": "gdelt_artigos_24h_estimado",
}
# Links assinados (S3 etc.) carregam credencial temporária: não se guardam nem se repetem.
_SIGNED_MARKERS = ("x-amz-", "signature=", "x-goog-signature")


class SourceHints:
    """As dicas de cada fonte, pela chave dela (host + seção)."""

    def __init__(
        self,
        by_key: Mapping[str, Sequence[str]],
        references: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        """Cria o índice.

        Args:
            by_key: Endereços candidatos por chave de fonte (``host`` ou ``host/seção``).
            references: O que o levantamento disse de cada fonte (categoria, estratégia
                recomendada, volume estimado), por chave: só para conferência, nunca decide nada.
        """
        self._by_key = {key: tuple(urls) for key, urls in by_key.items()}
        self._references = {key: dict(ref) for key, ref in (references or {}).items()}

    def reference(self, site: Site) -> dict[str, str]:
        """O que o levantamento disse da fonte (vazio se ela não consta)."""
        return dict(self._references.get(site.key, {}))

    def for_site(self, site: Site) -> tuple[str, ...]:
        """Os endereços candidatos da fonte; só os do próprio domínio e sem credencial.

        Um endereço de outro domínio (levantamento errado, domínio sequestrado) nem é baixado.

        Args:
            site: A fonte.

        Returns:
            Os endereços, sem repetição; vazio se a fonte não tem dica.
        """
        return tuple(
            url
            for url in self._by_key.get(site.key, ())
            if site.owns_host(url) and not _is_signed(url)
        )


def load_hints(paths: Path | Sequence[Path]) -> SourceHints:
    """Lê as dicas de um ou mais CSVs.

    Cada CSV precisa das colunas ``dominio``, ``news_sitemap_url``, ``rss_url`` e ``sitemap_url``;
    as demais são ignoradas, exceto as de referência (``categoria``, ``estrategia_recomendada``,
    ``itens_24h_estimado_feed`` e ``gdelt_artigos_24h_estimado``), guardadas só para conferência.

    Args:
        paths: Os CSVs, na ordem de precedência; um que não existe é ignorado.

    Returns:
        As dicas por fonte. Linhas repetidas de um mesmo domínio, e arquivos diferentes, somam-se,
        sem repetir endereços.
    """
    files = [paths] if isinstance(paths, Path) else list(paths)
    by_key: dict[str, list[str]] = {}
    references: dict[str, dict[str, str]] = {}
    for path in files:
        if not path.is_file():
            continue
        with open_csv(path) as handle:
            for row in read_rows(handle):
                key = normalize_domain(row.get(_DOMAIN_COLUMN) or "")
                urls = by_key.setdefault(key, [])
                for column in _COLUMNS:
                    url = (row.get(column) or "").strip()
                    if url and url not in urls:
                        urls.append(url)
                reference = {
                    name: (row.get(column) or "").strip()
                    for name, column in _REFERENCE_COLUMNS.items()
                    if (row.get(column) or "").strip()
                }
                if reference:
                    references.setdefault(key, reference)
    return SourceHints(by_key, references)


def _invalid_byte_as_space(error: UnicodeError) -> tuple[str, int]:
    """Troca o byte inválido por um espaço (``codecs`` error handler)."""
    if isinstance(error, UnicodeDecodeError):
        return " ", error.end
    raise error


codecs.register_error("bm_space", _invalid_byte_as_space)


def open_csv(path: Path) -> TextIO:
    """Abre um CSV de apoio em UTF-8; um byte inválido (espaço Latin-1 solto) vira espaço.

    O levantamento passa por editores que alinham colunas e deixam bytes estranhos no
    preenchimento; um deles não pode derrubar a leitura de todas as dicas.
    """
    return path.open(encoding="utf-8-sig", errors="bm_space", newline="")


def read_rows(handle: TextIO) -> Iterator[dict[str, str]]:
    """Linhas do CSV com nomes de coluna e células sem os espaços de alinhamento do editor."""
    reader = csv.reader(handle)
    header = [name.strip() for name in next(reader, [])]
    for cells in reader:
        yield {name: cell.strip() for name, cell in zip(header, cells, strict=False)}


def normalize_domain(domain: str) -> str:
    """Chave de fonte do domínio do levantamento: minúsculas, sem ``www.`` e sem ``/`` final."""
    return domain.strip().lower().removeprefix("www.").rstrip("/")


def _is_signed(url: str) -> bool:
    """Se o endereço carrega assinatura ou credencial temporária."""
    lowered = url.lower()
    return any(marker in lowered for marker in _SIGNED_MARKERS)
