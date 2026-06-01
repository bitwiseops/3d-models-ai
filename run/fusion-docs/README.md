# Fusion API docs — offline, browsable, lexically searchable

Turns the Autodesk Fusion API reference (shipped as a single offline `FusionAPI.chm`) into a
local knowledge base that is **readable as markdown** and **searchable with SQLite FTS5
(BM25)**. The CHM is the single source of truth — **no network access** anywhere in the
pipeline. Search is **lexical only** (no embeddings / no semantic): API docs are dominated by
exact identifiers, and BM25 ranks those well. The structured `docs.jsonl` keeps a semantic
layer one step away if ever wanted (see *Future* below).

## Pipeline

```
FusionAPI.chm
   │  extract_chm.sh   (7z / extract_chmLib; flatten *.htm)
   ▼
chm_html/*.htm  ──parse_html.py──►  docs.jsonl        (structured records, 1/page)
                                    md/<Object>/<member>.md   (readable, grep-friendly)
                       │
                       │  build_index.py
                       ▼
                    docs.db   (FTS5 virtual table, BM25)
                       │
                       │  search.py "<query>"
                       ▼
                  ranked results (Object.member [Kind] + snippet + md path + source)
```

## Setup

```bash
brew install p7zip          # CHM extractor (provides 7z); fallback: brew install chmlib
uv add beautifulsoup4       # lxml already in the project; sqlite3 is stdlib
```

## Commands

```bash
# 1. extract (idempotent; skips if chm_html/ already has >10k htm).
#    Override the source path with CHM_PATH=... if not ~/Desktop/FusionAPI.chm
bash run/fusion-docs/extract_chm.sh

# 2. parse -> docs.jsonl + md/   (idempotent; --force to re-parse, --dry-run / --inventory to inspect)
uv run python run/fusion-docs/parse_html.py

# 3. build the FTS5 index -> docs.db   (idempotent; drops + recreates)
uv run python run/fusion-docs/build_index.py

# 4. search (lexical BM25). -k N for result count.
uv run python run/fusion-docs/search.py "addSimple"
uv run python run/fusion-docs/search.py "create an extrusion through all" -k 8
uv run python run/fusion-docs/search.py "Component.occurrences"
```

You can also just `rg` the markdown directly: `rg -i "addSimple" run/fusion-docs/md/`.

## Corpus stats (last build)

| item | value |
| --- | --- |
| htm pages extracted | 17,871 |
| records parsed | 17,871 (parse errors: 0) |
| object/enum pages | 1,638 |
| member pages (Method/Property/Event) | 15,963 |
| code samples / concept pages | 226 / 44 |
| FTS5 rows | 17,871 |
| markdown size | ~71 MB |
| index (docs.db) | ~28 MB |

Namespaces: `adsk::fusion` 10,675 · `adsk::core` 3,422 · `adsk::cam` 2,126 ·
`adsk::electron` 1,091 · `adsk::volume` 217 · `adsk::drawing` 63.

## Uso da `fusion-build`

La skill `fusion-build` (`run/.claude/skills/fusion-build/`) usa questa doc come **lookup
dell'API Fusion** per scrivere il Python `adsk.*` giusto (render `saveAsImageFile`, `boundingBox`,
`isSolid`, `documents open`, joints) senza indovinare, e per capire l'API `adsk` dietro ai tool
MCP granulari (le cui firme non sono documentate). Gli executor/giudice lanciano, dalla cwd RUN:

```bash
uv run python fusion-docs/search.py "<keyword adsk>" -k 5   # poi Read del path md assoluto stampato
```

Cercano per **identificatore** (`saveAsImageFile`, `Joints add`), non per il nome snake_case del
tool MCP. `search.py` stampa path **assoluti** così i subagent leggono il md da qualunque cwd.
Precondizione: la doc va costruita una volta (i comandi sopra); `docs.db`/`md/` sono gitignored.

## How search works

- `parse_html.py` is **defensive**: it keys off stable textual section labels rendered in the
  page (`<h2>Description</h2>`, `Parent Object:`, `read/write`, `Introduced in version`…),
  never off CSS classes. Each page parses inside `try/except`; one bad page records a
  `parse_error` and the batch continues.
- `search.py` tokenizes the query on non-identifier characters (so `Component.occurrences`
  becomes `Component`, `occurrences`), drops English stopwords, quotes each token (so dots and
  operators can't break FTS5 `MATCH`), and ORs them. BM25 (IDF-weighted) then ranks: OR gives
  recall, IDF gives precision. Column weights boost `title`/`member`/`signature`; a small
  re-rank keeps reference pages above `Sample`/`Concept` pages on ties.

## What is committed

Only the scripts + this README + `.gitignore`. The generated artifacts (`chm_html/`, `md/`,
`docs.jsonl`, `docs.db`) and the `.chm` itself are **gitignored** — all are regenerable from
the CHM in a few minutes via the commands above.

## Future: adding semantic / hybrid search

`docs.jsonl` already carries a denormalized `search_doc` field per record. To add semantic
search later without re-parsing:
1. Embed each `search_doc` (e.g. `BAAI/bge-small-en-v1.5`, `normalize_embeddings=True`).
2. Store vectors (`embeddings.npy` + `ids.json`, or a pgvector / sqlite-vec table).
3. Fuse the dense rank list with this BM25 list via Reciprocal Rank Fusion (k≈60) for a hybrid
   ranker. The lexical half (this repo) is the part that nails exact identifiers; the dense
   half adds conceptual recall. The same `docs.jsonl` can feed an MCP server exposing
   `search`/`get_page` tools to the `fusion-build` skill.
