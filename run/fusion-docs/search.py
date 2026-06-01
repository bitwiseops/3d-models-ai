#!/usr/bin/env python3
"""Stage 5 — Lexical search over the Fusion API docs (SQLite FTS5 / BM25).

Lexical only by design (no embeddings / no semantic / no hybrid). Identifiers are the
common case in API docs, and FTS5 BM25 ranks exact-token matches well.

Usage:
  python search.py "addSimple"
  python search.py "create an extrusion through all" -k 8
  python search.py "Component.occurrences"

Dotted identifiers (Component.occurrences) are split into quoted tokens so they don't break
FTS5 MATCH syntax; the title column is weighted highest via bm25() column weights.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "docs.db")

# bm25 column weights — order matches build_index.COLUMNS. Boost title/object/member/signature.
#            doc_id title object member kind ns sig desc params url md  search_doc
WEIGHTS = [   0.0,  8.0,  4.0,   6.0,   1.0, 1.0, 4.0, 2.0, 2.0, 0.0, 0.0,  3.0]


STOPWORDS = {"a", "an", "the", "of", "to", "in", "on", "for", "and", "or", "is", "are",
             "be", "with", "by", "that", "this", "from", "as", "at", "it", "its", "how",
             "i", "do", "can", "use", "using", "about"}


def build_match(query: str) -> str:
    """Turn a free-text query into a safe FTS5 MATCH expression.

    Split on non-identifier chars (so `Component.occurrences` -> Component, occurrences),
    drop English stopwords (they hurt natural-language recall), quote every token (defuses
    dots/operators), and OR them. bm25 then ranks docs matching more/rarer terms on top —
    OR gives recall, IDF-weighted bm25 gives precision. Falls back to a quoted whole-query
    phrase if tokenization yields nothing.
    """
    tokens = re.findall(r"[A-Za-z0-9_]+", query)
    if not tokens:
        return '"' + query.replace('"', '""') + '"'
    content = [t for t in tokens if t.lower() not in STOPWORDS] or tokens
    return " OR ".join(f'"{t}"' for t in content)


# Domain-aware re-rank: reference pages outrank sample/concept pages on ties. Added to the
# bm25 score (lower = better), so a positive penalty pushes that kind down. Still lexical.
KIND_PENALTY = {"Sample": 2.5, "Concept": 1.5}


def search(query: str, k: int):
    if not os.path.exists(DB):
        raise SystemExit("docs.db missing — run build_index.py first.")
    con = sqlite3.connect(DB)
    cur = con.cursor()
    match = build_match(query)
    weights = ", ".join(str(w) for w in WEIGHTS)
    sql = f"""
        SELECT object, member, kind, snippet(docs, 7, '[', ']', ' … ', 12) AS snip,
               md_path, url, bm25(docs, {weights}) AS score
        FROM docs WHERE docs MATCH ?
        ORDER BY score LIMIT ?
    """
    try:
        rows = cur.execute(sql, (match, max(k * 4, 40))).fetchall()
    except sqlite3.OperationalError as e:
        con.close()
        raise SystemExit(f"FTS query error for {match!r}: {e}")
    con.close()
    rows = sorted(rows, key=lambda r: r[6] + KIND_PENALTY.get(r[2], 0.0))
    return match, rows[:k]


def main():
    ap = argparse.ArgumentParser(description="Lexical (FTS5/BM25) search of Fusion API docs.")
    ap.add_argument("query")
    ap.add_argument("-k", type=int, default=8, help="number of results (default 8)")
    args = ap.parse_args()

    match, rows = search(args.query, args.k)
    print(f'query: {args.query!r}   (MATCH {match})\n')
    if not rows:
        print("no matches.")
        return
    for i, (obj, member, kind, snip, md_path, url, score) in enumerate(rows, 1):
        name = f"{obj}.{member}" if member else obj
        snip = " ".join(snip.split())
        # Absolute paths so callers (incl. skill subagents from any cwd) can Read directly.
        print(f"{i:2d}. {name}  [{kind}]   (bm25 {score:.2f})")
        if snip:
            print(f"    {snip}")
        print(f"    md:  {os.path.join(HERE, md_path)}")
        print(f"    src: {os.path.join(HERE, url)}")
        print()


if __name__ == "__main__":
    main()
