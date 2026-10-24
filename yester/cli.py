"""Yester command line interface.

Layout of commands mirrors the product's six directions:

* time machine      → sync / watch / log / restore / ui
* publishing        → publish / preview
* encryption        → encrypt-init / mirror / unlock
* search            → search / index
* archive & inherit → remote / archive / inherit
* compliance        → audit / scan / lock
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, find_vault_root
from .gitops import GitError, Vault, open_vault
from . import audit as audit_mod
from . import archive as archive_mod
from . import crypto as crypto_mod
from . import license as license_mod
from . import publisher
from . import search as search_mod
from . import watcher


def _out(msg: str = "") -> None:
    print(msg, flush=True)


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr, flush=True)


# ----------------------------------------------------------------------
# command handlers
# ----------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path or ".").expanduser().resolve()
    if (path / ".yester").exists():
        _err(f"{path} is already a yester vault")
        return 1
    vault = Vault.init(path, name=args.name, device=args.device)
    _out(f"initialised vault {vault.config.name!r} at {vault.root}")
    _out(f"  device branch: {vault.device_branch}")
    _out("  local-only by default — nothing is uploaded until you say so")
    _out("\nnext steps:")
    _out("  yester watch          start auto-versioning this folder")
    _out("  yester ui             open the Time Machine in your browser")
    return 0


def _open(args: argparse.Namespace) -> Vault:
    try:
        return open_vault(getattr(args, "vault", None))
    except GitError as exc:
        _err(str(exc))
        raise SystemExit(2)


def cmd_watch(args: argparse.Namespace) -> int:
    vault = _open(args)
    watcher.watch(vault, interval=args.interval, on_message=_out)
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    vault = _open(args)
    rev = vault.auto_commit()
    _out(f"committed {rev}" if rev else "nothing to commit")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    vault = _open(args)
    stats = vault.stats()
    for key, value in stats.items():
        if key == "pending":
            continue
        _out(f"{key:>14}: {value}")
    if stats["pending"]:
        _out(f"{'pending':>14}: {', '.join(stats['pending'])}")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    vault = _open(args)
    for entry in vault.log(limit=args.limit):
        _out(f"{entry['short']}  {entry['date'][:19].replace('T', ' ')}  "
             f"[{entry['kind']:<8}] {entry['subject']}")
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    vault = _open(args)
    tag, rev = vault.snapshot(args.label)
    _out(f"snapshot {tag} @ {rev}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    vault = _open(args)
    target = vault.restore(args.path, args.at)
    _out(f"restored {args.path} from {target}")
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    vault = _open(args)
    backup = vault.promote()
    _out(f"{vault.device_branch} is now main; previous main kept as {backup}")
    return 0


def cmd_remote(args: argparse.Namespace) -> int:
    vault = _open(args)
    if args.remote_action == "add":
        archive_mod.add_remote(vault, args.name, args.url)
        _out(f"archive remote {args.name} -> {args.url}")
    else:
        for remote in archive_mod.list_remotes(vault):
            _out(f"{remote['name']:<16} {remote['url']}")
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    vault = _open(args)
    try:
        branch = vault.push()
    except GitError as exc:
        _err(str(exc))
        return 1
    _out(f"pushed {branch}")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    vault = _open(args)
    if args.preview:
        publisher.preview(vault, port=args.port)
        return 0
    try:
        rev = publisher.publish(vault, push=not args.no_push)
    except GitError as exc:
        _err(str(exc))
        return 1
    _out(f"site built and published (site rev {rev})")
    return 0


PASSPHRASE_ENV = "YESTER_PASSPHRASE"


def _passphrase(explicit: str | None, prompt: str = "passphrase: ") -> str | None:
    """Resolve a passphrase without ever blocking an unattended run.

    Order: --passphrase, $YESTER_PASSPHRASE, interactive prompt.  If stdin is
    not a terminal and neither of the first two is available, return None so
    the caller can fail with a message — `getpass` would block forever, which
    is fatal for the cron/systemd use case this tool is built for.
    """
    if explicit:
        return explicit
    env = os.environ.get(PASSPHRASE_ENV)
    if env:
        return env
    # On Windows, getpass reads the console through msvcrt and ignores stdin
    # redirection entirely, while sys.stdin.isatty() happily reports True for
    # NUL — so "isatty() and not redirected" is not a reliable test there.
    # Requiring a TTY on *stdout* as well detects redirection; when there is
    # no usable prompt we read one line from stdin, which supports both
    # `echo pw | yester mirror` and a clean EOF failure.
    if sys.stdin.isatty() and sys.stdout.isatty():
        return getpass.getpass(prompt)
    try:
        line = sys.stdin.readline().strip()
    except (OSError, ValueError):
        line = ""
    if line:
        return line
    _err(f"no passphrase available: pass --passphrase or set ${PASSPHRASE_ENV}")
    return None


def cmd_encrypt_init(args: argparse.Namespace) -> int:
    vault = _open(args)
    passphrase = _passphrase(args.passphrase)
    if passphrase is None:
        return 2
    if args.passphrase or not (sys.stdin.isatty() and sys.stdout.isatty()):
        confirm = passphrase          # non-interactive input is taken as given
    else:
        confirm = getpass.getpass("confirm:    ")
    if passphrase != confirm:
        _err("passphrases do not match")
        return 1
    try:
        info = crypto_mod.init_encryption(vault, passphrase)
    except crypto_mod.CryptoError as exc:
        _err(str(exc))
        return 1
    _out("encryption enabled — the plaintext vault will stay on this machine")
    _out(f"\nRECOVERY CODE (store it safely, shown once):\n\n  {info['recovery_code']}\n")
    _out("run `yester mirror` to build and push the encrypted mirror")
    return 0


def cmd_mirror(args: argparse.Namespace) -> int:
    vault = _open(args)
    passphrase = _passphrase(args.passphrase)
    if passphrase is None:
        return 2
    try:
        master = crypto_mod.load_master_key(vault, passphrase=passphrase)
    except crypto_mod.CryptoError as exc:
        _err(str(exc))
        return 1
    manifest = crypto_mod.build_mirror(vault, master)
    _out(f"encrypted mirror built: {manifest['file_count']} files "
         f"({manifest['algorithm']})")
    if args.push:
        mirror = vault.config.mirror_dir
        if not (mirror / ".git").exists():
            from .gitops import _git
            _git(mirror, "init", "-b", "mirror")
            _git(mirror, "config", "user.name", f"yester:{vault.config.device}")
            _git(mirror, "config", "user.email", "yester@localhost")
        from .gitops import _git
        _git(mirror, "add", "-A")
        _git(mirror, "commit", "-m", "yester(mirror): update", "--allow-empty")
        remote = vault.config.data.get("remotes", {}).get("origin", "")
        if not remote:
            _err("mirror built but not pushed — set remotes.origin first")
            return 1
        proc = _git(mirror, "push", "-f", remote, "HEAD:refs/heads/mirror")
        if proc.returncode != 0:
            _err(f"push failed: {proc.stderr.strip()[:200]}")
            return 1
        _out(f"mirror pushed to {remote}")
    return 0


def cmd_unlock(args: argparse.Namespace) -> int:
    passphrase = None
    if not args.recovery:
        passphrase = _passphrase(args.passphrase)
        if passphrase is None:
            return 2
    try:
        count = crypto_mod.unlock_mirror(
            args.mirror, args.output,
            passphrase=passphrase if not args.recovery else None,
            recovery=args.recovery,
        )
    except crypto_mod.CryptoError as exc:
        _err(str(exc))
        return 1
    _out(f"decrypted {count} files into {args.output}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    vault = _open(args)
    if args.reindex:
        info = search_mod.SearchIndex(vault).build()
        _out(f"indexed {info['indexed_documents']} documents")
    results = search_mod.SearchIndex(vault).query(args.query)
    if not results:
        _out("no matches")
        return 0
    for result in results:
        _out(f"{result['score']:>7}  {result['rev'][:10]}  {result['path']}")
        _out(f"         {result['snippet']}")
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    vault = _open(args)
    try:
        result = archive_mod.archive(vault, label=args.label)
    except GitError as exc:
        _err(str(exc))
        return 1
    _out(f"archived as {result['tag']}")
    for remote in result["remotes"]:
        _out(f"  {remote['remote']:<14} vault:{remote['vault']} "
             f"mirror:{remote['mirror']} blobs:{remote['blobs']}")
    return 0


def cmd_inherit(args: argparse.Namespace) -> int:
    vault = _open(args)
    path = archive_mod.configure_inheritance(vault, args.heir, args.contact, args.note)
    _out(f"inheritance plan written to {path}")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    vault = _open(args)
    key = vault.config.license_key
    try:
        license_mod.require("audit-export", key)
    except license_mod.LicenseError as exc:
        _err(str(exc))
        return 1
    report = audit_mod.export_audit(vault, fmt=args.format, since=args.since)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        _out(f"audit trail written to {args.output}")
    else:
        _out(report)
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    vault = _open(args)
    findings = audit_mod.scan_files(vault, args.paths)
    if not findings:
        _out("clean — no secrets or PII patterns found")
        return 0
    for finding in findings:
        _out(f"{finding['file']}:{finding['line']}  "
             f"[{finding['pattern']}]  {finding['match']}")
    _err(f"{len(findings)} finding(s)")
    return 1


def cmd_lock(args: argparse.Namespace) -> int:
    vault = _open(args)
    key = vault.config.license_key
    try:
        license_mod.require("retention-mode", key)
    except license_mod.LicenseError as exc:
        _err(str(exc))
        return 1
    audit_mod.set_retention(vault, True)
    _out("retention mode enabled — auto gc disabled, history preserved")
    return 0


def cmd_unlock_retention(args: argparse.Namespace) -> int:
    vault = _open(args)
    audit_mod.set_retention(vault, False)
    _out("retention mode disabled")
    return 0


def cmd_activate(args: argparse.Namespace) -> int:
    tier = license_mod.validate_key(args.key)
    if not tier:
        _err("invalid licence key")
        return 1
    root = find_vault_root()
    if root is None:
        _err("activate from inside a vault")
        return 1
    from .config import VaultConfig
    VaultConfig.load(root).set("license.key", args.key.strip().upper())
    _out(f"{tier} licence activated — thank you for supporting Yester")
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    from .timemachine import serve
    serve(_open(args), port=args.port)
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    import yaml

    vault = _open(args)
    cfg = vault.config
    if args.config_action == "set":
        raw = args.value
        lowered = raw.lower()
        if lowered in ("true", "false"):
            value: object = lowered == "true"
        else:
            try:
                value = int(raw)
            except ValueError:
                try:
                    value = float(raw)
                except ValueError:
                    value = raw
        cfg.set(args.key, value)
        _out(f"{args.key} = {value}")
    elif args.config_action == "get":
        node: object = cfg.data
        for part in args.key.split("."):
            if not isinstance(node, dict) or part not in node:
                _err(f"unknown key: {args.key}")
                return 1
            node = node[part]
        _out(node if isinstance(node, (str, int, float, bool)) else yaml.safe_dump(node))
    else:
        _out(yaml.safe_dump(cfg.data, sort_keys=False, allow_unicode=True))
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    _out(f"yester {__version__}")
    return 0


# ----------------------------------------------------------------------
# parser
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yester",
        description="A time machine for every folder you own — "
                    "local-first, Git-powered file versioning.",
    )
    parser.add_argument("--version", action="store_true",
                        help="print version and exit")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("init", help="create a new vault")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--name")
    p.add_argument("--device", default=None,
                   help="device name for this machine (default: hostname)")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("watch", help="watch the vault and auto-commit")
    p.add_argument("--interval", type=int, default=None,
                   help="seconds between syncs (default from config)")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("sync", help="commit pending changes now")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("status", help="vault overview")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("log", help="show history")
    p.add_argument("-n", "--limit", type=int, default=20)
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("snapshot", help="labelled snapshot (commit + tag)")
    p.add_argument("label")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("restore", help="restore a file to an earlier version")
    p.add_argument("path")
    p.add_argument("--at", default=None, help="revision (default: previous commit)")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("promote", help="make this device's branch the new main")
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("remote", help="manage archive remotes")
    remote_sub = p.add_subparsers(dest="remote_action")
    ra = remote_sub.add_parser("add")
    ra.add_argument("name")
    ra.add_argument("url")
    remote_sub.add_parser("list")
    p.set_defaults(func=cmd_remote)

    p = sub.add_parser("push", help="push the plaintext vault (only if you opted in)")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("publish", help="publish public/ as a static site")
    p.add_argument("--preview", action="store_true")
    p.add_argument("--no-push", action="store_true")
    p.add_argument("--port", type=int, default=8788)
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("encrypt-init", help="set up the encrypted mirror")
    p.add_argument("--passphrase", default=None,
                   help="(discouraged) pass passphrase on the command line")
    p.set_defaults(func=cmd_encrypt_init)

    p = sub.add_parser("mirror", help="build (and push) the encrypted mirror")
    p.add_argument("--push", action="store_true")
    p.add_argument("--passphrase", default=None)
    p.set_defaults(func=cmd_mirror)

    p = sub.add_parser("unlock", help="decrypt a mirror directory")
    p.add_argument("mirror")
    p.add_argument("output")
    p.add_argument("--passphrase", default=None)
    p.add_argument("--recovery", default=None)
    p.set_defaults(func=cmd_unlock)

    p = sub.add_parser("search", help="search every version")
    p.add_argument("query")
    p.add_argument("--reindex", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("index", help="rebuild the search index")
    p.set_defaults(func=lambda a: cmd_search(argparse.Namespace(
        query="__never_matches__", reindex=True)))

    p = sub.add_parser("archive", help="push everything to all archive remotes")
    p.add_argument("--label", default=None)
    p.set_defaults(func=cmd_archive)

    p = sub.add_parser("inherit", help="configure digital inheritance")
    p.add_argument("--heir", required=True)
    p.add_argument("--contact", default="")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_inherit)

    p = sub.add_parser("audit", help="export the compliance audit trail")
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--since", default=None)
    p.add_argument("--output", default=None)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("scan", help="scan working tree for secrets/PII")
    p.add_argument("paths", nargs="*")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("lock", help="enable retention (compliance) mode")
    p.set_defaults(func=cmd_lock)

    p = sub.add_parser("unlock-retention", help="disable retention mode")
    p.set_defaults(func=cmd_unlock_retention)

    p = sub.add_parser("activate", help="activate a licence key")
    p.add_argument("key")
    p.set_defaults(func=cmd_activate)

    p = sub.add_parser("ui", help="open the Time Machine in your browser")
    p.add_argument("--port", type=int, default=8787)
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("config", help="view or change vault configuration")
    config_sub = p.add_subparsers(dest="config_action")
    config_sub.add_parser("list")
    cg = config_sub.add_parser("get")
    cg.add_argument("key")
    cs = config_sub.add_parser("set")
    cs.add_argument("key")
    cs.add_argument("value")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("version", help="print version")
    p.set_defaults(func=cmd_version)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "version", False):
        return cmd_version(args)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except (GitError, ConfigError) as exc:
        _err(str(exc))
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
