"""HTML de trabalho comprimido, fora das mensagens e separado da evidência definitiva."""

from __future__ import annotations

import gzip
import os
import re
import tempfile
from pathlib import Path


class Artifacts:
    """Gravação atômica por hash; limite de tamanho e validação de caminho na leitura."""

    def __init__(self, root: Path | None = None) -> None:
        """Define raiz local compartilhada pelos workers da instalação."""
        configured = os.environ.get("BM_INGEST_WORK_DIR")
        self.root = (
            root
            or (
                Path(configured)
                if configured
                else Path(__file__).resolve().parents[3] / "var/ingestion/work"
            )
        ).resolve()

    def path(self, key: str) -> Path:
        """Recusa chaves de terceiros capazes de escapar do diretório de trabalho."""
        if not re.fullmatch("[a-f0-9]{64}", key):
            raise ValueError("Hash inválido")
        return self.root / (key + ".html.gz")

    def put(self, key: str, body: bytes) -> str:
        """Comprime antes do envio à próxima etapa, preservando bytes exatos."""
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(dir=self.root, suffix=".partial")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(gzip.compress(body, compresslevel=4))
            os.replace(name, target)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise
        return key

    def read(self, key: str) -> bytes:
        """Impede expansão ilimitada de um artefato alterado no disco."""
        with gzip.open(self.path(key), "rb") as stream:
            body = stream.read(16 * 1024 * 1024 + 1)
        if len(body) > 16 * 1024 * 1024:
            raise ValueError("Artefato acima do limite")
        return body

    def remove(self, key: str) -> None:
        """Libera o HTML transitório depois da gravação segura no corpus."""
        self.path(key).unlink(missing_ok=True)
