"""Publishing: Markdown rendering, index generation, site repo."""
from __future__ import annotations


def test_build_site_renders_markdown(vault):
    (vault.root / "public").mkdir()
    (vault.root / "public" / "hello.md").write_text(
        "# Hello World\n\nSome **bold** text.\n", encoding="utf-8"
    )
    (vault.root / "public" / "style.css").write_text("body{}", encoding="utf-8")
    from yester import publisher

    site = publisher.build_site(vault)
    page = (site / "hello.html").read_text(encoding="utf-8")
    assert "<h1" in page and "Hello World" in page
    assert "<strong>bold</strong>" in page
    assert (site / "style.css").read_text(encoding="utf-8") == "body{}"
    assert (site / "index.html").is_file()


def test_publish_without_push_creates_site_repo(vault):
    (vault.root / "public").mkdir()
    (vault.root / "public" / "a.md").write_text("# A\n", encoding="utf-8")
    from yester import publisher

    rev = publisher.publish(vault, push=False)
    assert rev
    site = vault.config.site_dir
    assert (site / ".git").is_dir()
    assert (site / "a.html").is_file()
    # publishing is recorded in the vault history
    assert any(e["kind"] == "publish" for e in vault.log())


def test_publish_without_public_dir_fails(vault):
    import pytest

    from yester import publisher
    from yester.gitops import GitError

    with pytest.raises(GitError):
        publisher.publish(vault, push=False)
