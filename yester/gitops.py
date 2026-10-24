"""Git operations: vault lifecycle, device branches, snapshots, restore.

Design notes
------------
* Every vault is a single git repository.  ``main`` is the canonical
  branch; each device works on ``devices/<device>`` and merges into
  ``main`` on sync, so history always records *who* changed *what*.
* Files larger than ``large_file_mb`` are moved into a content-addressed
  blob store (``.yester/blobs``) and replaced by a small ``.yblob``
  pointer file.  This keeps repositories small enough for free hosting.
* Commit messages follow the convention ``yester(<kind>): <subject>``
  which the audit module parses into compliance records.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import CONFIG_DIRNAME, VaultConfig

POINTER_SUFFIX = ".yblob"
MAIN_BRANCH = "main"


class GitError(Exception):
    """Raised when a git command fails."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts_slug(dt: datetime | None = None) -> str:
    return (dt or _now()).strftime("%Y%m%d-%H%M%S")


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[\s_-]+", "-", text) or "snapshot"


class Vault:
    """A git-backed folder under version control."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.config = VaultConfig.load(self.root)

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    @classmethod
    def init(cls, path: Path | str, name: str | None = None,
             device: str | None = None) -> "Vault":
        root = Path(path).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        vault = cls.__new__(cls)
        vault.root = root
        vault.config = VaultConfig.create(root, name=name, device=device)

        state = root / CONFIG_DIRNAME
        for sub in ("keys", "blobs", "index"):
            (state / sub).mkdir(parents=True, exist_ok=True)
        # Machine-local state must never be committed.
        (state / ".gitignore").write_text(
            "# machine-local state (never commit)\n"
            "keys/\nblobs/\nindex/\nmirror/\nsite/\nlicense\n*.log\n",
            encoding="utf-8",
        )
        (root / ".gitignore").write_text(
            "# Yester ignores\n*.tmp\n*.temp\n~$*\n.DS_Store\nThumbs.db\n",
            encoding="utf-8",
        )

        try:
            vault._git("init", "-b", MAIN_BRANCH)
        except GitError:
            # git < 2.28 has no `init -b`
            vault._git("init")
            vault._git("symbolic-ref", "HEAD", f"refs/heads/{MAIN_BRANCH}")
        vault._git("config", "user.name", f"yester:{vault.config.device}")
        vault._git("config", "user.email", "yester@localhost")
        vault._git("config", "commit.gpgsign", "false")
        vault._git("add", "-A")
        vault._git("commit", "-m", "yester(system): init vault",
                   "--allow-empty")
        # Device branch: every device works here, main stays canonical.
        vault._git("checkout", "-b", f"devices/{vault.config.device}")
        return vault

    # ------------------------------------------------------------------
    # git plumbing
    # ------------------------------------------------------------------
    def _git(self, *args: str, check: bool = True,
             input_text: str | None = None,
             binary: bool = False) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=not binary,
            input=input_text,
        )
        if check and proc.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed ({proc.returncode}): "
                f"{(proc.stderr or '').strip()[:500]}"
            )
        return proc

    def is_repo(self) -> bool:
        proc = self._git("rev-parse", "--is-inside-work-tree", check=False)
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    @property
    def device_branch(self) -> str:
        return f"devices/{self.config.device}"

    def current_branch(self) -> str:
        proc = self._git("rev-parse", "--abbrev-ref", "HEAD")
        return proc.stdout.strip()

    def head_rev(self) -> str:
        proc = self._git("rev-parse", "--short", "HEAD")
        return proc.stdout.strip()

    def head_rev_full(self) -> str:
        proc = self._git("rev-parse", "HEAD")
        return proc.stdout.strip()

    # ------------------------------------------------------------------
    # status & staging
    # ------------------------------------------------------------------
    def status(self) -> list[tuple[str, str]]:
        """Return ``(code, path)`` pairs from ``git status --porcelain``."""
        proc = self._git("status", "--porcelain", "-uall", "-z")
        entries: list[tuple[str, str]] = []
        for record in proc.stdout.split("\0"):
            if not record:
                continue
            code, path = record[:2], record[3:]
            entries.append((code.strip(), path))
        return entries

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _exclude_path(self, rel: str) -> None:
        exclude = self.root / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if rel not in existing.splitlines():
            with open(exclude, "a", encoding="utf-8") as fh:
                fh.write(rel + "\n")

    def _blobify(self, rel: str) -> str:
        """Move an oversized file into the blob store, write a pointer."""
        src = self.root / rel
        if not src.is_file():
            return rel
        sha = self._sha256(src)
        dest = self.config.blobs_dir / sha[:2] / sha
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copy2(src, dest)
        pointer = {
            "yester": "blob-pointer",
            "version": 1,
            "name": src.name,
            "size": src.stat().st_size,
            "sha256": sha,
            "stored_at": _now().isoformat(),
            "device": self.config.device,
        }
        pointer_path = self.root / (rel + POINTER_SUFFIX)
        pointer_path.parent.mkdir(parents=True, exist_ok=True)
        # keep the old pointer (and its timestamp) when nothing changed
        try:
            if json.loads(pointer_path.read_text(encoding="utf-8")).get("sha256") == sha:
                return rel + POINTER_SUFFIX
        except (OSError, json.JSONDecodeError):
            pass
        pointer_path.write_text(json.dumps(pointer, indent=2), encoding="utf-8")
        self._exclude_path(rel)
        # If the file was previously tracked, drop it from the index.
        tracked = self._git("ls-files", "--", rel).stdout.strip()
        if tracked:
            self._git("rm", "--cached", "--quiet", rel)
        return rel + POINTER_SUFFIX

    def stage(self) -> list[str]:
        """Stage changes, isolating oversized files.  Returns staged paths."""
        limit = self.config.large_file_bytes
        for code, path in self.status():
            if code == "??" or code in ("A", " M", "M"):
                candidate = self.root / path
                if (candidate.is_file() and candidate.stat().st_size > limit
                        and not path.endswith(POINTER_SUFFIX)
                        and not path.startswith(CONFIG_DIRNAME + "/")):
                    self._blobify(path)
        self._refresh_blobs()
        self._git("add", "-A")
        proc = self._git("diff", "--cached", "--name-only")
        return [line for line in proc.stdout.splitlines() if line.strip()]

    def _blob_managed_files(self) -> list[str]:
        """Working-tree files managed by the blob store (have a pointer).

        These are git-ignored, so ``git status`` never reports their
        modifications — they must be refreshed explicitly or edits to
        them would silently never be committed.
        """
        out: list[str] = []
        for pointer in self.root.rglob("*" + POINTER_SUFFIX):
            if any(part in (CONFIG_DIRNAME, ".git") for part in pointer.parts):
                continue
            original = pointer.with_name(pointer.name[:-len(POINTER_SUFFIX)])
            if original.is_file():
                out.append(original.relative_to(self.root).as_posix())
        return out

    def _refresh_blobs(self) -> None:
        """Re-seal blob-managed files whose content changed."""
        for rel in self._blob_managed_files():
            src = self.root / rel
            pointer_path = self.root / (rel + POINTER_SUFFIX)
            sha = self._sha256(src)
            try:
                existing = json.loads(pointer_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
            if existing.get("sha256") == sha:
                continue  # unchanged, keep the pointer (and its timestamp)
            dest = self.config.blobs_dir / sha[:2] / sha
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copy2(src, dest)
            pointer = {
                "yester": "blob-pointer",
                "version": 1,
                "name": src.name,
                "size": src.stat().st_size,
                "sha256": sha,
                "stored_at": _now().isoformat(),
                "device": self.config.device,
            }
            pointer_path.write_text(
                json.dumps(pointer, indent=2), encoding="utf-8"
            )

    # ------------------------------------------------------------------
    # commits
    # ------------------------------------------------------------------
    def commit(self, message: str, kind: str = "manual",
               allow_empty: bool = False) -> str | None:
        """Commit staged changes.  Returns the short rev or ``None``."""
        self._git("add", "-A")
        staged = self._git("diff", "--cached", "--name-only").stdout.strip()
        if not staged and not allow_empty:
            return None
        args = ["commit", "-m", f"yester({kind}): {message}"]
        if allow_empty:
            args.append("--allow-empty")
        self._git(*args)
        self._sync_main()
        return self.head_rev()

    def _sync_main(self) -> None:
        """Keep ``main`` in step with the device branch.

        Fast-forward when possible; a real merge commit when another
        device has synced in between, so both histories are preserved.
        """
        if self.current_branch() == MAIN_BRANCH:
            return
        device = self.device_branch
        if self._git("rev-parse", "--verify", MAIN_BRANCH, check=False).returncode != 0:
            self._git("branch", MAIN_BRANCH, device)
            return
        is_ancestor = self._git(
            "merge-base", "--is-ancestor", MAIN_BRANCH, device, check=False
        )
        if is_ancestor.returncode == 0:
            self._git("branch", "-f", MAIN_BRANCH, device)  # fast-forward
            return
        # diverged: merge so the other device's history is not lost
        current = self.current_branch()
        self._git("checkout", MAIN_BRANCH)
        self._git("merge", "--no-edit", "-m",
                  f"yester(system): merge {device} into {MAIN_BRANCH}", device)
        self._git("checkout", current)

    def auto_commit(self) -> str | None:
        self.stage()
        staged = [
            line for line in
            self._git("diff", "--cached", "--name-only").stdout.splitlines()
            if line.strip()
        ]
        if not staged:
            return None
        label = " + ".join(staged[:3]) + (f" (+{len(staged) - 3})" if len(staged) > 3 else "")
        return self.commit(f"sync {len(staged)} file(s): {label}", kind="auto")

    # ------------------------------------------------------------------
    # snapshots
    # ------------------------------------------------------------------
    def snapshot(self, label: str) -> tuple[str, str]:
        """Create a labelled snapshot (commit + annotated tag)."""
        self.stage()
        rev = self.commit(f"snapshot: {label}", kind="snapshot", allow_empty=True)
        tag = f"snap/{_ts_slug()}-{slugify(label)}"
        self._git("tag", "-a", tag, "-m", f"snapshot: {label}")
        return tag, rev or self.head_rev()

    def snapshots(self) -> list[dict[str, str]]:
        proc = self._git(
            "for-each-ref", "--sort=-creatordate",
            "--format=%(refname:short)%1f%(subject)%1f%(creatordate:iso-strict)",
            "refs/tags/snap/",
        )
        out = []
        for line in proc.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) >= 3:
                out.append({"tag": parts[0], "label": parts[1], "date": parts[2]})
        return out

    # ------------------------------------------------------------------
    # history
    # ------------------------------------------------------------------
    def log(self, limit: int = 50) -> list[dict[str, Any]]:
        sep = "%x1f"
        proc = self._git(
            "log", f"-n{limit}", MAIN_BRANCH,
            f"--format=%H{sep}%h{sep}%ct{sep}%s{sep}%an{sep}%D",
        )
        entries = []
        for line in proc.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) < 6:
                continue
            full, short, ts, subject, author, refs = parts[:6]
            kind = "manual"
            match = re.match(r"yester\((\w+)\):\s*(.*)", subject)
            text = subject
            if match:
                kind, text = match.group(1), match.group(2)
            entries.append({
                "rev": full, "short": short,
                "ts": int(ts), "subject": text, "author": author,
                "refs": refs, "kind": kind,
                "date": datetime.fromtimestamp(int(ts), timezone.utc).isoformat(),
            })
        return entries

    def changed_files(self, rev: str) -> list[str]:
        proc = self._git("show", "--name-only", "--format=", rev)
        return [l for l in proc.stdout.splitlines() if l.strip()]

    # ------------------------------------------------------------------
    # file access across revisions
    # ------------------------------------------------------------------
    def list_tree(self, rev: str) -> list[str]:
        proc = self._git("ls-tree", "-r", "--name-only", "-z", rev)
        return [p for p in proc.stdout.split("\0") if p]

    def read_file(self, rev: str, path: str) -> bytes:
        proc = self._git("show", f"{rev}:{path}", binary=True, check=False)
        if proc.returncode != 0:
            raise GitError(f"{path} not found at {rev}")
        return proc.stdout or b""

    def diff(self, from_rev: str, to_rev: str, path: str | None = None) -> str:
        args = ["diff", "--no-color", from_rev, to_rev]
        if path:
            args += ["--", path]
        return self._git(*args).stdout

    def restore(self, path: str, rev: str | None = None) -> str:
        """Restore *path* to its state at *rev* (defaults to previous commit)."""
        self.auto_commit()  # safety snapshot of current state
        target = rev or "HEAD~1"
        proc = self._git("checkout", target, "--", path, check=False)
        if proc.returncode != 0:
            raise GitError(
                f"cannot restore {path!r} from {target}: {proc.stderr.strip()}"
            )
        self.commit(f"restore {path} from {target}", kind="restore")
        return target

    # ------------------------------------------------------------------
    # branches & remotes
    # ------------------------------------------------------------------
    def promote(self) -> str:
        """Make this device's branch the new ``main``.

        The previous main is preserved as ``main-backup-<timestamp>``.
        """
        self.auto_commit()  # never leave uncommitted work behind
        device_branch = self.device_branch
        stamp = _ts_slug()
        backup = f"{MAIN_BRANCH}-backup-{stamp}"
        self._git("branch", "-m", MAIN_BRANCH, backup)
        self._git("branch", "-f", MAIN_BRANCH, device_branch)
        self._git("checkout", MAIN_BRANCH)
        self._git("branch", "-f", device_branch, MAIN_BRANCH)
        self._git("checkout", device_branch)
        return backup

    def remotes(self) -> dict[str, str]:
        proc = self._git("remote", "-v")
        out: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] not in out.values():
                out[parts[0]] = parts[1]
        return out

    def add_remote(self, name: str, url: str) -> None:
        self._git("remote", "add", name, url)
        self.config.set(f"remotes.{name}", url)

    def push(self, remote: str = "origin", branch: str | None = None) -> str:
        if self.config.crypto_enabled:
            raise GitError(
                "encryption is enabled: the plaintext vault is local-only. "
                "Run `yester mirror` to push the encrypted mirror instead."
            )
        branch = branch or MAIN_BRANCH
        self._git("push", "-u", remote, branch)
        return branch

    # ------------------------------------------------------------------
    # stats
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        proc = self._git("count-objects", "-vH")
        fields = {}
        for line in proc.stdout.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip()
        entries = self.status()
        blobs = list(self.config.blobs_dir.rglob("*")) if self.config.blobs_dir.exists() else []
        blob_files = [b for b in blobs if b.is_file()]
        blob_size = sum(b.stat().st_size for b in blob_files)
        return {
            "name": self.config.name,
            "device": self.config.device,
            "branch": self.current_branch(),
            "head": self.head_rev(),
            "repo_size": fields.get("size-pack", "?"),
            "loose_objects": fields.get("count", "0"),
            "dirty_files": len([e for e in entries if not e[1].startswith(CONFIG_DIRNAME)]),
            "pending": [p for _, p in entries][:10],
            "snapshots": len(self.snapshots()),
            "blobs": len(blob_files),
            "blob_size_mb": round(blob_size / 1024 / 1024, 2),
            "crypto": self.config.crypto_enabled,
            "retention": self.config.retention,
            "local_only": self.config.data.get("local_only", True),
        }


def open_vault(path: Path | str | None = None) -> Vault:
    """Open the vault containing *path* (or the current directory)."""
    from .config import find_vault_root

    root = find_vault_root(path)
    if root is None:
        raise GitError("not inside a yester vault (no .yester directory found)")
    return Vault(root)
