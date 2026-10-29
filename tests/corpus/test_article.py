from datetime import UTC, datetime

import pytest

from buscador_midias.corpus.article import (
    SOURCE,
    build_corpus_article,
    map_entity_type,
    map_role,
    normalize_entity_name,
)
from buscador_midias.domain.site import Site
from buscador_midias.domain.records import CorpusCandidate

PUBLISHED = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


def candidate(**kw) -> CorpusCandidate:
    base = {
        "url_hash": "ab" * 32,
        "url": "https://www.correio24horas.com.br/brasil/delegada-e-presa-1026",
        "site_key": "correio24horas.com.br",
        "title": "Delegada é presa suspeita de receber dinheiro do PCC",
        "description": "Operação Sinalagma",
        "lede": "Uma delegada da Polícia Civil do Pará foi presa nesta quinta-feira.",
        "published_at": PUBLISHED,
        "language": "pt-BR",
        "themes": ("criminal", "pldft"),
        "entities": (
            {"name": "Maria Virgínia Grimwood Pinto", "type": "PF", "role": "investigado"},
            {"name": "Primeiro Comando da Capital (PCC)", "type": "PJ", "role": "citado"},
        ),
        "adverse_fact": "Delegada presa por vazar informações ao PCC",
        "summary": "A delegada foi presa em Brasília.",
        "confidence": 0.9,
        "basis": "text",
        "model": "google/gemini-2.5-flash-lite",
        "evidence_status": "captured",
        "evidence_path": "midias-ia/evidencias/ab.html",
        "evidence_attempts": 1,
        "evidence_provider": "direct",
        "adverse": True,
    }
    base.update(kw)
    return CorpusCandidate(**base)


def site() -> Site:
    return Site.create(
        name="Correio",
        url="https://www.correio24horas.com.br",
        region="Brasil",
        country="Brasil",
        country_code="BR",
        language="Português",
        source_type="Jornal",
    )


def build(**kw):
    return build_corpus_article(candidate(**kw), site(), {"correio24horas.com.br": "Correio"})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Fraude de petição", "fraude de peticao"),
        ("  JOÃO   da   SILVA ", "joao da silva"),
        ("Müller Åström", "muller astrom"),
        ("Ексчоловік Скальницької", "ekscholovik skal'nits'koyi"),
        ("", ""),
    ],
)
def test_nome_normalizado_como_o_kyx_ascii_minusculo_e_espacos_unicos(raw, expected):
    assert normalize_entity_name(raw) == expected


def test_tipo_da_entidade_usa_o_vocabulario_do_corpus():
    assert map_entity_type("PF") == "PER"
    assert map_entity_type("PJ") == "ORG"
    assert map_entity_type("OUTRO") == "ORG"
    assert map_entity_type("xyz") == "ORG"


def test_papeis_usam_o_vocabulario_do_corpus():
    assert map_role("acusado", "PF") == "acusado"
    assert map_role("investigado", "PF") == "investigado"
    assert map_role("condenado", "PF") == "condenado"
    assert map_role("vitima", "PF") == "vitima"
    assert map_role("autoridade", "OUTRO") == "autoridade"
    assert map_role("citado", "PF") == "mencionado"
    assert map_role("reu", "PF") == "acusado"
    assert map_role("responsavel", "PJ") == "empresa_envolvida"
    assert map_role("responsavel", "PF") == "acusado"
    assert map_role("testemunha", "PF") == "outro"
    assert map_role("qualquer coisa", "PF") == "outro"


def test_artigo_leva_texto_da_pagina_e_metadados_da_extracao():
    article = build()

    assert article.url == "https://www.correio24horas.com.br/brasil/delegada-e-presa-1026"
    assert article.url_hash == "ab" * 32
    assert article.title == "Delegada é presa suspeita de receber dinheiro do PCC"
    assert article.description == "Operação Sinalagma"
    assert article.body.startswith("Uma delegada")
    assert article.language == "pt-BR"
    assert article.published_at == PUBLISHED


def test_dominio_fonte_e_pais_vem_da_url_e_do_catalogo():
    article = build()

    assert article.domain == article.source_domain == "correio24horas.com.br"
    assert article.source_name == "Correio"
    assert article.source_country == "BR"
    assert article.source_tier == "trusted"


def test_sem_nome_de_fonte_conhecido_fica_nulo():
    article = build_corpus_article(candidate(), site(), {})

    assert article.source_name is None


def test_campos_da_ia_seguem_o_formato_do_kyx():
    article = build()

    assert article.ai_verdict == "relevante"
    assert article.ai_score == 0.9
    assert article.ai_reason == "Delegada presa por vazar informações ao PCC"
    assert article.ai_summary == article.summary == "A delegada foi presa em Brasília."
    assert article.ai_model == "google/gemini-2.5-flash-lite"
    assert article.first_seen_by == SOURCE == "buscador-midias"
    assert article.origin == "crawler"


def test_entidades_mapeadas_e_com_o_nome_normalizado():
    article = build()

    first, second = article.entities
    assert (first.name, first.name_norm, first.entity_type, first.role) == (
        "Maria Virgínia Grimwood Pinto",
        "maria virginia grimwood pinto",
        "PER",
        "investigado",
    )
    assert (second.entity_type, second.role) == ("ORG", "mencionado")
    assert (
        first.context == "Delegada presa por vazar informações ao PCC"
    )  # papel adverso carrega o fato
    assert second.context is None


def test_entidades_repetidas_pelo_nome_normalizado_viram_uma_a_adversa_vence():
    entities = (
        {"name": "João Silva", "type": "PF", "role": "citado"},
        {"name": "JOAO SILVA", "type": "PF", "role": "acusado"},
    )

    article = build(entities=entities)

    assert [(e.name_norm, e.role) for e in article.entities] == [("joao silva", "acusado")]


def test_temas_viram_palavras_chave_no_formato_do_kyx():
    article = build(
        themes=(
            "criminal",
            "pldft",
            "corrupcao",
            "etica_integridade",
            "ciber_seguranca",
            "processos_judiciais",
        )
    )

    assert [k.keyword for k in article.keywords] == [
        "criminal",
        "pld/ft",
        "corrupção",
        "ética e integridade",
        "ciber segurança",
        "processos judiciais",
    ]
    assert article.keywords[0].category == "criminal"
    assert article.keywords[1].category == "pldft"
    assert all(k.field == "analysis" and k.weight == 0.9 for k in article.keywords)


def test_tema_desconhecido_e_ignorado():
    assert [k.keyword for k in build(themes=("criminal", "inventado")).keywords] == ["criminal"]


@pytest.mark.parametrize(
    ("status", "path", "kind", "state"),
    [
        ("captured", "midias-ia/evidencias/ab.html", "html", "captured"),
        ("failed", None, None, "failed"),
        (None, None, None, "pending"),
    ],
)
def test_estado_da_evidencia_segue_o_do_kyx(status, path, kind, state):
    article = build(evidence_status=status, evidence_path=path, evidence_attempts=2)

    assert article.evidence.status == state
    assert article.evidence.path == path
    assert article.evidence.kind == kind
    assert article.evidence.attempts == (2 if status else 0)


def test_sem_catalogo_e_sem_regiao_de_idioma_o_pais_fica_nulo():
    article = build_corpus_article(candidate(language="pt"), None, {})

    assert article.source_country is None


def neutral(**kw) -> CorpusCandidate:
    """Matéria sem fato adverso: só tem entidade nomeada (ex.: notícia da vida pessoal)."""
    base = {
        "themes": (),
        "adverse_fact": "",
        "adverse": False,
        "entities": ({"name": "Vinícius Júnior", "type": "PF", "role": "outro"},),
    }
    base.update(kw)
    return candidate(**base)


def test_materia_neutra_ganha_a_palavra_chave_diversos():
    article = build_corpus_article(neutral(), site(), {})

    assert [(k.keyword, k.category) for k in article.keywords] == [("diversos", "diversos")]


def test_materia_adversa_com_tema_nao_ganha_diversos():
    article = build_corpus_article(candidate(), site(), {})

    assert "diversos" not in [k.keyword for k in article.keywords]


def test_materia_adversa_so_com_tema_desconhecido_cai_em_diversos():
    article = build_corpus_article(candidate(themes=("tema-novo",)), site(), {})

    assert [k.keyword for k in article.keywords] == ["diversos"]


def test_veredito_geral_so_diz_relevante_para_fato_adverso():
    assert build_corpus_article(candidate(), site(), {}).ai_verdict == "relevante"

    article = build_corpus_article(neutral(), site(), {})

    assert article.ai_verdict == "incerto"  # o Treatment do KyX decide por entidade e tema
    assert article.ai_reason == ""


def test_entidade_neutra_vai_para_o_corpus_com_papel_outro():
    [entity] = build_corpus_article(neutral(), site(), {}).entities

    assert (entity.name_norm, entity.entity_type, entity.role) == (
        "vinicius junior",
        "PER",
        "outro",
    )
    assert entity.context is None


def test_category_and_country_follow_candidate_through_to_corpus():
    article = build_corpus_article(
        candidate(content_category="opinion", article_country="PT", language="pt"),
        None,
        {},
        source_countries={"correio24horas.com.br": "BR"},
    )
    assert article.content_category == "opinion"
    assert (article.source_country, article.source_country_method) == ("PT", "article")
    article = build_corpus_article(
        candidate(content_category="weather", language="pt-BR", article_country="CN"),
        None,
        {},
        source_countries={"correio24horas.com.br": "PT"},
    )
    assert article.content_category == "weather"
    assert (article.source_country, article.source_country_method) == ("BR", "language")
