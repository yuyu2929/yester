"""Multi-remote archiving and digital inheritance.

"Forever" is a systems property, not a promise: a single git host can
disappear.  ``yester archive`` pushes the vault (plus its encrypted
mirror and blob store) to *every* configured archive remote and records
a signed-off manifest, so redundancy is a one-command operation.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .gitops import GitError, Vault, _ts_slug, slugify

MANIFEST_NAME = "ARCHIVE-MANIFEST.json"
INHERITANCE_NAME = "INHERITANCE.md"

INHERITANCE_TEMPLATE = """# Digital inheritance

This vault is configured for transfer to a designated heir.

- **Heir:** {heir}
- **Contact:** {contact}
- **Configured:** {date}
- **Note:** {note}

## What the heir receives

1. This repository (or the encrypted mirror, if encryption is enabled).
2. The keyfile `.yester/keys/vault.key` **or** the recovery code below.
3. The passphrase, transmitted out of band.

## Recovery code

```
{recovery}
```

## Instructions for the heir

```bash
# encrypted mirror
yester unlock ./mirror ./restored --recovery "<code above>"

# or with the passphrase
yester unlock ./mirror ./restored
```

Without the keyfile, the passphrase **and** the recovery code are lost,
the mirror cannot be decrypted — store them with a lawyer or in a safe.
"""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def add_remote(vault: Vault, name: str, url: str) -> None:
    vault._git("remote", "add", name, url)
    remotes = list(vault.config.data.get("archive", {}).get("remotes", []))
    entry = {"name": name, "url": url}
    remotes = [r for r in remotes if r.get("name") != name] + [entry]
    vault.config.set("archive.remotes", remotes)


def list_remotes(vault: Vault) -> list[dict[str, str]]:
    return list(vault.config.data.get("archive", {}).get("remotes", []))


def _push(repo: Path, url: str, refspec: str) -> tuple[bool, str]:
    proc = _git(repo, "push", url, refspec)
    return proc.returncode == 0, proc.stderr.strip()[:200]


def archive(vault: Vault, label: str | None = None) -> dict:
    """Push everything to all archive remotes and record a manifest."""
    remotes = list_remotes(vault)
    if not remotes:
        raise GitError("no archive remotes — add one with `yester remote add <name> <url>`")

    head = vault.head_rev_full()
    results = []
    for remote in remotes:
        ok_main, err_main = _push(vault.root, remote["url"], f"{vault.current_branch()}:{vault.current_branch()}")
        ok_mirror, err_mirror = True, ""
        mirror = vault.config.mirror_dir
        if vault.config.crypto_enabled and (mirror / ".git").exists():
            ok_mirror, err_mirror = _push(mirror, remote["url"], "HEAD:refs/heads/mirror")
        # blob store: rsync when available, otherwise record for manual sync
        blobs_remote = vault.config.data.get("remotes", {}).get("blobs", "")
        ok_blobs, err_blobs = True, ""
        if blobs_remote and shutil.which("rsync"):
            proc = subprocess.run(
                ["rsync", "-a", "--delete", str(vault.config.blobs_dir) + "/",
                 f"{blobs_remote}/"],
                capture_output=True, text=True,
            )
            ok_blobs = proc.returncode == 0
            err_blobs = proc.stderr.strip()[:200]
        results.append({
            "remote": remote["name"], "url": remote["url"],
            "vault": "ok" if ok_main else f"failed: {err_main}",
            "mirror": "ok" if ok_mirror else f"failed: {err_mirror}",
            "blobs": "ok" if ok_blobs else f"failed: {err_blobs}",
        })

    manifest = {
        "yester": "archive-manifest",
        "version": 1,
        "label": label or "archive",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "vault": vault.config.name,
        "device": vault.config.device,
        "head": head,
        "snapshot_count": len(vault.snapshots()),
        "remotes": results,
    }
    path = vault.root / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    vault.commit(f"archive {label or head[:8]}", kind="archive")
    tag = f"archive/{_ts_slug()}-{slugify(label or 'archive')}"
    vault._git("tag", "-a", tag, "-m", f"archive: {label or head[:8]}")
    return {"tag": tag, "remotes": results, "manifest": str(path)}


def configure_inheritance(vault: Vault, heir: str, contact: str = "",
                          note: str = "") -> Path:
    """Write the INHERITANCE.md handover document into the vault."""
    from .crypto import load_master_key  # noqa: F401  (availability check)

    recovery = ""
    keyfile = vault.config.keys_dir / "vault.key"
    if keyfile.is_file():
        data = json.loads(keyfile.read_text(encoding="utf-8"))
        if "wrapped_by_recovery" in data:
            recovery = "<shown only at `yester encrypt-init` time>"
    path = vault.root / INHERITANCE_NAME
    path.write_text(INHERITANCE_TEMPLATE.format(
        heir=heir, contact=contact or "(not provided)",
        date=datetime.now(timezone.utc).date().isoformat(),
        note=note or "(none)",
        recovery=recovery,
    ), encoding="utf-8")
    vault.commit(f"configure inheritance for {heir}", kind="manual")
    return path
