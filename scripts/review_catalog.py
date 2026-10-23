"""Revisa o catálogo inteiro; --apply executa apenas consolidações explícitas e auditadas."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from buscador_midias.catalog.consolidation import consolidate_reviewed
from buscador_midias.catalog.review import (
    audit_catalog,
    repair_malformed_links,
    repair_url_plan,
    reviewed_plan,
)
from buscador_midias.catalog.store import CatalogStore


def main() -> None:
    """Salva diagnóstico completo, backup consistente e resumo legível sem coletar notícias."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Aplicar somente as decisões revisadas"
    )
    parser.add_argument("--database", type=Path, default=Path("var/gerenciador/catalogo.sqlite"))
    parser.add_argument("--output", type=Path, default=Path("var/gerenciador/revisao-base"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    store = CatalogStore(
        args.database, Path("var/consulta-funcionais/retrato.json"), Path("data/sites.csv")
    )
    before = audit_catalog(store)
    plan = reviewed_plan(store)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = args.output / f"antes-{stamp}.sqlite"
    correction = None
    url_repairs = []
    result = None
    if args.apply:
        if before["integrity"] != ["ok"] or before["foreign_key_violations"]:
            raise RuntimeError("Integridade inválida: nenhuma correção foi aplicada.")
        with store.connection() as src, sqlite3.connect(backup) as dst:
            src.backup(dst)
        # Ensaio em cópia consistente antes de aplicar ao catálogo em uso.
        trial_path = args.output / f"ensaio-{stamp}.sqlite"
        with sqlite3.connect(backup) as src, sqlite3.connect(trial_path) as dst:
            src.backup(dst)
        trial = CatalogStore(trial_path, store.snapshot, store.sites)
        trial_result = consolidate_reviewed(trial, reviewed_plan(trial))
        assert audit_catalog(trial)["unique_endpoint_urls"] == before["unique_endpoint_urls"]
        result = consolidate_reviewed(store, plan)
        assert result == trial_result
        # O domínio registrado excluía os três feeds oficiais do escopo de artigos.
        with store.connection() as db:
            row = db.execute(
                "SELECT * FROM sites WHERE name='Mongabay Brasil' "
                "AND url='https://www.mongabay.com.br' AND deleted_at IS NULL"
            ).fetchone()
        if row:
            old = dict(row)
            store.save(
                "source", dict(url="https://brasil.mongabay.com", version=old["version"]), old["id"]
            )
            correction = dict(
                id=old["id"],
                old_url=old["url"],
                new_url="https://brasil.mongabay.com",
                official_reference="https://brasil.mongabay.com/",
            )
    if args.apply:
        url_repairs = repair_url_plan(store)
        repair_malformed_links(store, url_repairs)
    after = audit_catalog(store)
    if args.apply:
        assert after["unique_endpoint_urls"] == before["unique_endpoint_urls"]
        assert after["integrity"] == ["ok"] and not after["foreign_key_violations"]
        # As linhas históricas das fontes incorporadas ficam intactas.
        with sqlite3.connect(backup) as old_db, store.connection() as current:
            for item in plan:
                original = old_db.execute(
                    "SELECT * FROM links WHERE source_id=? ORDER BY id", (item["source_id"],)
                ).fetchall()
                preserved = [
                    tuple(r)
                    for r in current.execute(
                        "SELECT * FROM links WHERE source_id=? ORDER BY id",
                        (item["source_id"],),
                    )
                ]
                assert original == preserved
    manifest_path = Path("var/consulta-funcionais/historico-preservado.json")
    protected = json.loads(manifest_path.read_text())["sha256"]
    mismatches = [
        path
        for path, digest in protected.items()
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest
    ]
    assert not mismatches, mismatches
    report = dict(
        generated_at=datetime.now(UTC).isoformat(),
        applied=args.apply,
        backup=str(backup) if args.apply else None,
        plan=plan,
        result=result,
        source_url_correction=correction,
        url_repairs=url_repairs,
        before=before,
        after=after,
        protected_files_unchanged=len(protected),
        official_alias_checks=[
            dict(url="https://lupa.uol.com.br/", final_url="https://www.agencialupa.org/"),
            dict(url="https://www1.folha.uol.com.br/", final_url="https://www.folha.uol.com.br/"),
        ],
    )
    (args.output / "resultado.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    fields = ["id", "name", "url", "country", "language", "endpoints"]
    with (args.output / "fontes.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(after["source_inventory"])
    source_names = {s["id"]: s["name"] for s in before["source_inventory"]}
    decisions = "\n".join(
        f"- {p['name']} → {source_names[p['target_id']]}: {p['reason']}." for p in plan
    )
    summary = f"""# Revisão do catálogo — {stamp}

Modo: {"aplicado" if args.apply else "diagnóstico sem alterações"}.

Revisão de consistência e duplicação de todas as {before["sources"]} fontes e
{before["endpoints"]} associações RSS/sitemap visíveis. Não é uma nova validação HTTP
individual de cada feed, nem altera o estado funcional histórico.

## Resultado

- Fontes: {before["sources"]} → {after["sources"]}.
- Associações de links: {before["endpoints"]} → {after["endpoints"]}.
- URLs únicas preservadas: {before["unique_endpoint_urls"]} → {after["unique_endpoint_urls"]}.
- Arquivos históricos intactos: {len(protected)}.
- Integridade SQLite: {after["integrity"]}.
- Violações de chaves estrangeiras: {len(after["foreign_key_violations"])}.
- Registros com URL/modelo inválido: {len(after["invalid_records"])}.
- Grupos com mesmo endereço de site: {len(after["same_source_addresses"])}.
- Variantes de codificação em links da mesma fonte: {len(after["encoded_url_candidates"])}.
- Links compartilhados entre fontes distintas: {len(after["shared_endpoint_urls"])} URLs.
- Fontes sem links cadastrados: {len(after["sources_without_links"])}; não foram eliminadas.

## Consolidações {"aplicadas" if args.apply else "planejadas"}

{decisions}

Endereços oficiais conferidos para aliases:
[Lupa](https://lupa.uol.com.br/) redireciona para [Agência Lupa](https://www.agencialupa.org/).
[Folha www1](https://www1.folha.uol.com.br/) redireciona para [Folha](https://www.folha.uol.com.br/).

Domínio oficial de referência para o Mongabay Brasil: https://brasil.mongabay.com/,
que corresponde ao portal oficial e aos três links já cadastrados. Provas anteriores
permanecem históricas; mudar o domínio exige novo teste para confirmação atual.

## URLs corrigidas

Espaços literais em {len(url_repairs)} caminhos foram codificados como `%20`.
Configurações corrigidas ficam sem prova atual; os valores anteriores e evidências
permanecem na auditoria.

## Mantidos separados

GloboNews, E-Investidor e Scorpio foram mantidos como marcas distintas.
DW Brasil, AP Noticias, Der Spiegel International, NHK World-Japan, The Conversation
Africa, RFI Afrique e RNZ Pacific mantêm cadastros por idioma/serviço.
Estações La 1ère de territórios distintos e ABC Pacific também permanecem separadas.
Homônimos em países diferentes (The Reporter, Le Soleil, Le Temps, Le Quotidien,
La Nation, The Standard e The Citizen) não foram unidos pelo nome.

Os RSS/sitemaps gerais compartilhados continuam associados às fontes pertinentes;
repetição de URL entre marcas/idiomas não comprova duplicação de identidade.
A busca futura deverá deduplicar downloads por URL e notícias por identidade,
preservando as associações editoriais. Este relatório não implementa essa busca.

## Preservação e arquivos

`resultado.json` contém o diagnóstico completo antes/depois, inventário de fontes,
URLs compartilhadas, decisões e backup. `fontes.csv` contém uma linha por fonte.
Cadastros incorporados foram excluídos logicamente; seus links e resultados de teste
originais permanecem intactos. Links exclusivos foram copiados sem atribuir testes
de outra fonte à configuração atual. Toda alteração está na auditoria SQLite.
"""
    (args.output / "relatorio.md").write_text(summary)
    print(
        json.dumps(
            dict(
                applied=args.apply,
                sources_before=before["sources"],
                sources_after=after["sources"],
                endpoints_before=before["endpoints"],
                endpoints_after=after["endpoints"],
                result=result,
                source_url_correction=correction,
                unique_urls=after["unique_endpoint_urls"],
                invalid_records=len(after["invalid_records"]),
                remaining_shared_urls=len(after["shared_endpoint_urls"]),
                external_hosts=len(after["external_endpoint_hosts"]),
                sources_without_links=len(after["sources_without_links"]),
                report=str(args.output / "relatorio.md"),
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
