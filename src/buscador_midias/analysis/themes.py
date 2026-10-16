"""Temas que a análise de contexto reconhece (slugs de ``midias_ia_theme_prompts``)."""

from __future__ import annotations

# Temas aceitos na resposta do modelo (slugs de ``midias_ia_theme_prompts``; "imagem" vai em
# "reputacional"; cadastral, bens e entidades associadas não decidem relevância sozinhos).
THEME_SLUGS: tuple[str, ...] = (
    "corrupcao",
    "financeiro",
    "trabalhista",
    "ambiental",
    "fiscal",
    "criminal",
    "reputacional",
    "etica_integridade",
    "pldft",
    "processos_judiciais",
    "ciber_seguranca",
)
