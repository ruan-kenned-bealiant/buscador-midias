"""Permite executar com ``python -m buscador_midias``."""

import sys

from buscador_midias.cli import main

if sys.argv[1:2] == ["ingest"]:
    from buscador_midias.ingestion.runtime import main as ingest_main

    raise SystemExit(ingest_main(sys.argv[2:]))

if sys.argv[1:2] == ["pipeline"]:
    from buscador_midias.pipeline.service import main as pipeline_main

    raise SystemExit(pipeline_main(sys.argv[2:]))

if sys.argv[1:2] == ["db"]:
    from buscador_midias.db.cli import main as db_main

    raise SystemExit(db_main(sys.argv[2:]))

if sys.argv[1:2] == ["review-links"]:
    from buscador_midias.catalog.review_cli import main as review_main

    raise SystemExit(review_main(sys.argv[2:]))

raise SystemExit(main())
