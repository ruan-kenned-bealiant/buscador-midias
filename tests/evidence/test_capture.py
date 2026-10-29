import hashlib
from datetime import UTC, datetime

from buscador_midias.domain.records import FindingUrl
from buscador_midias.evidence.capture import EvidenceCapturer, inject_base_href
from buscador_midias.extraction.fetch import PageFetch, PageStatus
from buscador_midias.net.fetcher import FetchResult

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
URL = "https://g1.globo.com/politica/noticia/2026/10/15/pf-prende-empresario-por-fraude-em-licitacao.ghtml"
TEXT = (
    "A Polícia Federal prendeu o empresário João Silva nesta quarta-feira por suspeita de fraude "
    "em licitação em contratos da prefeitura. Segundo a investigação, o esquema desviou recursos. "
)


def html(
    title: str = "PF prende empresário por fraude em licitação - G1", paragraphs: int = 8
) -> bytes:
    body = "".join(f"<p>{TEXT}</p>" for _ in range(paragraphs))
    return (
        f'<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>{title}</title>'
        f'<meta property="og:type" content="article"></head>'
        f"<body><article><h1>{title}</h1>{body}</article></body></html>"
    ).encode()


def finding(entities: tuple[str, ...] = ("João Silva",), adverse: bool = True) -> FindingUrl:
    return FindingUrl("h" * 64, URL, None, "g1.globo.com", entities, adverse)


def direct_page(content: bytes | None = None, **kw) -> PageFetch:
    base = {
        "url": URL,
        "status": PageStatus.OK,
        "http_status": 200,
        "content": content if content is not None else html(),
        "content_type": "text/html; charset=utf-8",
        "final_url": URL,
    }
    base.update(kw)
    return PageFetch(**base)


class MemoryStore:
    def __init__(self, fail: bool = False) -> None:
        self.files: dict[str, bytes] = {}
        self.fail = fail

    def put(self, url_hash: str, content: bytes) -> str:
        if self.fail:
            raise OSError("disco cheio")
        path = f"midias-ia/evidencias/{url_hash}.html"
        self.files[path] = content
        return path


class FakeBrightData:
    def __init__(
        self, content: bytes | None = None, status: int | None = 200, error: str | None = None
    ) -> None:
        self.content = content if content is not None else html()
        self.status = status
        self.error = error
        self.calls: list[str] = []

    @property
    def requests_made(self) -> int:
        return len(self.calls)

    def get(self, url: str) -> FetchResult:
        self.calls.append(url)
        if self.error or self.status is None:
            return FetchResult(url, None, b"", self.error or "falha")
        return FetchResult(url, self.status, self.content, None)


def capture(
    direct: PageFetch | None,
    *,
    store: MemoryStore | None = None,
    bright: FakeBrightData | None = None,
    reuse: bool = True,
    entities: tuple[str, ...] = ("João Silva",),
    adverse: bool = True,
):
    store = store if store is not None else MemoryStore()
    capturer = EvidenceCapturer(store, bright_data=bright, reuse=reuse, now=lambda: NOW)
    record = capturer.capture(
        finding(entities, adverse),
        direct,
        expected_headline="pf prende empresario por fraude em licitacao",
    )
    return record, store


def test_html_do_download_direto_e_reaproveitado_quando_confiavel():
    bright = FakeBrightData()

    record, store = capture(direct_page(), bright=bright)

    assert record.status == "captured"
    assert record.provider == "direct"
    assert record.rejected_reasons == ()
    assert bright.calls == []
    stored = store.files[record.path]
    assert hashlib.sha256(stored).hexdigest() == record.sha256
    assert record.size_bytes == len(stored)
    assert b'<base href="' + URL.encode() + b'">' in stored
    assert record.http_status == 200
    assert record.final_url == URL


def test_reaproveitamento_rejeitado_cai_para_o_bright_data_e_registra_o_motivo():
    thin = direct_page(html(paragraphs=1))
    bright = FakeBrightData(html(paragraphs=8))

    record, store = capture(thin, bright=bright)

    assert record.provider == "brightdata"
    assert record.status == "captured"
    assert "thin_content" in record.rejected_reasons
    assert bright.calls == [URL]
    assert record.path in store.files


def test_modo_somente_bright_data_ignora_o_html_direto():
    bright = FakeBrightData()

    record, _ = capture(direct_page(), bright=bright, reuse=False)

    assert record.provider == "brightdata"
    assert record.rejected_reasons == ("reuse_disabled",)
    assert bright.calls == [URL]


def test_sem_download_direto_o_motivo_e_o_desfecho_do_download():
    blocked = PageFetch(URL, PageStatus.BLOCKED, 403)
    bright = FakeBrightData()

    record, _ = capture(blocked, bright=bright)

    assert record.provider == "brightdata"
    assert record.rejected_reasons == ("direct_blocked",)


def test_sem_nenhum_download_direto_registra_ausencia():
    record, _ = capture(None, bright=FakeBrightData())

    assert record.rejected_reasons == ("direct_missing",)


def test_entidade_ausente_na_pagina_impede_o_reaproveitamento():
    record, _ = capture(direct_page(), bright=FakeBrightData(), entities=("Fulano de Tal",))

    # o Bright Data devolve a mesma página, que também não cita a entidade: falha de confiabilidade
    assert record.status == "failed"
    assert "entity_missing" in (record.error or "")


def test_copia_do_bright_data_tambem_precisa_ser_confiavel():
    error_page = html(title="Página não encontrada - 404 | G1", paragraphs=8)
    bright = FakeBrightData(error_page)

    record, store = capture(direct_page(html(paragraphs=1)), bright=bright)

    assert record.status == "failed"
    assert record.provider is None
    assert record.path is None
    assert "brightdata" in (record.error or "")
    assert "error_page" in (record.error or "")
    assert store.files == {}
    assert "thin_content" in record.rejected_reasons


def test_sem_bright_data_configurado_a_falha_diz_isso():
    record, _ = capture(direct_page(html(paragraphs=1)), bright=None)

    assert record.status == "failed"
    assert "Bright Data" in (record.error or "")
    assert "thin_content" in record.rejected_reasons


def test_falha_de_rede_no_bright_data_e_registrada():
    bright = FakeBrightData(error="ReadTimeout: x")

    record, _ = capture(None, bright=bright)

    assert record.status == "failed"
    assert "ReadTimeout" in (record.error or "")


def test_falha_ao_gravar_no_armazenamento_vira_falha_sem_levantar():
    record, _ = capture(direct_page(), store=MemoryStore(fail=True), bright=FakeBrightData())

    assert record.status == "failed"
    assert "armazenamento" in (record.error or "")


def test_base_href_entra_logo_depois_do_head_e_nao_duplica():
    page = b"<html><head><title>x</title></head><body></body></html>"

    once = inject_base_href(page, "https://a.com/x?a=1&b=2")

    assert once.startswith(b'<html><head><base href="https://a.com/x?a=1&amp;b=2">')
    assert inject_base_href(once, "https://a.com/x?a=1&b=2") == once


def test_base_href_sem_head_vai_no_inicio_e_aspas_sao_escapadas():
    assert inject_base_href(b"<p>oi</p>", 'https://a.com/"x"').startswith(
        b'<base href="https://a.com/&quot;x&quot;">'
    )


def test_materia_sem_fato_adverso_reaproveita_o_html_direto_sem_chamar_o_bright_data():
    bright = FakeBrightData()

    record, _ = capture(direct_page(), bright=bright, adverse=False)

    assert (record.status, record.provider) == ("captured", "direct")
    assert bright.calls == []


def test_materia_sem_fato_adverso_nunca_paga_pelo_bright_data():
    thin = direct_page(html(paragraphs=1))
    bright = FakeBrightData(html(paragraphs=8))

    record, store = capture(thin, bright=bright, adverse=False)

    assert record.status == "failed"
    assert bright.calls == []  # pagar é reservado ao achado adverso
    assert "thin_content" in record.rejected_reasons
    assert "fato adverso" in (record.error or "")
    assert store.files == {}


def test_materia_sem_fato_adverso_no_modo_somente_bright_data_nao_gasta_nada():
    bright = FakeBrightData()

    record, _ = capture(direct_page(), bright=bright, reuse=False, adverse=False)

    assert record.status == "failed"
    assert bright.calls == []
