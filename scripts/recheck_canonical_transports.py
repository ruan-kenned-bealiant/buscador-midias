"""Uma conferência finita dos sete cadastros canônicos ainda no transporte padrão."""

import sys
import uuid

from psycopg.rows import dict_row

import recover_all_transports as recovery
from buscador_midias.catalog.postgres import connect_postgres
from map_editorial_priority import credentials


def main():
    """Retoma sete provas canônicas e aplica somente as aprovadas."""
    original = recovery.OUT
    out = original / "canonical-recheck"
    manifest_path = out / "manifest.json.gz"
    if manifest_path.exists():
        manifest = recovery.read_gz(manifest_path)
    else:
        decisions = [recovery.read_gz(p) for p in (original / "applied").glob("*.json.gz")]
        preferred = {
            d["duplicate_link_id"]: d["profile"]
            for d in decisions
            if d["action"] == "duplicate_preserved"
        }
        with connect_postgres(credentials()) as db:
            db.row_factory = dict_row
            rows = db.execute(
                """SELECT l.id,l.url,l.kind,l.model,l.version,l.source_id,l.active,l.deleted_at,
                l.last_test->>'outcome' previous_outcome,s.name source_name,s.url source_url,
                s.version source_version,s.active source_active
                FROM midias_manager.links l JOIN midias_manager.sites s ON s.id=l.source_id
                WHERE l.id=ANY(%s) AND l.access_transport='direct' AND l.active=1 AND s.active=1
                AND l.deleted_at IS NULL AND s.deleted_at IS NULL""",
                (list(preferred),),
            ).fetchall()
            models = db.execute("SELECT * FROM midias_manager.mechanisms").fetchall()
        manifest = dict(
            id=uuid.uuid4().hex,
            created_at=recovery.stamp(),
            links=rows,
            models=models,
            preferred=preferred,
            parent_run=recovery.read_gz(original / "manifest.json.gz")["id"],
        )
        recovery.write_gz(manifest_path, manifest)
        manifest = recovery.read_gz(manifest_path)
    recovery.OUT = out
    models = {m["model"]: m for m in manifest["models"]}
    # São sete links conhecidos; acesso sequencial para não disputar o mesmo host.
    for item in manifest["links"]:
        row = recovery.inspect_link(item, models, manifest["preferred"].get(str(item["id"])))
        print(item["id"], row["confirmed"], row.get("profile"), flush=True)
    import apply_transport_recovery as application

    application.OUT = out
    sys.argv = ["apply_transport_recovery.py", "--apply"]
    application.main()
    print("CANONICAL_RECHECK_COMPLETE", flush=True)


if __name__ == "__main__":
    main()
