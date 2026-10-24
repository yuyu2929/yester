"""Filesystem watching with debounced auto-sync."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from .config import CONFIG_DIRNAME
from .gitops import Vault, open_vault

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "watchdog is required for `yester watch` — pip install watchdog"
    ) from exc


class _Handler(FileSystemEventHandler):
    def __init__(self, dirty: set[str], lock: threading.Lock):
        self._dirty = dirty
        self._lock = lock

    def _mark(self, path: str) -> None:
        with self._lock:
            self._dirty.add(path)

    def on_any_event(self, event) -> None:
        if event.is_directory:
            return
        src = str(getattr(event, "src_path", "") or "")
        dest = str(getattr(event, "dest_path", "") or "")
        for candidate in (src, dest):
            if candidate and f"/{CONFIG_DIRNAME}/" not in candidate and "/.git/" not in candidate:
                self._mark(candidate)


def sync_once(vault: Vault | None = None,
              on_message: Callable[[str], None] | None = None) -> str | None:
    """Perform a single auto-sync cycle.  Returns the commit rev or None."""
    vault = vault or open_vault()
    rev = vault.auto_commit()
    message = f"synced {vault.head_rev()}" if rev else "nothing to sync"
    if on_message:
        on_message(message)
    return rev


def watch(vault: Vault | None = None,
          interval: int | None = None,
          on_message: Callable[[str], None] | None = None) -> None:
    """Blocking watch loop: commits on debounce or at *interval* seconds."""
    vault = vault or open_vault()
    interval = interval or vault.config.sync_interval
    debounce = vault.config.debounce_seconds

    dirty: set[str] = set()
    lock = threading.Lock()
    handler = _Handler(dirty, lock)
    observer = Observer()
    observer.schedule(handler, str(vault.root), recursive=True)
    observer.start()

    def say(msg: str) -> None:
        if on_message:
            on_message(msg)

    say(f"watching {vault.root} (interval {interval}s, debounce {debounce}s) — Ctrl-C to stop")
    last_change = 0.0
    last_sync = time.monotonic()
    try:
        while True:
            time.sleep(1)
            now = time.monotonic()
            with lock:
                pending = len(dirty)
                if pending:
                    last_change = now
                    dirty.clear()
            # Commit when changes have settled for the debounce window,
            # or when the interval elapses with pending work.
            due_debounce = pending and (now - last_change) >= debounce
            due_interval = (now - last_sync) >= interval
            if due_debounce or (due_interval and vault.status()):
                rev = vault.auto_commit()
                last_sync = now
                say(f"[{time.strftime('%H:%M:%S')}] "
                    + (f"committed {rev}" if rev else "nothing to commit"))
    except KeyboardInterrupt:
        say("stopping watcher")
    finally:
        observer.stop()
        observer.join()
