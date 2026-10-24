# Changelog

All notable changes to Yester are documented here. The encrypted mirror has a
format version (`yester/crypto.py`: `MANIFEST_VERSION`); readers support the
previous layout.

## [0.1.1] - 2026-10-25

Security and honesty release. The encrypted mirror did not do what its own
docstring promised, and the paid-feature list advertised things that do not
exist.

### Fixed

- **The encrypted mirror leaked every file name and the whole folder tree.**
  `build_mirror` wrote `<plaintext path>.enc` (e.g. `diary/secret-plan.txt.enc`)
  and a **plaintext** `manifest.json` containing paths, sizes and mtimes —
  while the module docstring claimed "the remote host cannot read file contents
  or names". Blobs are now stored flat as `sha256(path).enc` and the
  path→blob map lives in `manifest.enc`, sealed with the master key. A remote
  now sees: file count, ciphertext sizes, and that it is a Yester mirror.
  Legacy (v1) mirrors still restore.
- **Path traversal in `unlock_mirror`.** Restoration now takes paths from a
  manifest, so a crafted manifest could have written outside the output
  directory (`../…`, absolute paths, `C:` prefixes). Paths are validated
  before any write. (0.1.0 derived paths from directory entries and was not
  exploitable this way; the new layout made the check necessary.)
- **Recovery codes only worked in the exact issued format.** Typing the code
  without dashes, or in lowercase, derived a different key and reported
  "wrong passphrase". Codes are normalised (case, separators, whitespace) and
  validated as 32 hex characters with a clear error otherwise.
- **Five of the seven "paid features" were never implemented.** `scan-hook`,
  `publish-custom-domain`, `semantic-search`, `private-deployment` and
  `sso-config` were listed in `FEATURE_TIERS`, but nothing in the codebase
  gated them. Meanwhile `scan`/`hook` actually shipped free, so the README
  sold a paid feature that was already free. The paid list is now exactly
  `audit-export` and `retention-mode`; the rest moved to `PLANNED_FEATURES`
  (refused for every tier) and `scan-hook` to `FREE_FEATURES`.
- **`yester mirror` / `encrypt-init` / `unlock` could hang forever.** They
  called `getpass`, which on Windows reads the console through `msvcrt` and
  ignores stdin redirection — so in cron, systemd or CI the command waited on
  a prompt nobody could answer. Passphrases now resolve from `--passphrase`,
  then `$YESTER_PASSPHRASE`, then one line of stdin, and otherwise fail with a
  message and exit 2.
- **`yester mirror` / `encrypt-init` / `unlock` could hang forever.** They
  called `getpass`, which on Windows reads the console through `msvcrt` and
  ignores stdin redirection — so in cron, systemd or CI the command waited on
  a prompt nobody could answer. Passphrases now resolve from `--passphrase`,
  then `$YESTER_PASSPHRASE`, then one line of stdin, and otherwise fail with a
  message and exit 2.
- Package metadata pointed at `github.com/YOUR_NAME/yester`; now the real URL.

### Added

- `COMMERCIAL.md`: what the Team/Enterprise editions include, and an explicit
  statement that the offline key check is a product boundary rather than a
  security boundary.
- Tests for name-hiding, manifest path traversal and recovery-code
  normalisation (`tests/test_crypto.py`), and a paid-list consistency check.

### Changed

- README: editions table matches the implementation; badges; security section
  states exactly what the mirror reveals and what it does not.

## [0.1.0] - 2024

Initial release: git-backed vaults, watcher, time-machine UI, publishing,
full-text search, encrypted mirror, archive/inheritance, compliance tooling.
