"""Interpretação do robots.txt: regras de acesso e sitemaps declarados."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

_HTTP_SCHEMES = ("http", "https")


def _resolve(raw: str, base_url: str | None) -> str | None:
    """Resolve um sitemap declarado; devolve ``None`` se não virar uma URL http(s) com host."""
    url = urljoin(base_url, raw.strip()) if base_url else raw.strip()
    parts = urlsplit(url)
    return url if parts.scheme in _HTTP_SCHEMES and parts.hostname else None


@dataclass(frozen=True, slots=True)
class RobotsPolicy:
    """Política de um site derivada do seu ``robots.txt``.

    Attributes:
        sitemaps: URLs declaradas em linhas ``Sitemap:``, na ordem do arquivo.
    """

    sitemaps: tuple[str, ...]
    _parser: RobotFileParser

    @classmethod
    def parse(cls, text: str, *, base_url: str | None = None) -> RobotsPolicy:
        """Interpreta o conteúdo de um ``robots.txt``.

        Sitemaps relativos (``/sitemap.xml``) são resolvidos contra ``base_url``; os que não
        resultam em uma URL http(s) (relativos sem base, esquema inválido) são descartados.

        Args:
            text: Texto do arquivo (vazio equivale a "tudo permitido").
            base_url: Origem do site, usada para resolver sitemaps relativos.

        Returns:
            A política, com regras de acesso e sitemaps declarados.
        """
        parser = RobotFileParser()
        parser.parse(text.splitlines())
        declared = (_resolve(raw, base_url) for raw in parser.site_maps() or ())
        return cls(sitemaps=tuple(url for url in declared if url), _parser=parser)

    @classmethod
    def allow_all(cls) -> RobotsPolicy:
        """Política para sites sem ``robots.txt``: tudo permitido, nenhum sitemap declarado."""
        return cls.parse("")

    def allows(self, url: str, user_agent: str) -> bool:
        """Indica se o agente pode acessar a URL.

        Args:
            url: URL a verificar.
            user_agent: Nome do nosso agente.

        Returns:
            ``True`` se nenhuma regra proíbe o acesso.
        """
        return self._parser.can_fetch(user_agent, url)
