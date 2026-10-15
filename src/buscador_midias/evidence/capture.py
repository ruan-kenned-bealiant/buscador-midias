"""Captura de evidência: reaproveita o HTML do download direto só se for confiável.

Ordem de decisão para cada achado:

1. Se o reaproveitamento está ligado e o download direto da matéria deu certo, o HTML passa pela
   avaliação de confiabilidade (``assess_evidence``). Se passar, vira a evidência (``direct``).
2. Se não passar (ou o download direto falhou, ou o reaproveitamento está desligado), a página é
   capturada pelo Bright Data e **passa pela mesma avaliação**: uma cópia do provedor também pode
   ser uma página de erro. Se passar, vira a evidência (``brightdata``).
3. Se nada passar, a captura fica ``failed`` com o motivo, para nova tentativa.

O Bright Data é pago, então só entra para achados adversos (``FindingUrl.adverse``). A matéria que
só tem entidade, sem fato adverso, fica com o HTML do download direto ou sem evidência.

Em todos os casos o motivo da rejeição do reaproveitamento fica gravado, e o hash do conteúdo
gravado serve de selo de integridade.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import re
from collections.abc import Callable
from datetime import datetime

from buscador_midias.evidence.storage import EvidenceStore
from buscador_midias.extraction.fetch import PageFetch, PageStatus
from buscador_midias.extraction.reliability import PageSnapshot, assess_evidence
from buscador_midias.net.fetcher import Fetcher
from buscador_midias.state.store import EvidenceRecord, FindingUrl

_HEAD = re.compile(rb"<head[^>]*>", re.IGNORECASE)
_NOT_PAID = "brightdata dispensado: matéria sem fato adverso (o serviço pago é só para adversos)"


class EvidenceCapturer:
    """Decide a origem da evidência de cada achado e a grava no armazenamento."""

    def __init__(
        self,
        store: EvidenceStore,
        *,
        bright_data: Fetcher | None,
        reuse: bool,
        now: Callable[[], datetime],
    ) -> None:
        """Cria o capturador.

        Args:
            store: Armazenamento das evidências.
            bright_data: Fetcher do Bright Data (``None`` se não configurado).
            reuse: Se ``True``, tenta reaproveitar o HTML do download direto antes de pagar.
            now: Relógio (a extração descarta datas futuras).
        """
        self._store = store
        self._bright_data = bright_data
        self._reuse = reuse
        self._now = now

    def capture(
        self, finding: FindingUrl, direct: PageFetch | None, *, expected_headline: str
    ) -> EvidenceRecord:
        """Captura a evidência de uma matéria.

        Args:
            finding: A matéria (URL, entidades que a página precisa mencionar e se é adversa).
            direct: Resultado do download direto, se houve.
            expected_headline: Manchete esperada (título do sitemap ou slug).

        Returns:
            O estado da evidência: ``captured`` (com caminho, hash e origem) ou ``failed``.
        """
        rejected: tuple[str, ...] = ()
        if not self._reuse:
            rejected = ("reuse_disabled",)
        elif direct is None:
            rejected = ("direct_missing",)
        elif direct.status is not PageStatus.OK:
            rejected = (f"direct_{direct.status.value}",)
        else:
            snapshot = PageSnapshot(
                url=finding.url,
                status=direct.http_status,
                content_type=direct.content_type,
                final_url=direct.final_url,
                html=direct.content,
            )
            verdict = assess_evidence(
                snapshot,
                expected_headline=expected_headline,
                must_mention=finding.entity_names,
                now=self._now(),
            )
            if verdict.ok:
                return self._save(finding, "direct", snapshot, rejected_reasons=())
            rejected = verdict.reasons
        if not finding.adverse:
            return _failed(finding, rejected, _NOT_PAID)
        return self._from_bright_data(finding, expected_headline, rejected)

    def _from_bright_data(
        self, finding: FindingUrl, expected_headline: str, rejected: tuple[str, ...]
    ) -> EvidenceRecord:
        """Captura pelo Bright Data e confere a cópia com os mesmos critérios."""
        if self._bright_data is None:
            return _failed(finding, rejected, "Bright Data indisponível (não configurado)")
        result = self._bright_data.get(finding.url)
        if result.status is None or result.error is not None:
            return _failed(finding, rejected, f"brightdata: {result.error or 'sem resposta'}")
        snapshot = PageSnapshot(
            url=finding.url,
            status=result.status,
            content_type=result.content_type,
            final_url=result.final_url,
            html=result.content,
        )
        verdict = assess_evidence(
            snapshot,
            expected_headline=expected_headline,
            must_mention=finding.entity_names,
            now=self._now(),
        )
        if not verdict.ok:
            return _failed(finding, rejected, f"brightdata: {','.join(verdict.reasons)}")
        return self._save(finding, "brightdata", snapshot, rejected_reasons=rejected)

    def _save(
        self,
        finding: FindingUrl,
        provider: str,
        snapshot: PageSnapshot,
        *,
        rejected_reasons: tuple[str, ...],
    ) -> EvidenceRecord:
        """Grava o HTML (com ``<base href>``) e devolve o registro, ou a falha de gravação."""
        content = inject_base_href(snapshot.html, finding.url)
        try:
            path = self._store.put(finding.url_hash, content)
        except (OSError, ValueError) as error:
            return _failed(
                finding, rejected_reasons, f"armazenamento: {type(error).__name__}: {error}"
            )
        return EvidenceRecord(
            url_hash=finding.url_hash,
            status="captured",
            provider=provider,
            path=path,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            content_type=snapshot.content_type,
            http_status=snapshot.status,
            final_url=snapshot.final_url,
            rejected_reasons=rejected_reasons,
            error=None,
        )


def inject_base_href(content: bytes, url: str) -> bytes:
    """Insere ``<base href="url">`` logo depois de ``<head>``, para a página abrir com os recursos.

    Mesma ideia do KyX (``injectBaseHref``). Não duplica se a página já tem ``<base>``; sem
    ``<head>``, a tag vai no início.

    Args:
        content: HTML bruto.
        url: URL original da matéria.

    Returns:
        O HTML com a tag.
    """
    if b"<base " in content[:20000].lower():
        return content
    tag = f'<base href="{html_lib.escape(url, quote=True)}">'.encode()
    match = _HEAD.search(content)
    if match is None:
        return tag + content
    return content[: match.end()] + tag + content[match.end() :]


def _failed(finding: FindingUrl, rejected: tuple[str, ...], error: str) -> EvidenceRecord:
    """Registro de uma captura que não deu certo."""
    return EvidenceRecord(
        url_hash=finding.url_hash,
        status="failed",
        provider=None,
        path=None,
        sha256=None,
        size_bytes=0,
        content_type=None,
        http_status=None,
        final_url=None,
        rejected_reasons=rejected,
        error=error,
    )
