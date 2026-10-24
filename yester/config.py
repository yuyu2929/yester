"""Vault configuration.

A vault's configuration lives in ``<vault>/.yester/config.yml`` and is
committed to git so that every device shares the same settings.  Machine
specific secrets (keys, licence, indexes, mirrors, blob store) live in
sibling directories that are excluded via ``.yester/.gitignore``.
"""
from __future__ import annotations

import copy
import os
import socket
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIRNAME = ".yester"
CONFIG_FILENAME = "config.yml"

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "name": "my-vault",
    "device": socket.gethostname(),
    "sync": {
        "interval_minutes": 5,
        "debounce_seconds": 20,
        "auto_commit": True,
    },
    # Files larger than this are stored in the content-addressed blob
    # store instead of git history, keeping repositories slim.
    "large_file_mb": 25,
    # The plaintext repository never leaves the machine unless the user
    # explicitly opts in by configuring a remote.
    "local_only": True,
    "remotes": {
        "origin": "",   # encrypted mirror remote (safe on public hosts)
        "blobs": "",    # rsync-style target for the blob store
    },
    "publish": {
        "enabled": False,
        "remote": "",
        "branch": "gh-pages",
    },
    "crypto": {
        "enabled": False,
        "kdf": "scrypt",
        "algorithm": "AES-256-GCM",
    },
    "archive": {
        "remotes": [],
    },
    "license": {
        "key": "",
    },
    # Compliance retention mode: blocks history rewrites and auto gc.
    "retention": False,
}


class ConfigError(Exception):
    """Raised when a vault configuration is missing or malformed."""


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep-merge *override* into a deep copy of *base*.

    The deep copy matters: without it, config instances would share
    nested dicts with DEFAULT_CONFIG and ``set()`` would mutate the
    module-level defaults for the rest of the process.
    """
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class VaultConfig:
    """Typed accessor around the raw config mapping."""

    def __init__(self, root: Path, data: dict[str, Any]):
        self.root = Path(root)
        self.data = data

    # -- persistence -----------------------------------------------------
    @property
    def path(self) -> Path:
        return self.root / CONFIG_DIRNAME / CONFIG_FILENAME

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(self.data, fh, allow_unicode=True, sort_keys=False)

    @classmethod
    def load(cls, root: Path) -> "VaultConfig":
        path = Path(root) / CONFIG_DIRNAME / CONFIG_FILENAME
        if not path.is_file():
            raise ConfigError(f"no vault config at {path}")
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return cls(Path(root), _deep_merge(DEFAULT_CONFIG, data))

    @classmethod
    def create(cls, root: Path, name: str | None = None,
               device: str | None = None, **overrides: Any) -> "VaultConfig":
        data = _deep_merge(DEFAULT_CONFIG, overrides)
        data["name"] = name or Path(root).resolve().name
        data["device"] = device or socket.gethostname()
        cfg = cls(Path(root), data)
        cfg.save()
        return cfg

    # -- shortcuts -------------------------------------------------------
    @property
    def name(self) -> str:
        return str(self.data.get("name", "vault"))

    @property
    def device(self) -> str:
        return str(self.data.get("device", socket.gethostname()))

    @property
    def state_dir(self) -> Path:
        return self.root / CONFIG_DIRNAME

    @property
    def keys_dir(self) -> Path:
        return self.state_dir / "keys"

    @property
    def blobs_dir(self) -> Path:
        return self.state_dir / "blobs"

    @property
    def mirror_dir(self) -> Path:
        return self.state_dir / "mirror"

    @property
    def site_dir(self) -> Path:
        return self.state_dir / "site"

    @property
    def index_dir(self) -> Path:
        return self.state_dir / "index"

    @property
    def large_file_bytes(self) -> float:
        return float(self.data.get("large_file_mb", 25)) * 1024 * 1024

    @property
    def sync_interval(self) -> int:
        return int(self.data.get("sync", {}).get("interval_minutes", 5)) * 60

    @property
    def debounce_seconds(self) -> int:
        return int(self.data.get("sync", {}).get("debounce_seconds", 20))

    @property
    def crypto_enabled(self) -> bool:
        return bool(self.data.get("crypto", {}).get("enabled", False))

    @property
    def retention(self) -> bool:
        return bool(self.data.get("retention", False))

    @property
    def license_key(self) -> str:
        return str(self.data.get("license", {}).get("key", "") or "")

    def set(self, dotted: str, value: Any) -> None:
        """Set a nested value using dotted notation, e.g. ``sync.interval_minutes``."""
        node = self.data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ConfigError(f"config path conflict at {dotted!r}")
        node[parts[-1]] = value
        self.save()


def find_vault_root(start: Path | str | None = None) -> Path | None:
    """Walk upwards from *start* looking for a ``.yester`` directory."""
    env = os.environ.get("YESTER_VAULT")
    if env:
        p = Path(env).expanduser()
        return p if (p / CONFIG_DIRNAME).is_dir() else None
    current = Path(start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / CONFIG_DIRNAME).is_dir():
            return candidate
    return None
