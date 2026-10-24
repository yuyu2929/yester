"""Cross-version search: English and CJK text."""
from __future__ import annotations

from yester import search


def test_index_and_query_finds_old_revision(vault):
    (vault.root / "notes" / "hello.md").write_text(
        "# Recipe\n\nsourdough starter needs flour and water\n", encoding="utf-8"
    )
    vault.snapshot("recipe draft")
    (vault.root / "notes" / "hello.md").write_text(
        "# Notes\n\nnothing about baking anymore\n", encoding="utf-8"
    )
    vault.auto_commit()

    index = search.SearchIndex(vault)
    info = index.build()
    assert info["indexed_documents"] >= 2

    results = index.query("sourdough")
    assert results, "expected a hit for 'sourdough'"
    top = results[0]
    assert top["path"] == "notes/hello.md"
    assert "sourdough" in top["snippet"].lower()
    # the hit is from the snapshot revision, not the latest
    assert top["rev"] != vault.head_rev_full()


def test_chinese_bigram_search(vault):
    (vault.root / "notes" / "cn.md").write_text(
        "# 旅行计划\n\n下个月去云南大理，记得带相机和防晒霜。\n", encoding="utf-8"
    )
    vault.auto_commit()
    results = search.SearchIndex(vault).query("防晒霜")
    assert results
    assert results[0]["path"] == "notes/cn.md"


def test_empty_query_returns_nothing(vault):
    assert search.SearchIndex(vault).query("   ") == []


def test_tokenizer_handles_mixed_text():
    tokens = search.tokenize("hello world 你好世界")
    assert "hello" in tokens
    assert any("\u4f60" in t for t in tokens)  # CJK bigram containing 你
