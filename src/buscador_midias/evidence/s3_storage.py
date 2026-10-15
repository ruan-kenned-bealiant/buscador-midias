"""Armazenamento da evidência em S3 (ou compatível, como o MinIO) e escolha do armazenamento.

Usa as mesmas variáveis do KyX (``AWS_BUCKET``, ``AWS_ENDPOINT``, ``AWS_DEFAULT_REGION``,
``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY``, ``AWS_USE_PATH_STYLE_ENDPOINT``) e escolhe o
disco por ``MIDIAS_IA_STORAGE_DISK`` (``local`` ou ``s3``), então cada ambiente decide só pela
configuração. A chave do objeto é a mesma do disco local e do KyX:
``midias-ia/evidencias/<url_hash>.html``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Self

from buscador_midias.evidence.storage import (
    DEFAULT_PREFIX,
    SAFE_HASH,
    EvidenceStore,
    LocalEvidenceStore,
)

DISK_VAR = "MIDIAS_IA_STORAGE_DISK"
_TRUE = frozenset({"1", "true", "yes", "on"})


class StorageConfigError(ValueError):
    """Configuração de armazenamento inválida ou incompleta (a mensagem nunca traz segredos)."""


class EvidenceStorageError(OSError):
    """Falha ao gravar ou ler a evidência no provedor (sem credenciais na mensagem)."""


class S3EvidenceStore:
    """Grava as evidências em um bucket S3 (ou compatível)."""

    def __init__(self, client: Any, *, bucket: str, prefix: str = DEFAULT_PREFIX) -> None:
        """Cria o armazenamento.

        Args:
            client: Cliente S3 (``boto3.client("s3")`` ou um fake nos testes).
            bucket: Nome do bucket.
            prefix: Prefixo das chaves.
        """
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/")

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        client_factory: Callable[..., Any] | None = None,
    ) -> Self:
        """Monta o armazenamento com as variáveis ``AWS_*`` do KyX.

        Sem chave e segredo no ambiente, o ``boto3`` usa a cadeia padrão (perfil, IAM role).

        Args:
            env: Variáveis de ambiente.
            client_factory: Fábrica do cliente (injetável; padrão usa ``boto3``).

        Raises:
            StorageConfigError: Se faltar ``AWS_BUCKET``.
        """
        bucket = env.get("AWS_BUCKET")
        if not bucket:
            raise StorageConfigError("defina AWS_BUCKET para guardar a evidência no S3")
        factory = client_factory or _boto_client
        client = factory(
            endpoint_url=env.get("AWS_ENDPOINT") or None,
            region_name=env.get("AWS_DEFAULT_REGION") or None,
            aws_access_key_id=env.get("AWS_ACCESS_KEY_ID") or None,
            aws_secret_access_key=env.get("AWS_SECRET_ACCESS_KEY") or None,
            path_style=env.get("AWS_USE_PATH_STYLE_ENDPOINT", "").lower() in _TRUE,
        )
        return cls(client, bucket=bucket)

    def put(self, url_hash: str, content: bytes) -> str:
        """Grava o HTML em ``<prefixo>/<url_hash>.html`` e devolve a chave.

        Args:
            url_hash: Hash canônico da URL (só letras, dígitos, ``-`` e ``_``).
            content: Bytes do HTML.

        Returns:
            A chave gravada, igual ao caminho relativo do armazenamento local.

        Raises:
            ValueError: Se o ``url_hash`` não serve de nome de objeto.
            EvidenceStorageError: Se o provedor recusar a gravação.
        """
        if not SAFE_HASH.fullmatch(url_hash):
            raise ValueError(f"url_hash inválido para um nome de objeto: {url_hash!r}")
        key = f"{self._prefix}/{url_hash}.html"
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content,
                ContentType="text/html",
                Metadata={"sha256": hashlib.sha256(content).hexdigest()},
            )
        except Exception as error:  # noqa: BLE001 - botocore tem muitas exceções próprias
            raise EvidenceStorageError(f"falha ao gravar no S3: {type(error).__name__}") from error
        return key

    def read(self, path: str) -> bytes:
        """Lê uma evidência gravada.

        Raises:
            ValueError: Se a chave for suspeita (``..`` ou absoluta).
            EvidenceStorageError: Se o provedor recusar a leitura.
        """
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError(f"caminho inválido para um objeto de evidência: {path!r}")
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=path)
            return bytes(response["Body"].read())
        except Exception as error:  # noqa: BLE001 - botocore tem muitas exceções próprias
            raise EvidenceStorageError(f"falha ao ler do S3: {type(error).__name__}") from error


def make_evidence_store(
    env: Mapping[str, str],
    local_root: Path,
    *,
    kind: str | None = None,
    client_factory: Callable[..., Any] | None = None,
) -> EvidenceStore:
    """Escolhe o armazenamento da evidência: disco local ou S3.

    Args:
        env: Variáveis de ambiente (``MIDIAS_IA_STORAGE_DISK`` decide quando ``kind`` é omitido;
            sem ela, o padrão é ``local``).
        local_root: Pasta raiz do armazenamento local.
        kind: ``local`` ou ``s3``, para sobrepor o ambiente.
        client_factory: Fábrica do cliente S3 (testes).

    Raises:
        StorageConfigError: Disco desconhecido ou S3 sem ``AWS_BUCKET``.
    """
    disk = (kind or env.get(DISK_VAR) or "local").lower()
    if disk == "local":
        return LocalEvidenceStore(local_root)
    if disk == "s3":
        return S3EvidenceStore.from_env(env, client_factory=client_factory)
    raise StorageConfigError(f"{DISK_VAR} deve ser 'local' ou 's3' (recebi {disk!r})")


def _boto_client(
    *,
    endpoint_url: str | None,
    region_name: str | None,
    aws_access_key_id: str | None,
    aws_secret_access_key: str | None,
    path_style: bool,
) -> Any:
    """Cliente S3 real; o ``boto3`` só é necessário quando o S3 é usado (extra ``s3``)."""
    try:
        import boto3
        from botocore.config import Config
    except ImportError as error:
        raise StorageConfigError(
            "instale o extra s3 (pip install -e '.[s3]') para usar o S3"
        ) from error
    config = Config(
        s3={"addressing_style": "path"} if path_style else {},
        retries={"max_attempts": 3, "mode": "standard"},
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region_name,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        config=config,
    )
