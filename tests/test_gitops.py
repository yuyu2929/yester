"""Vault lifecycle, sync, snapshots, restore, promote, blob isolation."""
from __future__ import annotations

import json


def test_init_creates_repo_and_device_branch(vault):
    assert vault.is_repo()
    assert vault.current_branch() == "devices/testbox"
    assert (vault.root / ".yester" / "config.yml").is_file()
    assert (vault.root / ".yester" / ".gitignore").is_file()


def test_auto_commit_records_changes(vault):
    (vault.root / "notes" / "hello.md").write_text("# Hello\n\nedited\n", encoding="utf-8")
    (vault.root / "new.txt").write_text("brand new\n", encoding="utf-8")
    rev = vault.auto_commit()
    assert rev is not None
    assert "new.txt" in vault.changed_files(rev)
    # idempotent: nothing pending afterwards
    assert vault.auto_commit() is None


def test_snapshot_creates_annotated_tag(vault):
    (vault.root / "notes" / "hello.md").write_text("# Hello\n\nv2\n", encoding="utf-8")
    tag, rev = vault.snapshot("before big edit")
    assert tag.startswith("snap/")
    assert tag.endswith("before-big-edit")
    snapshots = vault.snapshots()
    assert any(s["tag"] == tag for s in snapshots)


def test_restore_rolls_back_file(vault):
    original = (vault.root / "notes" / "hello.md").read_text(encoding="utf-8")
    (vault.root / "notes" / "hello.md").write_text("# Hello\n\ndestroyed\n", encoding="utf-8")
    vault.auto_commit()
    target = vault.restore("notes/hello.md", "HEAD~1")
    assert (vault.root / "notes" / "hello.md").read_text(encoding="utf-8") == original
    assert target == "HEAD~1"
    # restore itself is recorded in history
    assert any(e["kind"] == "restore" for e in vault.log())


def test_promote_renames_previous_main(vault):
    backup = vault.promote()
    assert backup.startswith("main-backup-")
    # the backup branch exists and main now tracks the device branch
    assert vault._git("rev-parse", "--verify", backup).returncode == 0
    main_rev = vault._git("rev-parse", "main").stdout.strip()
    device_rev = vault._git("rev-parse", vault.device_branch).stdout.strip()
    assert main_rev == device_rev


def test_oversized_files_go_to_blob_store(tiny_blob_vault):
    vault = tiny_blob_vault
    (vault.root / "big.bin").write_bytes(b"x" * 1024)
    vault.auto_commit()
    pointer = vault.root / "big.bin.yblob"
    assert pointer.is_file()
    import json
    meta = json.loads(pointer.read_text(encoding="utf-8"))
    assert meta["yester"] == "blob-pointer"
    assert meta["size"] == 1024
    # the raw payload lives in the blob store, not in git
    blobs = list(vault.config.blobs_dir.rglob("*"))
    assert any(b.is_file() for b in blobs)
    tracked = vault._git("ls-files").stdout.split()
    assert "big.bin" not in tracked
    assert "big.bin.yblob" in tracked


def test_read_file_and_tree_across_revisions(vault):
    (vault.root / "notes" / "hello.md").write_text("# Hello\n\nv2\n", encoding="utf-8")
    vault.auto_commit()
    head = vault.head_rev_full()
    assert b"v2" in vault.read_file(head, "notes/hello.md")
    assert b"first version" in vault.read_file("HEAD~1", "notes/hello.md")
    assert "notes/hello.md" in vault.list_tree(head)


def test_blob_managed_file_edits_are_still_committed(tiny_blob_vault):
    """Regression: blob-managed files are git-ignored, so edits to them
    must be detected by refreshing pointers, not by git status."""
    vault = tiny_blob_vault
    big = vault.root / "big.bin"
    big.write_bytes(b"first payload")
    vault.auto_commit()

    pointer = vault.root / "big.bin.yblob"
    first_sha = json.loads(pointer.read_text(encoding="utf-8"))["sha256"]

    # user edits the (ignored) file — nothing shows up in git status
    big.write_bytes(b"second payload, much longer")
    assert not [e for e in vault.status() if e[1] == "big.bin"]

    rev = vault.auto_commit()
    assert rev is not None, "edit to a blob-managed file must be committed"
    second_sha = json.loads(pointer.read_text(encoding="utf-8"))["sha256"]
    assert first_sha != second_sha
    blobs = [b for b in vault.config.blobs_dir.rglob("*") if b.is_file()]
    assert len(blobs) == 2


def test_unchanged_blob_file_produces_no_commit(tiny_blob_vault):
    vault = tiny_blob_vault
    (vault.root / "big.bin").write_bytes(b"payload")
    vault.auto_commit()
    # touching nothing: no pointer rewrite, no commit
    assert vault.auto_commit() is None


def test_stats_reports_pending_and_blobs(tiny_blob_vault):
    vault = tiny_blob_vault
    (vault.root / "another.bin").write_bytes(b"y" * 512)
    stats = vault.stats()
    assert stats["name"] == "test-vault"
    assert stats["device"] == "testbox"
    assert stats["branch"].startswith("devices/")
