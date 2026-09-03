"""Gazetteer snapshot build + hot-reload loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fg_voice.rag.gazetteer import load_full_gazetteer
from fg_voice.rag.snapshot import (
    SNAPSHOT_FILENAME,
    VERSION_FILENAME,
    SnapshotLoader,
    build_snapshot,
    load_snapshot,
)


@pytest.fixture
def sample_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """Returns (input_dir, snapshot_dir)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "districts.json").write_text(
        json.dumps(
            {
                "districts": [
                    {"name": "Kakinada", "state": "Andhra Pradesh", "variants": []},
                    {"name": "Visakhapatnam", "state": "Andhra Pradesh", "variants": ["Vizag"]},
                ]
            }
        )
    )
    return src, tmp_path / "snapshot"


def test_build_snapshot_writes_files(sample_dirs: tuple[Path, Path]) -> None:
    src, snapshot_dir = sample_dirs
    gaz = load_full_gazetteer(
        districts_path=src / "districts.json", mandals_path=None, pois_path=None
    )
    path, version = build_snapshot(gaz, out_dir=snapshot_dir)
    assert path.exists()
    assert path.name == SNAPSHOT_FILENAME
    assert (snapshot_dir / VERSION_FILENAME).exists()
    assert version  # non-empty


def test_snapshot_round_trip_preserves_entries(sample_dirs: tuple[Path, Path]) -> None:
    src, snapshot_dir = sample_dirs
    gaz = load_full_gazetteer(
        districts_path=src / "districts.json", mandals_path=None, pois_path=None
    )
    build_snapshot(gaz, out_dir=snapshot_dir)
    reloaded = load_snapshot(snapshot_dir / SNAPSHOT_FILENAME)
    # After round-trip, both should carry all 2 canonical districts.
    canonical_names = {e.canonical_name for e in reloaded.entries}
    assert "Kakinada" in canonical_names
    assert "Visakhapatnam" in canonical_names


def test_snapshot_serialises_variants(sample_dirs: tuple[Path, Path]) -> None:
    src, snapshot_dir = sample_dirs
    gaz = load_full_gazetteer(
        districts_path=src / "districts.json", mandals_path=None, pois_path=None
    )
    build_snapshot(gaz, out_dir=snapshot_dir)
    reloaded = load_snapshot(snapshot_dir / SNAPSHOT_FILENAME)
    vizag_entries = [e for e in reloaded.entries if "Vizag" in (e.matched_name, *e.variants)]
    assert vizag_entries, "variant lost across snapshot round-trip"


def test_load_snapshot_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="snapshot missing"):
        load_snapshot(tmp_path / "not_there.json")


# ─── SnapshotLoader.poll_and_swap ───────────────────────────────────


def test_loader_from_dir_bootstraps_gazetteer(sample_dirs: tuple[Path, Path]) -> None:
    src, snapshot_dir = sample_dirs
    gaz = load_full_gazetteer(
        districts_path=src / "districts.json", mandals_path=None, pois_path=None
    )
    build_snapshot(gaz, out_dir=snapshot_dir, version="v1")
    loader = SnapshotLoader.from_dir(snapshot_dir)
    assert loader.version == "v1"
    assert loader.current.size() >= 2


def test_loader_swaps_on_version_change(sample_dirs: tuple[Path, Path]) -> None:
    src, snapshot_dir = sample_dirs
    gaz = load_full_gazetteer(
        districts_path=src / "districts.json", mandals_path=None, pois_path=None
    )
    build_snapshot(gaz, out_dir=snapshot_dir, version="v1")
    loader = SnapshotLoader.from_dir(snapshot_dir)

    # Poll with no change — no swap.
    assert loader.poll_and_swap() is False

    # Rebuild snapshot with a new entry AND a bumped version.
    (src / "districts.json").write_text(
        json.dumps(
            {
                "districts": [
                    {"name": "Kakinada", "state": "Andhra Pradesh", "variants": []},
                    {"name": "Visakhapatnam", "state": "Andhra Pradesh", "variants": ["Vizag"]},
                    {"name": "Bapatla", "state": "Andhra Pradesh", "variants": []},
                ]
            }
        )
    )
    gaz2 = load_full_gazetteer(
        districts_path=src / "districts.json", mandals_path=None, pois_path=None
    )
    build_snapshot(gaz2, out_dir=snapshot_dir, version="v2")

    swapped = loader.poll_and_swap()
    assert swapped is True
    assert loader.version == "v2"
    assert loader.swaps_done == 1
    canonical = {e.canonical_name for e in loader.current.entries}
    assert "Bapatla" in canonical


def test_loader_load_failure_keeps_old_snapshot(sample_dirs: tuple[Path, Path]) -> None:
    """Corrupt the snapshot file after boot; poll should NOT crash
    and MUST keep serving the old gazetteer."""
    src, snapshot_dir = sample_dirs
    gaz = load_full_gazetteer(
        districts_path=src / "districts.json", mandals_path=None, pois_path=None
    )
    build_snapshot(gaz, out_dir=snapshot_dir, version="v1")
    loader = SnapshotLoader.from_dir(snapshot_dir)

    # Bump version but corrupt the snapshot payload.
    (snapshot_dir / VERSION_FILENAME).write_text("v2")
    (snapshot_dir / SNAPSHOT_FILENAME).write_text("not json at all")

    swapped = loader.poll_and_swap()
    assert swapped is False
    assert loader.version == "v1"  # unchanged
    assert loader.read_failures >= 1
    # Still serves the previous gazetteer.
    assert loader.current.size() >= 2


def test_loader_missing_version_file_is_noop(tmp_path: Path) -> None:
    """A snapshot dir with no version.txt (fresh boot before any
    snapshot published) should not raise on poll — just no-op."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "districts.json").write_text(
        json.dumps({"districts": [{"name": "Kakinada", "state": "AP", "variants": []}]})
    )
    snap_dir = tmp_path / "snap"
    snap_dir.mkdir()
    gaz = load_full_gazetteer(
        districts_path=src / "districts.json", mandals_path=None, pois_path=None
    )
    build_snapshot(gaz, out_dir=snap_dir, version="v1")
    loader = SnapshotLoader.from_dir(snap_dir)
    # Delete the version file after boot.
    (snap_dir / VERSION_FILENAME).unlink()
    assert loader.poll_and_swap() is False
