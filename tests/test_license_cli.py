"""Licence gate and CLI end-to-end smoke tests."""
from __future__ import annotations

import pytest

from yester import license as license_mod


def test_issue_and_validate_key():
    key = license_mod.issue_key("team", "ABC12345")
    assert license_mod.validate_key(key) == "team"
    assert license_mod.validate_key(key.lower()) == "team"
    assert license_mod.validate_key("YESTER-TEAM-ABC12345-DEADBEEF") is None
    assert license_mod.validate_key("garbage") is None
    with pytest.raises(license_mod.LicenseError):
        license_mod.issue_key("free")


def test_feature_gate():
    team_key = license_mod.issue_key("team")
    enterprise_key = license_mod.issue_key("enterprise")
    license_mod.require("audit-export", enterprise_key)   # enterprise >= team
    license_mod.require("audit-export", team_key)         # ok
    with pytest.raises(license_mod.LicenseError):
        license_mod.require("audit-export", "")            # free tier
    # paid list must only contain features that are actually gated somewhere
    for feature in license_mod.FEATURE_TIERS:
        assert feature not in license_mod.PLANNED_FEATURES, feature
    # roadmap features are refused for every tier, including enterprise
    with pytest.raises(license_mod.LicenseError):
        license_mod.require("sso-config", enterprise_key)
    # free features and unknown names never gate
    license_mod.require("scan-hook", "")
    license_mod.require("anything", "")


def test_cli_full_flow(tmp_path, capsys, monkeypatch):
    from yester.cli import main

    vault_dir = tmp_path / "my vault"
    assert main(["init", str(vault_dir)]) == 0
    out = capsys.readouterr().out
    assert "initialised vault" in out

    monkeypatch.chdir(vault_dir)
    (vault_dir / "notes").mkdir()
    (vault_dir / "notes" / "idea.md").write_text(
        "# Idea\n\nbuild a time machine for files\n", encoding="utf-8"
    )

    assert main(["sync"]) == 0
    assert "committed" in capsys.readouterr().out

    assert main(["snapshot", "first idea"]) == 0
    assert "snap/" in capsys.readouterr().out

    assert main(["status"]) == 0
    status_out = capsys.readouterr().out
    assert "device" in status_out

    assert main(["log"]) == 0
    assert "snapshot" in capsys.readouterr().out

    assert main(["search", "time machine", "--reindex"]) == 0
    assert "idea.md" in capsys.readouterr().out

    assert main(["activate", license_mod.issue_key("team")]) == 0
    assert main(["audit", "--format", "csv"]) == 0
    assert "device,kind" in capsys.readouterr().out

    assert main(["version"]) == 0
    assert "yester" in capsys.readouterr().out


def test_cli_restore(tmp_path, capsys, monkeypatch):
    from yester.cli import main

    vault_dir = tmp_path / "v"
    main(["init", str(vault_dir)])
    monkeypatch.chdir(vault_dir)
    target = vault_dir / "doc.txt"
    target.write_text("good\n", encoding="utf-8")
    main(["sync"])
    target.write_text("broken\n", encoding="utf-8")
    main(["sync"])
    assert main(["restore", "doc.txt"]) == 0
    assert target.read_text(encoding="utf-8") == "good\n"


def test_cli_outside_vault_fails_cleanly(tmp_path, capsys, monkeypatch):
    from yester.cli import main

    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["status"])
    assert exc.value.code == 2
    assert "not inside a yester vault" in capsys.readouterr().err
