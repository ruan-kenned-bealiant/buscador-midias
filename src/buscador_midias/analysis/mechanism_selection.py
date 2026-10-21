"""Classificação rastreável de mecanismos, sem executar ou modificar a coleta."""

from __future__ import annotations

from dataclasses import dataclass

HISTORICAL_DOCUMENT_THRESHOLD = 1000


@dataclass(frozen=True)
class MechanismEvidence:
    """Evidência observada, sem extrapolar amostras para todo o endpoint."""

    usable_http: bool
    manual_exclusion: bool = False
    xml_confirmed: bool = False
    useful_texts: int = 0
    sampled: int = 0
    sampled_audio: int = 0
    dedicated_audio_proven: bool = False
    documents: int | None = None
    pending: int = 0


def classify(evidence: MechanismEvidence) -> tuple[str, str]:
    """Propõe uso futuro preservando inconclusivos e XMLs sem novidades."""
    if not evidence.usable_http:
        return "inconclusivo", "origem_sem_url_http_reutilizavel_ou_fora_do_escopo"
    if evidence.manual_exclusion:
        return "excluir", "exclusao_manual_preexistente"
    if evidence.useful_texts:
        if evidence.pending:
            return "periodico", "texto_confirmado_percurso_parcial_preservado"
        if evidence.documents is not None and evidence.documents > HISTORICAL_DOCUMENT_THRESHOLD:
            return "periodico", "texto_confirmado_trabalho_documental_alto"
        return "frequente", "texto_confirmado_por_url_observada"
    if evidence.dedicated_audio_proven and evidence.sampled_audio:
        return "excluir", "audio_dedicado_comprovado_no_mecanismo_e_na_amostra"
    if evidence.sampled_audio:
        return "inconclusivo", "audio_na_amostra_sem_prova_de_endpoint_exclusivo"
    if evidence.sampled:
        return "inconclusivo", "amostra_sem_texto_util_confirmado"
    if evidence.xml_confirmed:
        return "periodico", "xml_interpretado_sem_amostra_textual_na_janela"
    return "inconclusivo", "evidencia_individual_insuficiente_ou_falha_de_acesso"
