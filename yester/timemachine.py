"""The Time Machine: a local web UI for browsing, diffing and restoring
any revision of any file in the vault.

Everything runs on 127.0.0.1 — no accounts, no telemetry, no network
access required.  Open it, drag the timeline, click a file, press
restore.  This is the screenshot people share.
"""
from __future__ import annotations

import json
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from . import __version__
from .gitops import Vault, open_vault
from .search import SearchIndex
from .watcher import sync_once

KIND_COLORS = {
    "system": "#6b7280",
    "snapshot": "#f59e0b",
    "auto": "#3b82f6",
    "manual": "#22c55e",
    "restore": "#ef4444",
    "publish": "#a855f7",
    "archive": "#14b8a6",
}


def create_app(vault: Vault | None = None) -> Flask:
    vault = vault or open_vault()
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent / "templates"),
        static_folder=str(Path(__file__).parent / "static"),
    )
    app.config["VAULT"] = vault

    # ------------------------------------------------------------------
    def v() -> Vault:
        return app.config["VAULT"]  # type: ignore[return-value]

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            version=__version__,
            vault_name=v().config.name,
            device=v().config.device,
            kind_colors=KIND_COLORS,
        )

    # ------------------------------------------------------------------
    @app.get("/api/status")
    def api_status():
        return jsonify(v().stats())

    @app.get("/api/timeline")
    def api_timeline():
        entries = v().log(limit=200)
        for entry in entries:
            entry["color"] = KIND_COLORS.get(entry["kind"], "#6b7280")
            entry["files"] = v().changed_files(entry["rev"])[:20]
        return jsonify(entries)

    @app.get("/api/tree")
    def api_tree():
        rev = request.args.get("rev", "HEAD")
        files = v().list_tree(rev)
        tree: dict = {}
        for path in files:
            node = tree
            parts = path.split("/")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
                if not isinstance(node, dict):
                    node = {}
            node[parts[-1]] = None
        return jsonify(tree)

    @app.get("/api/file")
    def api_file():
        rev = request.args.get("rev", "HEAD")
        path = request.args["path"]
        try:
            data = v().read_file(rev, path)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 404
        truncated = len(data) > 200_000
        text = data[:200_000].decode("utf-8", errors="replace")
        return jsonify({
            "rev": rev, "path": path, "size": len(data),
            "truncated": truncated, "content": text,
            "binary": b"\0" in data[:2048],
        })

    @app.get("/api/diff")
    def api_diff():
        rev_from = request.args.get("from", "HEAD~1")
        rev_to = request.args.get("to", "HEAD")
        path = request.args.get("path") or None
        try:
            diff = v().diff(rev_from, rev_to, path)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"from": rev_from, "to": rev_to, "path": path, "diff": diff})

    @app.post("/api/restore")
    def api_restore():
        payload = request.get_json(force=True)
        rev = payload.get("rev", "HEAD~1")
        path = payload["path"]
        try:
            target = v().restore(path, rev)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({"restored": path, "from": target, "head": v().head_rev()})

    @app.post("/api/sync")
    def api_sync():
        rev = sync_once(v())
        return jsonify({"committed": bool(rev), "head": v().head_rev()})

    @app.get("/api/search")
    def api_search():
        query = request.args.get("q", "").strip()
        if not query:
            return jsonify([])
        results = SearchIndex(v()).query(query)
        return jsonify(results)

    @app.get("/api/snapshots")
    def api_snapshots():
        return jsonify(v().snapshots())

    return app


def serve(vault: Vault | None = None, port: int = 8787) -> None:
    app = create_app(vault)
    print(f"Time Machine on http://127.0.0.1:{port} — Ctrl-C to stop")
    app.run(host="127.0.0.1", port=port, debug=False)
