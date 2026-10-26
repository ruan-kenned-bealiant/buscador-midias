"""Armazenamento da evidência: porta e implementação em disco local.

O caminho segue o do KyX (``midias-ia/evidencias/<url_hash>.html``) para que o corpus aponte para o
mesmo lugar. Trocar o disco local por um bucket é só implementar a mesma porta.
"""

from __future__ import annotations

import gzip
import os
import re
import tempfile
from pathlib import Path
from typing import Protocol

DEFAULT_PREFIX = "midias-ia/evidencias"
SAFE_HASH = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class EvidenceTooLargeError(ValueError):
    """A cópia salva excede o limite de leitura após descompressão."""


def read_stored_evidence(root: Path, path: str, *, max_bytes: int = 16 * 1024 * 1024) -> bytes:
    """Lê bytes originais ou sua cópia gzip, mantendo confinamento e limite de tamanho."""
    for suffix in ("", ".gz"):
        target = (root / (path + suffix)).resolve()
        if not target.is_relative_to(root.resolve()):
            raise ValueError(f"caminho fora da raiz de evidências: {path!r}")
        if not target.is_file():
            continue
        opener = gzip.open if suffix else open
        with opener(target, "rb") as stream:
            data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise EvidenceTooLargeError("Evidência acima do tamanho permitido.")
        return data
    raise FileNotFoundError(path)


class EvidenceStore(Protocol):
    """Porta: grava o HTML de uma evidência e devolve o caminho relativo gravado."""

    def put(self, url_hash: str, content: bytes) -> str:
        """Grava ``content`` e devolve o caminho relativo."""
        ...


class LocalEvidenceStore:
    """Grava as evidências em uma pasta local, de forma atômica."""

    def __init__(self, root: Path, *, prefix: str = DEFAULT_PREFIX) -> None:
        """Cria o armazenamento.

        Args:
            root: Pasta raiz (criada sob demanda).
            prefix: Subpasta relativa onde ficam os arquivos.
        """
        self._root = root
        self._prefix = prefix.strip("/")

    def put(self, url_hash: str, content: bytes) -> str:
        """Grava o HTML em ``<prefixo>/<url_hash>.html``, substituindo o anterior.

        A escrita vai para um arquivo temporário e só então é renomeada, então um erro no meio
        nunca deixa uma evidência pela metade.

        Args:
            url_hash: Hash canônico da URL (só letras, dígitos, ``-`` e ``_``).
            content: Bytes do HTML.

        Returns:
            O caminho relativo gravado.

        Raises:
            ValueError: Se o ``url_hash`` poderia escapar da pasta.
        """
        if not SAFE_HASH.fullmatch(url_hash):
            raise ValueError(f"url_hash inválido para um nome de arquivo: {url_hash!r}")
        relative = f"{self._prefix}/{url_hash}.html"
        target = self._root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, suffix=".partial")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return relative

    def read(self, path: str) -> bytes:
        """Lê uma evidência gravada.

        Raises:
            ValueError: Se o caminho sair da pasta raiz.
        """
        target = (self._root / path).resolve()
        if not target.is_relative_to(self._root.resolve()):
            raise ValueError(f"caminho fora da raiz de evidências: {path!r}")
        return read_stored_evidence(self._root, path)
