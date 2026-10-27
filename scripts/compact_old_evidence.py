"""Compacta cópias HTML antigas sem alterar chaves, bytes ou registros do corpus."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOTS = [
    ROOT / f"var/{folder}/midias-ia/evidencias"
    for folder in ("evidence", "ingestion/evidence", "ingestion/daily/evidence")
]
LOCK = threading.Lock()


def compact(path: Path, cutoff: float):
    """Só retira a cópia bruta depois de reler e verificar a cópia compactada no disco."""
    if path.is_symlink() or not path.is_file():
        return None
    before = path.stat()
    if before.st_mtime >= cutoff or before.st_size > 16 * 1024 * 1024:
        return None
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    zipped = gzip.compress(body, compresslevel=3, mtime=0)
    if len(zipped) >= len(body):
        return None
    archive = path.with_suffix(path.suffix + ".gz")
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".compact-partial")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(zipped)
            stream.flush()
            os.fsync(stream.fileno())
        with gzip.open(temporary, "rb") as stream:
            assert hashlib.sha256(stream.read()).hexdigest() == digest
        current = path.stat()
        if (current.st_ino, current.st_size, current.st_mtime_ns) != (
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            return None
        os.chmod(temporary, before.st_mode & 0o777)
        os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(temporary, archive)
        # O corpus conserva .html como chave; os leitores encontram a cópia gzip.
        path.unlink()
        return dict(
            path=str(path.relative_to(ROOT)),
            sha256=digest,
            original_bytes=before.st_size,
            compressed_bytes=len(zipped),
            original_mtime_ns=before.st_mtime_ns,
        )
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    """Encerra ao obter a folga solicitada; nenhuma evidência recente é compactada."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--target-free-gib", type=int, default=12)
    parser.add_argument("--min-age-hours", type=int, default=24)
    args = parser.parse_args()
    if args.min_age_hours < 1 or args.target_free_gib < 1:
        parser.error("Informe idade mínima e espaço desejado positivos.")
    cutoff = time.time() - args.min_age_hours * 3600
    paths = sorted(
        (p for root in ROOTS for p in root.glob("*.html") if p.stat().st_mtime < cutoff),
        key=lambda p: p.stat().st_mtime,
    )
    if not args.apply:
        print("candidate_files", len(paths), "original_bytes", sum(p.stat().st_size for p in paths))
        return
    folder = ROOT / "var/maintenance" / datetime.now(UTC).strftime("space-%Y%m%d")
    folder.mkdir(parents=True, exist_ok=True)
    free_before = shutil.disk_usage(ROOT).free
    total = 0
    count = 0
    with (folder / "compressed.jsonl").open("a") as log:

        def run(path):
            if shutil.disk_usage(ROOT).free >= args.target_free_gib * 1024**3:
                return None
            return compact(path, cutoff)

        with ThreadPoolExecutor(max_workers=4) as pool:
            for result in pool.map(run, paths):
                if result is None:
                    continue
                log.write(json.dumps(result) + "\n")
                total += result["original_bytes"] - result["compressed_bytes"]
                count += 1
                if count % 1000 == 0:
                    log.flush()
                    print("files", count, "saved_gib", round(total / 1024**3, 2), flush=True)
    summary = dict(
        finished_at=datetime.now(UTC).isoformat(),
        files=count,
        min_age_hours=args.min_age_hours,
        saved_bytes=total,
        free_before=free_before,
        free_after=shutil.disk_usage(ROOT).free,
    )
    (folder / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
