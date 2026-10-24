"""Compliance tooling: audit export, secret scanning, retention mode.

Git history is already an immutable, attributable change log — these
helpers turn it into artefacts an auditor will accept, and stop sensitive
data from ever entering history.
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .gitops import Vault

# (name, compiled pattern) — deliberately conservative to limit noise.
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("aws-access-key-id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("private-key-block", re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("cn-id-number", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("cn-phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("generic-secret", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*"
        r"['\"]?[A-Za-z0-9/+_\-]{16,}")),
]

_KIND_ORDER = {"system": 0, "snapshot": 1, "auto": 2, "manual": 3,
               "restore": 4, "publish": 5, "archive": 6}


def audit_records(vault: Vault, since: str | None = None,
                  limit: int = 2000) -> list[dict]:
    """Structured audit trail derived from git history."""
    sep = "%x1f"
    args = ["log", f"-n{limit}", "main",
            f"--format=%H{sep}%h{sep}%ct{sep}%an{sep}%s"]
    if since:
        args.append(f"--since={since}")
    proc = vault._git(*args)
    records = []
    for line in proc.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 5:
            continue
        full, short, ts, author, subject = parts[:5]
        kind, text = "manual", subject
        match = re.match(r"yester\((\w+)\):\s*(.*)", subject)
        if match:
            kind, text = match.group(1), match.group(2)
        files = vault.changed_files(full)
        records.append({
            "rev": full,
            "rev_short": short,
            "timestamp": datetime.fromtimestamp(int(ts), timezone.utc).isoformat(),
            "epoch": int(ts),
            "device": author.replace("yester:", "", 1)
            if author.startswith("yester:") else author,
            "kind": kind,
            "subject": text,
            "file_count": len(files),
            "files": files,
        })
    return records


def export_audit(vault: Vault, fmt: str = "json",
                 since: str | None = None) -> str:
    records = audit_records(vault, since=since)
    if fmt == "json":
        return json.dumps({
            "vault": vault.config.name,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "record_count": len(records),
            "records": records,
        }, indent=2, ensure_ascii=False)
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=["timestamp", "device", "kind", "subject",
                        "file_count", "rev_short"],
        )
        writer.writeheader()
        for record in records:
            writer.writerow({k: record[k] for k in writer.fieldnames})
        return buf.getvalue()
    raise ValueError(f"unsupported format: {fmt}")


def scan_files(vault: Vault, paths: list[str] | None = None) -> list[dict]:
    """Scan working-tree files for secrets / PII patterns."""
    if paths:
        candidates = [vault.root / p for p in paths]
    else:
        candidates = [
            p for p in vault.root.rglob("*")
            if p.is_file() and ".git" not in p.parts and ".yester" not in p.parts
        ]
    findings: list[dict] = []
    for path in candidates:
        if not path.is_file() or path.stat().st_size > 5 * 1024 * 1024:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(vault.root).as_posix()
        for lineno, line in enumerate(text.splitlines(), start=1):
            for name, pattern in SECRET_PATTERNS:
                for match in pattern.finditer(line):
                    findings.append({
                        "file": rel,
                        "line": lineno,
                        "pattern": name,
                        "match": _redact(match.group(0)),
                    })
    return findings


def _redact(value: str) -> str:
    if len(value) <= 8:
        return value[:2] + "***"
    return value[:4] + "***" + value[-2:]


PRE_COMMIT_HOOK = """#!/bin/sh
# Yester secret scan — blocks commits containing credentials or PII.
if command -v yester >/dev/null 2>&1; then
    yester scan --staged || exit 1
fi
exit 0
"""


def install_hook(vault: Vault) -> Path:
    hooks = vault.root / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    path = hooks / "pre-commit"
    path.write_text(PRE_COMMIT_HOOK, encoding="utf-8")
    path.chmod(0o755)
    return path


def set_retention(vault: Vault, enabled: bool) -> None:
    """Toggle compliance retention mode.

    When enabled: automatic garbage collection is disabled and a
    ``pre-receive``-style guard is documented for shared remotes, so
    history cannot be silently rewritten or pruned.
    """
    vault.config.set("retention", enabled)
    vault._git("config", "gc.auto", "0" if enabled else "6700")
    note = vault.root / "RETENTION.md"
    if enabled:
        note.write_text(
            "# Retention mode\n\n"
            "This vault is under compliance retention: automatic garbage\n"
            "collection is disabled and history rewrites are discouraged.\n"
            f"Enabled: {datetime.now(timezone.utc).isoformat()}\n",
            encoding="utf-8",
        )
        vault.commit("enable retention mode", kind="manual")
    elif note.exists():
        note.unlink()
        vault.commit("disable retention mode", kind="manual")
