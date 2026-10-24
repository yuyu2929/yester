"""Encrypted mirror: key handling, sealing, round-trip unlock."""
from __future__ import annotations

import pytest

from yester import crypto


def test_init_encryption_and_roundtrip(vault, tmp_path):
    info = crypto.init_encryption(vault, "correct horse battery")
    assert info["recovery_code"].count("-") == 7
    assert vault.config.crypto_enabled

    (vault.root / "notes" / "secret.md").write_text(
        "launch codes: 0000\n", encoding="utf-8"
    )
    master = crypto.load_master_key(vault, passphrase="correct horse battery")
    manifest = crypto.build_mirror(vault, master)
    assert manifest["file_count"] >= 1
    # sealed payloads exist and the keyfile travels with the mirror
    assert list(vault.config.mirror_dir.rglob("*.enc"))
    assert (vault.config.mirror_dir / "vault.key").is_file()

    out = tmp_path / "restored"
    count = crypto.unlock_mirror(vault.config.mirror_dir, out,
                                 passphrase="correct horse battery")
    assert count == manifest["file_count"]
    restored = (out / "notes" / "secret.md").read_text(encoding="utf-8")
    assert restored == "launch codes: 0000\n"


def test_wrong_passphrase_rejected(vault):
    crypto.init_encryption(vault, "right passphrase")
    with pytest.raises(crypto.CryptoError):
        crypto.load_master_key(vault, passphrase="wrong passphrase")


def test_recovery_code_unlocks(vault, tmp_path):
    info = crypto.init_encryption(vault, "another passphrase")
    master = crypto.load_master_key(vault, recovery=info["recovery_code"])
    crypto.build_mirror(vault, master)
    out = tmp_path / "restored"
    count = crypto.unlock_mirror(vault.config.mirror_dir, out,
                                 recovery=info["recovery_code"])
    assert count >= 1


def test_sealed_bytes_are_authenticated(vault):
    crypto.init_encryption(vault, "passphrase123")
    master = crypto.load_master_key(vault, passphrase="passphrase123")
    sealed = crypto.seal_bytes(master, b"hello", aad=b"path")
    assert sealed != b"hello"
    assert crypto.open_bytes(master, sealed, aad=b"path") == b"hello"
    with pytest.raises(Exception):
        crypto.open_bytes(master, sealed, aad=b"other-path")
    with pytest.raises(crypto.CryptoError):
        crypto.open_bytes(master, b"not-a-sealed-blob")


def test_short_passphrase_rejected(vault):
    with pytest.raises(crypto.CryptoError):
        crypto.init_encryption(vault, "short")


def test_mirror_hides_file_names(vault):
    """The mirror must not reveal names or directory structure.

    0.1.0 wrote ``diary/secret.txt.enc`` plus a plaintext manifest.json, so a
    remote could read the whole tree even though the README promised
    end-to-end encryption.
    """
    crypto.init_encryption(vault, "passphrase123")
    (vault.root / "diary").mkdir(exist_ok=True)
    (vault.root / "diary" / "secret-plan.txt").write_text("meet at midnight",
                                                          encoding="utf-8")
    master = crypto.load_master_key(vault, passphrase="passphrase123")
    crypto.build_mirror(vault, master)

    mirror = vault.config.mirror_dir
    names = sorted(p.name for p in mirror.rglob("*") if p.is_file())
    joined = "\n".join(names)
    assert "secret" not in joined, names
    assert "diary" not in joined, names
    assert not (mirror / "manifest.json").exists(), "plaintext manifest written"
    assert (mirror / crypto.MANIFEST_SEALED).is_file()
    # every blob is flat and opaque
    blobs = [n for n in names if n.endswith(".enc") and n != crypto.MANIFEST_SEALED]
    assert blobs and all(len(n) == 68 for n in blobs), blobs  # sha256 hex + .enc

    # ... and it still round-trips
    out = tmp_path_restore(vault)
    assert (out / "diary" / "secret-plan.txt").read_text(encoding="utf-8") == \
        "meet at midnight"


def tmp_path_restore(vault):
    import tempfile
    from pathlib import Path
    out = Path(tempfile.mkdtemp()) / "restored"
    crypto.unlock_mirror(vault.config.mirror_dir, out,
                         passphrase="passphrase123")
    return out


def test_recovery_code_accepts_hand_typed_variants(vault):
    info = crypto.init_encryption(vault, "passphrase123")
    code = info["recovery_code"]
    for variant in (code, code.lower(), code.replace("-", ""), f"  {code}  "):
        assert crypto.load_master_key(vault, recovery=variant)


def test_manifest_path_traversal_is_refused(vault, tmp_path):
    """A crafted manifest must not be able to write outside the output dir."""
    crypto.init_encryption(vault, "passphrase123")
    master = crypto.load_master_key(vault, passphrase="passphrase123")
    blob = crypto.seal_bytes(master, b"pwned", aad=b"../escaped")
    mirror = vault.config.mirror_dir
    mirror.mkdir(parents=True, exist_ok=True)
    import json as _json
    (mirror / "evil.enc").write_bytes(blob)
    manifest = {"version": 2, "files": [{"path": "../escaped", "blob": "evil.enc"}]}
    (mirror / crypto.MANIFEST_SEALED).write_bytes(
        crypto.seal_bytes(master, _json.dumps(manifest).encode(), aad=crypto.MANIFEST_AAD))
    import shutil
    shutil.copy(vault.config.keys_dir / crypto.KEY_FILE, mirror / crypto.KEY_FILE)
    out = tmp_path / "restored"
    with pytest.raises(crypto.CryptoError):
        crypto.unlock_mirror(mirror, out, passphrase="passphrase123")
    assert not (tmp_path / "escaped").exists()


def test_mirror_hides_file_names(vault):
    """The mirror must not reveal names or directory structure.

    0.1.0 wrote ``diary/secret.txt.enc`` plus a plaintext manifest.json, so a
    remote could read the whole tree even though the README promised
    end-to-end encryption.
    """
    crypto.init_encryption(vault, "passphrase123")
    (vault.root / "diary").mkdir(exist_ok=True)
    (vault.root / "diary" / "secret-plan.txt").write_text("meet at midnight",
                                                          encoding="utf-8")
    master = crypto.load_master_key(vault, passphrase="passphrase123")
    crypto.build_mirror(vault, master)

    mirror = vault.config.mirror_dir
    names = sorted(p.name for p in mirror.rglob("*") if p.is_file())
    joined = "\n".join(names)
    assert "secret" not in joined, names
    assert "diary" not in joined, names
    assert not (mirror / "manifest.json").exists(), "plaintext manifest written"
    assert (mirror / crypto.MANIFEST_SEALED).is_file()
    # every blob is flat and opaque
    blobs = [n for n in names if n.endswith(".enc") and n != crypto.MANIFEST_SEALED]
    assert blobs and all(len(n) == 68 for n in blobs), blobs  # sha256 hex + .enc

    # ... and it still round-trips
    out = tmp_path_restore(vault)
    assert (out / "diary" / "secret-plan.txt").read_text(encoding="utf-8") == \
        "meet at midnight"


def tmp_path_restore(vault):
    import tempfile
    from pathlib import Path
    out = Path(tempfile.mkdtemp()) / "restored"
    crypto.unlock_mirror(vault.config.mirror_dir, out,
                         passphrase="passphrase123")
    return out


def test_recovery_code_accepts_hand_typed_variants(vault):
    info = crypto.init_encryption(vault, "passphrase123")
    code = info["recovery_code"]
    for variant in (code, code.lower(), code.replace("-", ""), f"  {code}  "):
        assert crypto.load_master_key(vault, recovery=variant)


def test_manifest_path_traversal_is_refused(vault, tmp_path):
    """A crafted manifest must not be able to write outside the output dir."""
    crypto.init_encryption(vault, "passphrase123")
    master = crypto.load_master_key(vault, passphrase="passphrase123")
    blob = crypto.seal_bytes(master, b"pwned", aad=b"../escaped")
    mirror = vault.config.mirror_dir
    mirror.mkdir(parents=True, exist_ok=True)
    import json as _json
    (mirror / "evil.enc").write_bytes(blob)
    manifest = {"version": 2, "files": [{"path": "../escaped", "blob": "evil.enc"}]}
    (mirror / crypto.MANIFEST_SEALED).write_bytes(
        crypto.seal_bytes(master, _json.dumps(manifest).encode(), aad=crypto.MANIFEST_AAD))
    import shutil
    shutil.copy(vault.config.keys_dir / crypto.KEY_FILE, mirror / crypto.KEY_FILE)
    out = tmp_path / "restored"
    with pytest.raises(crypto.CryptoError):
        crypto.unlock_mirror(mirror, out, passphrase="passphrase123")
    assert not (tmp_path / "escaped").exists()
