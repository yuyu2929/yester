"""End-to-end encryption for the cloud mirror.

Threat model
------------
The plaintext vault never leaves the machine.  Before anything is pushed
to a remote, Yester builds an *encrypted mirror*: every file is sealed
with AES-256-GCM under a random master key.  The master key is wrapped
twice — once by a key derived from the user's passphrase (scrypt) and
once by a random recovery code — and stored locally in
``.yester/keys/vault.key`` (mode 0600).

What the mirror does NOT reveal:

* file **contents** (AES-256-GCM, AAD-bound to the path so a blob cannot be
  swapped between names);
* file **names** and the **directory structure**.  Blobs are stored flat under
  ``sha256(path).enc`` and the path → blob mapping lives in ``manifest.enc``,
  which is itself sealed.  (The 0.1.0 layout wrote ``diary/secret.txt.enc``
  and a plaintext ``manifest.json``, which leaked both — see CHANGELOG.)

What it DOES reveal, unavoidably, at the remote:

* the number of files and each ciphertext's size (≈ plaintext size + 36 bytes),
* modification times of the blobs when the mirror is committed to git,
* that this is a Yester mirror at all (the ``.yester``-era file names remain).

Consequences:

* A stolen/leaked mirror repository is useless without the passphrase or
  the recovery code.
* Losing ``.yester/keys`` *and* the passphrase *and* the recovery code
  means the mirror cannot be decrypted.  This is documented, not hidden.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .config import CONFIG_DIRNAME
from .gitops import Vault

KEY_FILE = "vault.key"
MAGIC = b"YESTER1"
CHECK_PLAINTEXT = b"yester-key-check-v1"

#: sealed manifest (v2 layout); the legacy plaintext one is only read, never written
MANIFEST_SEALED = "manifest.enc"
MANIFEST_LEGACY = "manifest.json"
MANIFEST_AAD = b"yester-manifest-v2"
MANIFEST_VERSION = 2


class CryptoError(Exception):
    """Raised for key derivation, encryption or decryption failures."""


# ----------------------------------------------------------------------
# key handling
# ----------------------------------------------------------------------
def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


def _derive(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1)
    return kdf.derive(passphrase.encode("utf-8"))


def _wrap(key: bytes, plaintext: bytes) -> str:
    """AES-GCM wrap: nonce || ciphertext, base64 encoded."""
    nonce = secrets.token_bytes(12)
    sealed = AESGCM(key).encrypt(nonce, plaintext, None)
    return _b64(nonce + sealed)


def _unwrap(key: bytes, blob: str) -> bytes:
    raw = _unb64(blob)
    nonce, sealed = raw[:12], raw[12:]
    return AESGCM(key).decrypt(nonce, sealed, None)


def _recovery_code() -> str:
    groups = [secrets.token_hex(2).upper() for _ in range(8)]
    return "-".join(groups)


def _normalise_recovery(code: str) -> str:
    """Accept the code with or without dashes, in any case.

    The issued format is ``XXXX-XXXX-...`` (8 groups).  Typing it back without
    the dashes used to derive a different key and report "wrong passphrase",
    which is a terrible failure mode for something people copy by hand.
    """
    raw = "".join(ch for ch in code if ch.isalnum()).upper()
    if len(raw) != 32 or any(ch not in "0123456789ABCDEF" for ch in raw):
        raise CryptoError(
            f"recovery code should be 32 hex characters, got {len(raw)}"
        )
    return "-".join(raw[i:i + 4] for i in range(0, 32, 4))


def _blob_name(rel_str: str) -> str:
    """Opaque, stable blob name: the path is not recoverable from it."""
    return hashlib.sha256(rel_str.encode("utf-8")).hexdigest() + ".enc"


def _safe_rel(rel: str) -> str:
    """Reject absolute paths and traversal in a manifest-supplied path.

    Once paths come from a file (rather than from directory entries), a
    crafted manifest could otherwise write outside the output directory.
    """
    if not rel or "\0" in rel:
        raise CryptoError(f"unsafe path in manifest: {rel!r}")
    posix = PurePosixPath(rel)
    if posix.is_absolute() or PureWindowsPath(rel).is_absolute():
        raise CryptoError(f"unsafe path in manifest: {rel!r}")
    parts = posix.parts
    if any(part in ("..", ".") or ":" in part for part in parts):
        raise CryptoError(f"unsafe path in manifest: {rel!r}")
    return posix.as_posix()


def init_encryption(vault: Vault, passphrase: str) -> dict[str, str]:
    """Create the master key, wrapped by passphrase and recovery code."""
    if len(passphrase) < 8:
        raise CryptoError("passphrase must be at least 8 characters")
    if vault.config.crypto_enabled:
        raise CryptoError("encryption already initialised")

    salt = secrets.token_bytes(16)
    master = secrets.token_bytes(32)
    recovery = _recovery_code()

    pass_key = _derive(passphrase, salt)
    recovery_key = _derive(recovery, salt)

    keyfile = {
        "yester": "keyfile",
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "kdf": "scrypt(n=32768,r=8,p=1)",
        "algorithm": "AES-256-GCM",
        "salt": _b64(salt),
        "verifier": _wrap(master, CHECK_PLAINTEXT),
        "wrapped_by_passphrase": _wrap(pass_key, master),
        "wrapped_by_recovery": _wrap(recovery_key, master),
    }
    keys_dir = vault.config.keys_dir
    keys_dir.mkdir(parents=True, exist_ok=True)
    path = keys_dir / KEY_FILE
    path.write_text(json.dumps(keyfile, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)
    vault.config.set("crypto.enabled", True)
    return {"recovery_code": recovery, "keyfile": str(path)}


def load_master_key(vault: Vault, passphrase: str | None = None,
                    recovery: str | None = None) -> bytes:
    path = vault.config.keys_dir / KEY_FILE
    if not path.is_file():
        raise CryptoError(f"no keyfile at {path} — run `yester encrypt-init` first")
    keyfile = json.loads(path.read_text(encoding="utf-8"))
    salt = _unb64(keyfile["salt"])

    if passphrase:
        wrapped = keyfile["wrapped_by_passphrase"]
        candidate = _derive(passphrase, salt)
    elif recovery:
        wrapped = keyfile["wrapped_by_recovery"]
        candidate = _derive(_normalise_recovery(recovery), salt)
    else:
        raise CryptoError("provide a passphrase or a recovery code")

    try:
        master = _unwrap(candidate, wrapped)
        # verifier proves the unwrapped key is the real master key
        _unwrap(master, keyfile["verifier"])
    except Exception as exc:
        raise CryptoError("wrong passphrase or recovery code") from exc
    return master


# ----------------------------------------------------------------------
# file sealing
# ----------------------------------------------------------------------
def seal_bytes(master: bytes, data: bytes, aad: bytes | None = None) -> bytes:
    nonce = secrets.token_bytes(12)
    sealed = AESGCM(master).encrypt(nonce, data, aad)
    return MAGIC + nonce + sealed


def open_bytes(master: bytes, blob: bytes, aad: bytes | None = None) -> bytes:
    if not blob.startswith(MAGIC):
        raise CryptoError("not a yester sealed blob")
    nonce, sealed = blob[len(MAGIC):len(MAGIC) + 12], blob[len(MAGIC) + 12:]
    return AESGCM(master).decrypt(nonce, sealed, aad)


def _is_probably_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\0" in fh.read(2048)
    except OSError:
        return True


def _iter_vault_files(vault: Vault):
    for path in sorted(vault.root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(vault.root)
        parts = rel.parts
        if any(p in (CONFIG_DIRNAME, ".git") for p in parts):
            continue
        yield path, rel


def build_mirror(vault: Vault, master: bytes) -> dict:
    """Encrypt every vault file into ``.yester/mirror`` and write a manifest.

    Layout (v2): blobs are flat, named ``sha256(path).enc``, and the mapping
    from path to blob lives in ``manifest.enc`` — sealed with the same master
    key.  A remote therefore cannot see file names or the directory tree,
    which the 0.1.0 layout leaked through ``dir/name.txt.enc`` paths and a
    plaintext ``manifest.json``.
    """
    mirror = vault.config.mirror_dir
    if mirror.exists():
        for stale in mirror.rglob("*"):
            if stale.is_file():
                stale.unlink()
    mirror.mkdir(parents=True, exist_ok=True)

    manifest_files = []
    for path, rel in _iter_vault_files(vault):
        data = path.read_bytes()
        rel_str = rel.as_posix()
        sealed = seal_bytes(master, data, aad=rel_str.encode("utf-8"))
        blob_name = _blob_name(rel_str)
        (mirror / blob_name).write_bytes(sealed)
        manifest_files.append({
            "path": rel_str,
            "blob": blob_name,
            "size": len(data),
            "encrypted_size": len(sealed),
            "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        })

    manifest = {
        "yester": "mirror-manifest",
        "version": MANIFEST_VERSION,
        "vault": vault.config.name,
        "device": vault.config.device,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "algorithm": vault.config.data["crypto"]["algorithm"],
        "file_count": len(manifest_files),
        "files": manifest_files,
    }
    (mirror / MANIFEST_SEALED).write_bytes(
        seal_bytes(master, json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
                   aad=MANIFEST_AAD)
    )
    # The keyfile only holds *wrapped* keys — useless without the
    # passphrase or recovery code, and required to decrypt the mirror.
    keyfile = vault.config.keys_dir / KEY_FILE
    if keyfile.is_file():
        (mirror / KEY_FILE).write_text(
            keyfile.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return manifest


def _read_manifest(mirror_dir: Path, master: bytes) -> dict:
    """Return the manifest; reads the legacy plaintext one if present.

    Legacy (v1) manifests have no "blob" field, so `unlock_mirror` falls back
    to ``path + ".enc"`` — that is what the old layout wrote.
    """
    sealed = mirror_dir / MANIFEST_SEALED
    if sealed.is_file():
        try:
            data = open_bytes(master, sealed.read_bytes(), aad=MANIFEST_AAD)
            return json.loads(data.decode("utf-8"))
        except Exception as exc:
            raise CryptoError("manifest failed to decrypt (wrong key?)") from exc
    legacy = mirror_dir / MANIFEST_LEGACY
    if legacy.is_file():
        return json.loads(legacy.read_text(encoding="utf-8"))
    raise CryptoError(
        "no manifest in mirror: this does not look like a Yester mirror"
    )


def unlock_mirror(mirror_dir: Path, out_dir: Path,
                  passphrase: str | None = None,
                  recovery: str | None = None) -> int:
    """Decrypt a mirror repository back into plaintext at *out_dir*."""
    mirror_dir = Path(mirror_dir)
    keyfile_path = mirror_dir / KEY_FILE
    if not keyfile_path.is_file():
        raise CryptoError(
            f"no keyfile in mirror — the keyfile lives only on the machine "
            f"that ran `yester encrypt-init`; copy it here or use the "
            f"recovery code with `yester unlock --recovery`"
        )
    keyfile = json.loads(keyfile_path.read_text(encoding="utf-8"))
    salt = _unb64(keyfile["salt"])
    if passphrase:
        candidate = _derive(passphrase, salt)
        wrapped = keyfile["wrapped_by_passphrase"]
    elif recovery:
        candidate = _derive(_normalise_recovery(recovery), salt)
        wrapped = keyfile["wrapped_by_recovery"]
    else:
        raise CryptoError("provide a passphrase or a recovery code")
    try:
        master = _unwrap(candidate, wrapped)
        _unwrap(master, keyfile["verifier"])
    except Exception as exc:
        raise CryptoError("wrong passphrase or recovery code") from exc

    manifest = _read_manifest(mirror_dir, master)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for entry in manifest.get("files", []):
        original = _safe_rel(entry.get("path", ""))
        blob_name = entry.get("blob") or (original + ".enc")
        blob_path = mirror_dir / blob_name
        if not blob_path.is_file():
            raise CryptoError(f"mirror is missing blob for {original!r}")
        data = open_bytes(master, blob_path.read_bytes(),
                          aad=original.encode("utf-8"))
        target = out_dir / original
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        count += 1
    return count
