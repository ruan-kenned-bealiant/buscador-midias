"""Leitura da evidência registrada no corpus, sem executar seu HTML no painel."""

from __future__ import annotations

import os
import re
from pathlib import Path

from buscador_midias.catalog.store_errors import CatalogError
from buscador_midias.evidence.s3_storage import S3EvidenceStore
from buscador_midias.evidence.storage import EvidenceTooLargeError, read_stored_evidence

ROOT = Path(__file__).resolve().parents[3]
MAX_BYTES = 16 * 1024 * 1024
TYPES = {
    "html": "text/html",
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}


def read_local(path: str, roots: list[Path]) -> bytes:
    """Lê somente evidências com chave permitida, sem aceitar travessia ou links externos."""
    if not re.fullmatch(r"midias-ia/evidencias/[A-Za-z0-9_-]{1,128}\.(html|pdf|png|jpe?g)", path):
        raise CatalogError("Caminho de evidência inválido.")
    for root in roots:
        try:
            return read_stored_evidence(root, path, max_bytes=MAX_BYTES)
        except EvidenceTooLargeError as error:
            raise CatalogError(str(error), 413) from error
        except (FileNotFoundError, ValueError):
            continue
    raise CatalogError("Evidência não encontrada no armazenamento local.", 404)


def read_captured(path: str | None, status: str | None) -> tuple[str, bytes]:
    """Lê a evidência de um registro do corpus; só caminhos do formato esperado são abertos."""
    if status != "captured" or not path:
        raise CatalogError("Esta notícia ainda não possui evidência capturada.", 404)
    if not re.fullmatch(r"midias-ia/evidencias/[A-Za-z0-9_-]{1,128}\.(html|pdf|png|jpe?g)", path):
        raise CatalogError("Formato de evidência não disponível para visualização.", 404)
    if os.environ.get("MIDIAS_IA_STORAGE_DISK", "local").lower() == "s3":
        data = S3EvidenceStore.from_env(os.environ).read(path)
        if len(data) > MAX_BYTES:
            raise CatalogError("Evidência acima do tamanho permitido.", 413)
    else:
        data = read_local(
            path, [ROOT / "var/ingestion/daily/evidence", ROOT / "var/ingestion/evidence"]
        )
    return TYPES[path.rsplit(".", 1)[-1]], data
