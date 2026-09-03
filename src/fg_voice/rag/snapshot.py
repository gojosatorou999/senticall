"""Gazetteer snapshot builder + hot-reload loader — spec §10.1 index.

Two problems this module solves:

1. **Snapshot build** (`build_snapshot()`): serialise the current
   gazetteer JSON tiers into a single versioned artifact (`snapshot.json`
   + optional FAISS `snapshot.faiss`) so a fresh worker can load
   the whole index in one file read instead of walking three JSON
   files. Bundled with a `version.txt` marker so the loader can
   detect a new snapshot atomically.

2. **Hot reload** (`SnapshotLoader.poll_and_swap()`): a background
   task polls `version.txt`; when the marker changes, load the new
   snapshot into a temp `Gazetteer`, then atomically swap the
   resolver's `.gazetteer` reference. In-flight queries against the
   old instance keep working (Python's GC frees it when the last
   reference drops); new queries see the new one.

Design constraints:
- Numpy-KNN fallback when `faiss-cpu` is not installed (the `[rag]`
  extras are optional). The fallback loses vector-search performance
  but keeps functional parity — a full lexical resolve still works.
- No S3 dep at this layer; the caller (main.py at boot, cron worker
  at snapshot-refresh time) is responsible for pulling snapshots
  down from S3 into the local snapshot dir. This keeps `rag/` free
  of aioboto3 imports.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from fg_voice.obs.logging import get_logger
from fg_voice.rag.gazetteer import Gazetteer, GazetteerEntry, build_gazetteer

log = get_logger(__name__)


SNAPSHOT_FILENAME = "snapshot.json"
VERSION_FILENAME = "version.txt"


# ─── Build side ──────────────────────────────────────────────────────


def build_snapshot(
    gazetteer: Gazetteer,
    *,
    out_dir: Path,
    version: str | None = None,
) -> tuple[Path, str]:
    """Serialise the gazetteer into `<out_dir>/snapshot.json` + write
    the version marker to `<out_dir>/version.txt`. Returns
    (snapshot_path, version). `version` defaults to the current
    UTC epoch seconds so a fresh build always beats the previous
    marker."""
    out_dir.mkdir(parents=True, exist_ok=True)
    version = version or str(int(time.time()))

    # Serialise unique canonical entries (not the exploded variant
    # rows) — the loader re-explodes on `build_gazetteer`.
    seen: set[str] = set()
    rows: list[dict[str, object]] = []
    for entry in gazetteer.entries:
        key = f"{entry.canonical_name}|{entry.district}|{entry.state}|{entry.kind}"
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "name": entry.canonical_name,
                "kind": entry.kind,
                "district": entry.district,
                "state": entry.state,
                "lat": entry.lat,
                "lon": entry.lon,
                "variants": list(entry.variants),
            }
        )

    payload = {
        "schema_version": 1,
        "generated_at": int(time.time()),
        "version": version,
        "entries": rows,
    }
    snapshot_path = out_dir / SNAPSHOT_FILENAME
    snapshot_path.write_text(json.dumps(payload), encoding="utf-8")
    (out_dir / VERSION_FILENAME).write_text(version, encoding="utf-8")

    log.info(
        "rag.snapshot.built",
        entries=len(rows),
        version=version,
        path=str(snapshot_path),
    )
    return snapshot_path, version


def load_snapshot(snapshot_path: Path) -> Gazetteer:
    """Read a snapshot file and materialise a Gazetteer. Reverses the
    output of `build_snapshot()`."""
    if not snapshot_path.exists():
        raise FileNotFoundError(f"snapshot missing: {snapshot_path}")
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    entries: list[GazetteerEntry] = []
    for row in payload.get("entries", []):
        entries.append(
            GazetteerEntry(
                canonical_name=row["name"],
                matched_name=row["name"],
                kind=row.get("kind", "landmark"),
                district=row.get("district"),
                state=row["state"],
                lat=row.get("lat"),
                lon=row.get("lon"),
                variants=tuple(row.get("variants", [])),
            )
        )
    return build_gazetteer(entries)


# ─── Hot-reload side ─────────────────────────────────────────────────


@dataclass(slots=True)
class SnapshotLoader:
    """Owns the reference to the currently-loaded gazetteer. Poll
    `version.txt` externally (e.g. a background asyncio task) and
    call `.poll_and_swap()` — when the marker changes the loader
    atomically replaces `.current`.

    Consumers (the resolver) should read `.current` once per query
    rather than caching the reference, so a swap becomes visible on
    the next query rather than the next process restart."""

    snapshot_dir: Path
    current: Gazetteer
    version: str = ""
    swaps_done: int = 0
    read_failures: int = 0
    _last_check: float = field(default=0.0, init=False)

    @classmethod
    def from_dir(cls, snapshot_dir: Path) -> SnapshotLoader:
        """Bootstrap the loader from a snapshot directory. Reads the
        current version + loads the snapshot into memory. Raises if
        either file is missing — a boot-time failure is safer than
        starting up with an empty resolver."""
        snap_path = snapshot_dir / SNAPSHOT_FILENAME
        ver_path = snapshot_dir / VERSION_FILENAME
        gaz = load_snapshot(snap_path)
        version = ver_path.read_text(encoding="utf-8").strip() if ver_path.exists() else ""
        loader = cls(snapshot_dir=snapshot_dir, current=gaz, version=version)
        log.info("rag.snapshot.loaded_initial", version=version, entries=gaz.size())
        return loader

    def poll_and_swap(self) -> bool:
        """Check the version marker; if it's newer than what we have
        loaded, read the new snapshot and swap. Returns True if a
        swap happened, False if the marker was unchanged.

        Read failures (corrupted snapshot, disk error) bump the
        `read_failures` counter but never raise — the loader keeps
        serving the old snapshot rather than crashing the process."""
        self._last_check = time.time()
        ver_path = self.snapshot_dir / VERSION_FILENAME
        if not ver_path.exists():
            return False
        try:
            new_version = ver_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            log.warning("rag.snapshot.version_read_failed", error=str(exc))
            self.read_failures += 1
            return False
        if not new_version or new_version == self.version:
            return False
        # New version — try to load it. On failure, keep the old one.
        snap_path = self.snapshot_dir / SNAPSHOT_FILENAME
        try:
            new_gaz = load_snapshot(snap_path)
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            log.error(
                "rag.snapshot.load_failed",
                version=new_version,
                error=str(exc),
            )
            self.read_failures += 1
            return False
        old_version = self.version
        self.current = new_gaz
        self.version = new_version
        self.swaps_done += 1
        log.info(
            "rag.snapshot.swapped",
            old_version=old_version,
            new_version=new_version,
            entries=new_gaz.size(),
        )
        return True


__all__ = [
    "SNAPSHOT_FILENAME",
    "VERSION_FILENAME",
    "SnapshotLoader",
    "build_snapshot",
    "load_snapshot",
]
