"""Compliance: audit export, secret scanning, retention mode."""
from __future__ import annotations

import json


def test_audit_export_json_and_csv(vault):
    (vault.root / "notes" / "hello.md").write_text("# Hello\n\nv2\n", encoding="utf-8")
    vault.snapshot("milestone")

    from yester import audit

    payload = audit.export_audit(vault, fmt="json")
    data = json.loads(payload)
    assert data["record_count"] >= 2
    kinds = {r["kind"] for r in data["records"]}
    assert "snapshot" in kinds
    record = data["records"][0]
    assert record["device"] == "testbox"
    assert "timestamp" in record

    csv_text = audit.export_audit(vault, fmt="csv")
    assert "device,kind,subject" in csv_text


def test_scan_detects_fake_credentials(vault):
    (vault.root / "config.env").write_text(
        "AWS_KEY=AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8"
    )
    (vault.root / "clean.txt").write_text("nothing to see here\n", encoding="utf-8")

    from yester import audit

    findings = audit.scan_files(vault)
    patterns = {f["pattern"] for f in findings}
    assert "aws-access-key-id" in patterns
    # secrets are redacted in reports
    assert all("EXAMPLE" not in f["match"] or f["match"].endswith("**")
               for f in findings)


def test_scan_clean_vault(vault):
    from yester import audit

    assert audit.scan_files(vault) == []


def test_retention_mode_toggles(vault):
    from yester import audit

    audit.set_retention(vault, True)
    assert vault.config.retention
    assert (vault.root / "RETENTION.md").is_file()
    assert vault._git("config", "--get", "gc.auto").stdout.strip() == "0"
    audit.set_retention(vault, False)
    assert not vault.config.retention
