"""Modelo de domínio de uma fonte de notícias confiável."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

_HTTP_SCHEMES = ("http", "https")


def _strip_www(host: str) -> str:
    """Remove apenas o prefixo literal ``www.``; ``www12.`` e ``www3.`` fazem parte do host."""
    return host.removeprefix("www.")


@dataclass(frozen=True, slots=True)
class Site:
    """Uma fonte de notícias a ser varrida.

    Uma fonte pode ser um domínio inteiro (``path_prefix`` vazio) ou apenas uma seção dele
    (``path_prefix="/globonews"``). Nesse caso, só URLs sob o prefixo pertencem à fonte.

    Attributes:
        name: Nome de exibição da fonte.
        url: URL base normalizada, usada para requisições (mantém esquema e ``www``).
        host: Host em minúsculas, sem o prefixo ``www.``; é a identidade do domínio.
        path_prefix: Prefixo de seção sem barra final, ou ``""`` para o domínio inteiro.
        region: Região geográfica do catálogo.
        country: País por extenso.
        country_code: Código do país (ex.: ``BR``, ``INT``).
        language: Idioma principal.
        source_type: Tipo editorial (jornal, portal, agência...).
    """

    name: str
    url: str
    host: str
    path_prefix: str
    region: str
    country: str
    country_code: str
    language: str
    source_type: str

    @classmethod
    def create(
        cls,
        *,
        name: str,
        url: str,
        region: str,
        country: str,
        country_code: str,
        language: str,
        source_type: str,
    ) -> Site:
        """Cria um ``Site`` normalizando a URL.

        Args:
            name: Nome de exibição.
            url: URL absoluta ``http(s)`` da fonte; query e fragmento são descartados.
            region: Região do catálogo.
            country: País por extenso.
            country_code: Código do país.
            language: Idioma principal.
            source_type: Tipo editorial.

        Returns:
            O ``Site`` com ``url``, ``host`` e ``path_prefix`` normalizados.

        Raises:
            ValueError: Se a URL não for absoluta com esquema http/https e host.
        """
        parsed = urlsplit(url.strip())
        if parsed.scheme not in _HTTP_SCHEMES or not parsed.hostname:
            raise ValueError(f"URL inválida para a fonte {name!r}: {url!r}")

        path_prefix = parsed.path.rstrip("/")
        return cls(
            name=name,
            url=f"{parsed.scheme}://{parsed.netloc.lower()}{path_prefix}",
            host=_strip_www(parsed.hostname.lower()),
            path_prefix=path_prefix,
            region=region,
            country=country,
            country_code=country_code,
            language=language,
            source_type=source_type,
        )

    @property
    def key(self) -> str:
        """Identidade única da fonte: host + prefixo de seção (ex.: ``bbc.com/portuguese``)."""
        return f"{self.host}{self.path_prefix}"

    def owns(self, candidate_url: str) -> bool:
        """Indica se uma URL pertence a esta fonte.

        O host da URL precisa ser o da fonte (com ou sem ``www``) ou um **subdomínio** dele: sites
        publicam em hosts como ``www1.folha.uol.com.br`` enquanto o catálogo lista
        ``folha.uol.com.br``. Hosts irmãos (``oglobo.globo.com`` para ``g1.globo.com``) e o
        domínio-pai não pertencem à fonte. Para fontes de seção, o caminho precisa estar sob o
        prefixo, sem confundir ``/a`` com ``/ab``.

        Args:
            candidate_url: URL a verificar.

        Returns:
            ``True`` se a URL é http(s), do host da fonte ou de um subdomínio, e sob o prefixo.
        """
        parsed = urlsplit(candidate_url)
        if parsed.scheme not in _HTTP_SCHEMES or not parsed.hostname:
            return False
        host = _strip_www(parsed.hostname.lower())
        if host != self.host and not host.endswith(f".{self.host}"):
            return False
        if not self.path_prefix:
            return True
        return parsed.path == self.path_prefix or parsed.path.startswith(f"{self.path_prefix}/")
