"""Cross-version search: find any sentence you ever wrote.

The index covers the current tree, every snapshot tag and the most recent
commits.  Documents are scored with TF-IDF cosine similarity; CJK text is
segmented into character bigrams so Chinese notes are searchable without
external dependencies.  The index lives in ``.yester/index/search.json``
and can be rebuilt at any time.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .gitops import Vault

INDEX_FILE = "search.json"
MAX_FILE_BYTES = 512 * 1024
MAX_REVS = 40

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is",
    "are", "was", "were", "be", "been", "with", "as", "at", "by", "it",
    "this", "that", "的", "了", "和", "是", "在", "我", "有", "就", "不",
    "人", "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去",
}


def tokenize(text: str) -> list[str]:
    """ASCII words plus CJK bigrams, stopwords removed."""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        token = match.group(0)
        if re.fullmatch(r"[\u4e00-\u9fff]", token):
            continue  # handled via bigrams below
        if token.lower() not in _STOPWORDS:
            tokens.append(token.lower())
    for run in _CJK_RE.findall(text):
        for i in range(len(run) - 1):
            bigram = run[i:i + 2]
            if bigram not in _STOPWORDS:
                tokens.append(bigram)
        if len(run) == 1 and run not in _STOPWORDS:
            tokens.append(run)
    return tokens


class SearchIndex:
    def __init__(self, vault: Vault):
        self.vault = vault
        self.path = vault.config.index_dir / INDEX_FILE

    # ------------------------------------------------------------------
    def _docs(self) -> Iterable[tuple[str, str, str]]:
        """Yield ``(rev, path, text)`` for indexable documents."""
        revs: list[str] = []
        try:
            head = self.vault.head_rev_full()
            revs.append(head)
        except Exception:
            return
        for snap in self.vault.snapshots():
            revs.append(snap["tag"])
        for entry in self.vault.log(limit=MAX_REVS):
            revs.append(entry["rev"])
        seen: set[str] = set()
        for rev in revs:
            if rev in seen:
                continue
            seen.add(rev)
            try:
                files = self.vault.list_tree(rev)
            except Exception:
                continue
            for path in files:
                if path.endswith((".yblob", ".enc")) or "/.yester/" in path:
                    continue
                try:
                    data = self.vault.read_file(rev, path)
                except Exception:
                    continue
                if len(data) > MAX_FILE_BYTES or b"\0" in data[:2048]:
                    continue
                yield rev, path, data.decode("utf-8", errors="replace")

    def build(self) -> dict[str, Any]:
        docs: dict[str, dict[str, Any]] = {}
        for rev, path, text in self._docs():
            tokens = tokenize(text)
            if not tokens:
                continue
            key = f"{rev[:10]}:{path}"
            docs[key] = {
                "rev": rev, "path": path, "tf": dict(Counter(tokens)),
                "length": len(tokens),
                "snippet": text[:400],
            }
        # document frequency across the corpus
        df: Counter[str] = Counter()
        for doc in docs.values():
            for term in doc["tf"]:
                df[term] += 1
        n = max(len(docs), 1)
        for doc in docs.values():
            doc["tfidf"] = {
                term: (1 + math.log(count)) * math.log((n + 1) / (df[term] + 0.5))
                for term, count in doc["tf"].items()
            }
            doc["norm"] = math.sqrt(sum(v * v for v in doc["tfidf"].values())) or 1.0
        index = {"docs": docs, "doc_count": n}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(index), encoding="utf-8")
        return {"indexed_documents": n}

    # ------------------------------------------------------------------
    def query(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        if not self.path.exists():
            self.build()
        index = json.loads(self.path.read_text(encoding="utf-8"))
        docs = index.get("docs", {})
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        q_tf = Counter(q_tokens)

        results = []
        for key, doc in docs.items():
            score = 0.0
            for term, count in q_tf.items():
                weight = doc["tfidf"].get(term)
                if weight is None:
                    continue
                q_weight = 1 + math.log(count)
                score += q_weight * weight
            if score <= 0:
                continue
            score /= doc["norm"]
            snippet = _snippet(doc["snippet"], q_tokens)
            results.append({
                "rev": doc["rev"], "path": doc["path"],
                "score": round(score, 4), "snippet": snippet,
            })
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:limit]


def _snippet(text: str, terms: list[str], width: int = 160) -> str:
    lowered = text.lower()
    best_term, best_at = None, -1
    for term in terms:
        at = lowered.find(term.lower())
        if at >= 0 and (best_at < 0 or at < best_at):
            best_term, best_at = term, at
    if best_at < 0:
        return " ".join(text[:width].split())
    start = max(0, best_at - width // 2)
    end = min(len(text), best_at + width // 2)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + " ".join(text[start:end].split()) + suffix
