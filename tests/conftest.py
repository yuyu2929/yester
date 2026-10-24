"""Shared pytest fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest

from yester.config import VaultConfig
from yester.gitops import Vault


@pytest.fixture()
def vault(tmp_path: Path) -> Vault:
    vault = Vault.init(tmp_path / "vault", name="test-vault", device="testbox")
    (vault.root / "notes").mkdir()
    (vault.root / "notes" / "hello.md").write_text(
        "# Hello\n\nfirst version of the note\n", encoding="utf-8"
    )
    (vault.root / "readme.txt").write_text("project readme\n", encoding="utf-8")
    vault.auto_commit()
    return vault


@pytest.fixture()
def tiny_blob_vault(vault: Vault) -> Vault:
    """Vault where every non-empty file is treated as oversized."""
    vault.config.set("large_file_mb", 0)
    return vault
