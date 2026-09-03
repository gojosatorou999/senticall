"""The import-linter contracts are the machine-checked form of the
architecture invariants in CLAUDE.md. Running them here as well as in
`make lint` means a broken contract fails during `pytest` too, which is
what most developers run more often."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.slow
def test_import_linter_contracts_pass() -> None:
    result = subprocess.run(
        ["uv", "run", "lint-imports"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"import-linter failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
