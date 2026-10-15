import hashlib
from pathlib import Path

import pytest

from buscador_midias.evidence.s3_storage import (
    EvidenceStorageError,
    S3EvidenceStore,
    StorageConfigError,
    make_evidence_store,
)
from buscador_midias.evidence.storage import LocalEvidenceStore

HASH = "ab" * 32


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict] = {}
        self.fail: Exception | None = None

    def put_object(self, **kwargs):
        if self.fail:
            raise self.fail
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = kwargs
        return {}

    def get_object(self, *, Bucket, Key):  # noqa: N803 - assinatura do boto3
        if self.fail:
            raise self.fail
        return {"Body": FakeBody(self.objects[(Bucket, Key)]["Body"])}


def test_grava_no_bucket_com_o_mesmo_caminho_do_kyx_e_tipo_de_conteudo():
    client = FakeS3()
    store = S3EvidenceStore(client, bucket="bucket-x")

    path = store.put(HASH, b"<html>x</html>")

    assert path == f"midias-ia/evidencias/{HASH}.html"
    sent = client.objects[("bucket-x", path)]
    assert sent["Body"] == b"<html>x</html>"
    assert sent["ContentType"] == "text/html"
    assert sent["Metadata"]["sha256"] == hashlib.sha256(b"<html>x</html>").hexdigest()


def test_prefixo_e_configuravel():
    client = FakeS3()

    path = S3EvidenceStore(client, bucket="b", prefix="/outro/prefixo/").put(HASH, b"x")

    assert path == f"outro/prefixo/{HASH}.html"


@pytest.mark.parametrize("bad", ["../fora", "a/b", "", "a b", ".."])
def test_hash_inseguro_e_recusado(bad):
    client = FakeS3()

    with pytest.raises(ValueError, match="url_hash"):
        S3EvidenceStore(client, bucket="b").put(bad, b"x")

    assert client.objects == {}


def test_le_de_volta_o_que_gravou():
    client = FakeS3()
    store = S3EvidenceStore(client, bucket="b")
    path = store.put(HASH, b"conteudo")

    assert store.read(path) == b"conteudo"


@pytest.mark.parametrize("bad", ["../x.html", "/abs.html", "a/../b.html"])
def test_leitura_recusa_caminho_suspeito(bad):
    with pytest.raises(ValueError, match="caminho"):
        S3EvidenceStore(FakeS3(), bucket="b").read(bad)


def test_falha_do_provedor_vira_erro_de_armazenamento_que_o_capturador_trata_sem_vazar_segredos():
    client = FakeS3()
    client.fail = RuntimeError("AccessDenied AKIASECRETO123")

    with pytest.raises(EvidenceStorageError) as error:
        S3EvidenceStore(client, bucket="b").put(HASH, b"x")

    assert isinstance(error.value, OSError)  # o capturador captura OSError
    assert "AKIASECRETO123" not in str(error.value)
    assert "RuntimeError" in str(error.value)


ENV = {
    "AWS_BUCKET": "teste-private",
    "AWS_ENDPOINT": "http://minio:9000/",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "minioadmin",
    "AWS_SECRET_ACCESS_KEY": "segredo",
    "AWS_USE_PATH_STYLE_ENDPOINT": "true",
}


def test_from_env_usa_as_mesmas_variaveis_do_kyx():
    seen: dict = {}

    def factory(**kwargs):
        seen.update(kwargs)
        return FakeS3()

    store = S3EvidenceStore.from_env(ENV, client_factory=factory)

    assert seen == {
        "endpoint_url": "http://minio:9000/",
        "region_name": "us-east-1",
        "aws_access_key_id": "minioadmin",
        "aws_secret_access_key": "segredo",
        "path_style": True,
    }
    assert store.put(HASH, b"x").endswith(f"{HASH}.html")


def test_from_env_sem_endpoint_nem_path_style_usa_a_aws_padrao():
    seen: dict = {}

    def factory(**kwargs):
        seen.update(kwargs)
        return FakeS3()

    S3EvidenceStore.from_env(
        {"AWS_BUCKET": "b", "AWS_DEFAULT_REGION": "sa-east-1"}, client_factory=factory
    )

    assert seen["endpoint_url"] is None
    assert seen["path_style"] is False
    assert seen["aws_access_key_id"] is None  # cadeia padrão de credenciais (perfil, IAM role)


def test_from_env_sem_bucket_nomeia_a_variavel_e_nao_vaza_segredos():
    with pytest.raises(StorageConfigError, match="AWS_BUCKET") as error:
        S3EvidenceStore.from_env(
            {"AWS_SECRET_ACCESS_KEY": "segredo"}, client_factory=lambda **k: FakeS3()
        )

    assert "segredo" not in str(error.value)


def test_escolha_do_armazenamento_pelo_disco_do_kyx(tmp_path: Path):
    s3 = make_evidence_store(
        {**ENV, "MIDIAS_IA_STORAGE_DISK": "s3"}, tmp_path, client_factory=lambda **k: FakeS3()
    )
    local = make_evidence_store({"MIDIAS_IA_STORAGE_DISK": "local"}, tmp_path)
    default = make_evidence_store({}, tmp_path)

    assert isinstance(s3, S3EvidenceStore)
    assert isinstance(local, LocalEvidenceStore)
    assert isinstance(default, LocalEvidenceStore)


def test_escolha_explicita_vence_o_ambiente(tmp_path: Path):
    forced = make_evidence_store(
        {**ENV, "MIDIAS_IA_STORAGE_DISK": "local"},
        tmp_path,
        kind="s3",
        client_factory=lambda **k: FakeS3(),
    )
    local = make_evidence_store({**ENV, "MIDIAS_IA_STORAGE_DISK": "s3"}, tmp_path, kind="local")

    assert isinstance(forced, S3EvidenceStore)
    assert isinstance(local, LocalEvidenceStore)


def test_disco_desconhecido_e_erro_claro(tmp_path: Path):
    with pytest.raises(StorageConfigError, match="MIDIAS_IA_STORAGE_DISK"):
        make_evidence_store({"MIDIAS_IA_STORAGE_DISK": "ftp"}, tmp_path)
