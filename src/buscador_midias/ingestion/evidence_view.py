"""Leitura da evidência registrada no corpus, sem executar seu HTML no painel."""

from __future__ import annotations

import os
import re
from html import escape
from pathlib import Path
from typing import Any

from lxml import html as lxml_html

from buscador_midias.catalog.store import CatalogError
from buscador_midias.evidence.s3_storage import S3EvidenceStore
from buscador_midias.extraction.html import decode_html, extract_article

ROOT = Path(__file__).resolve().parents[3]
MAX_BYTES = 16 * 1024 * 1024
TYPES = {
    "html": "text/html",
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}


def preview_html(data: bytes, article_hash: str) -> bytes:
    """Exibe blocos React já capturados sem executar scripts ou alterar o original.

    Respostas transmitidas em blocos guardam a matéria em S:* até a hidratação.
    Somente esses contêineres, associados a templates de streaming, são tratados;
    outros elementos ocultos da página não são revelados.
    """
    if not data.strip():
        return data
    root: Any = lxml_html.fromstring(decode_html(data))
    if not root.xpath('//template[starts-with(@id,"B:")]'):
        return data
    candidates = []
    for node in root.xpath('//*[@hidden and starts-with(@id,"S:")]'):
        if not node.xpath(".//h1"):
            continue
        paragraphs = [
            p.text_content().strip()
            for p in node.xpath(".//p[not(ancestor::nav or ancestor::footer or ancestor::aside)]")
            if len(p.text_content().strip()) >= 40
        ]
        if sum(map(len, paragraphs)) >= 200:
            candidates.append(paragraphs)
    if not candidates:
        return data
    paragraphs = max(candidates, key=lambda ps: sum(map(len, ps)))
    content = extract_article(data)
    title = escape(content.title or "Notícia capturada")
    body = "".join("<p>" + escape(p) + "</p>" for p in paragraphs)
    original = f"/evidencias/corpus/{escape(article_hash, quote=True)}?original=1"
    page = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title}</title><style>"
        "body{max-width:800px;margin:32px auto;padding:0 24px;color:#172b29;"
        "background:#fff;font:18px/1.7 system-ui}"
        "aside{padding:16px;background:#eef4f2;font-size:14px}"
        "h1{font-size:30px;line-height:1.3}a{color:#08665c}"
        "</style></head><body><aside>Visualização de leitura gerada do HTML capturado. "
        "O site usa JavaScript para exibir os blocos de texto já presentes na captura. "
        f'<a href="{original}" download>Baixar HTML original</a>.'
        f"</aside><main><article><h1>{title}</h1>{body}</article></main></body></html>"
    )
    return page.encode("utf-8")


def read_local(path: str, roots: list[Path]) -> bytes:
    """Lê somente evidências com chave permitida, sem aceitar travessia ou links externos."""
    if not re.fullmatch(r"midias-ia/evidencias/[A-Za-z0-9_-]{1,128}\.(html|pdf|png|jpe?g)", path):
        raise CatalogError("Caminho de evidência inválido.")
    for root in roots:
        target = (root / path).resolve()
        if target.is_relative_to(root.resolve()) and target.is_file():
            with target.open("rb") as stream:
                data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise CatalogError("Evidência acima do tamanho permitido.", 413)
            return data
    raise CatalogError("Evidência não encontrada no armazenamento local.", 404)


def read_evidence(repository: Any, article_hash: str) -> tuple[str, bytes]:
    """Resolve o caminho pelo corpus; a URL não recebe caminhos locais nem chaves arbitrárias."""
    if not re.fullmatch(r"[a-f0-9]{64}", article_hash):
        raise CatalogError("Notícia inválida.", 404)
    with repository.connection() as db:
        row = db.execute(
            "SELECT c.evidencia_path,c.evidencia_status FROM ingest_articles a "
            "JOIN public.midias_ia_articles c ON c.id=a.article_id WHERE a.hash=%s",
            (article_hash,),
        ).fetchone()
    if not row or row["evidencia_status"] != "captured" or not row["evidencia_path"]:
        raise CatalogError("Esta notícia ainda não possui evidência capturada.", 404)
    path = row["evidencia_path"]
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
