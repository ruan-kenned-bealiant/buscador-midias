"""Validação de URLs públicas do cadastro, sem rede nem DNS."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit

from buscador_midias.catalog.store_errors import CatalogError


def clean_url(value: object, *, source: bool = False) -> str:
    """Valida URL pública HTTP(S) sem buscar conteúdo nem resolver DNS."""
    if not isinstance(value, str) or len(value) > 4096:
        raise CatalogError("Informe uma URL válida com http:// ou https://.")
    value = value.strip()
    if any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        raise CatalogError("A URL não pode conter espaços ou caracteres de controle.")
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").encode("idna").decode("ascii").lower()
        port = parts.port
    except (ValueError, UnicodeError) as error:
        raise CatalogError("URL inválida.") from error
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        raise CatalogError("Use uma URL pública HTTP(S), sem usuário ou senha.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if (
        (address is not None and not address.is_global)
        or host == "localhost"
        or host.endswith((".localhost", ".local", ".internal"))
        or "." not in host
        or host.endswith(".")
        or any(
            not label
            or label.startswith("-")
            or label.endswith("-")
            or not all(c.isalnum() or c == "-" for c in label)
            for label in host.split(".")
        )
    ):
        raise CatalogError("Informe um domínio público de notícias.")
    if source and (parts.query or parts.fragment):
        raise CatalogError("A URL do site deve ser o domínio ou seção, sem query ou fragmento.")
    netloc = host
    if port and port != (443 if parts.scheme == "https" else 80):
        netloc += f":{port}"
    path = parts.path.rstrip("/") if source else (parts.path or "/")
    return urlunsplit((parts.scheme, netloc, path, parts.query, ""))
