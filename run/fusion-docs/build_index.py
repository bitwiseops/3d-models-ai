#!/usr/bin/env python3
"""Stage 4 — Build a SQLite FTS5 lexical index from docs.jsonl.

Lexical only (BM25). No embeddings / no semantic search by design. The denormalized
`search_doc` field plus the structured columns are all present in docs.jsonl, so a future
semantic layer can be added without re-parsing the CHM.

Idempotent: drops and recreates the table on every run (cheap relative to parsing).
"""
from __future__ import annotations

import json
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS_JSONL = os.path.join(HERE, "docs.jsonl")
DB = os.path.join(HERE, "docs.db")

COLUMNS = ["doc_id", "title", "object", "member", "kind", "namespace",
           "signature", "description", "params", "url", "md_path", "search_doc"]


def row_from_record(r: dict) -> tuple:
    params = " ".join(f"{p.get('name','')} ({p.get('type','')}) {p.get('description','')}"
                      for p in r.get("parameters", []))
    if r.get("values"):
        params += " " + " ".join(f"{v.get('name','')}={v.get('value','')} {v.get('description','')}"
                                 for v in r["values"])
    return (
        r.get("doc_id", ""),
        r.get("title_full", ""),
        r.get("object", ""),
        r.get("member", ""),
        r.get("page_kind", ""),
        r.get("api_namespace", ""),
        r.get("syntax_python", ""),
        r.get("description", ""),
        params.strip(),
        f"chm_html/{r.get('source_htm','')}",
        r.get("md_path", ""),
        r.get("search_doc", ""),
    )


def main():
    if not os.path.exists(DOCS_JSONL):
        raise SystemExit("docs.jsonl missing — run parse_html.py first.")

    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("DROP TABLE IF EXISTS docs")
    cols = ", ".join(COLUMNS)
    cur.execute(f"CREATE VIRTUAL TABLE docs USING fts5({cols}, tokenize='unicode61')")

    n_jsonl = 0
    n_indexed = 0
    placeholders = ", ".join("?" * len(COLUMNS))
    batch = []
    for line in open(DOCS_JSONL, encoding="utf-8"):
        n_jsonl += 1
        r = json.loads(line)
        if r.get("parse_error"):
            continue  # skip pages that failed to parse (none expected)
        batch.append(row_from_record(r))
        if len(batch) >= 1000:
            cur.executemany(f"INSERT INTO docs ({cols}) VALUES ({placeholders})", batch)
            n_indexed += len(batch)
            batch = []
    if batch:
        cur.executemany(f"INSERT INTO docs ({cols}) VALUES ({placeholders})", batch)
        n_indexed += len(batch)
    con.commit()

    (fts_rows,) = cur.execute("SELECT count(*) FROM docs").fetchone()
    con.close()

    print(f"docs.jsonl lines: {n_jsonl}")
    print(f"indexed records:  {n_indexed}")
    print(f"FTS5 rows:        {fts_rows}")
    ok = fts_rows == n_indexed
    print("PASS (Stage 4)" if ok else "FAIL (Stage 4): FTS row count != indexed record count")
    print(f"DB: {DB}")


if __name__ == "__main__":
    main()
