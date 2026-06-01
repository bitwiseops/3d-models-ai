#!/usr/bin/env python3
"""Stage 2+3 — Parse the extracted Fusion API CHM pages into structured records + markdown.

Single source of truth: chm_html/ (extracted by extract_chm.sh). No network access.

Defensive by design: parsing keys off STABLE TEXTUAL section labels rendered in the page
(`<h2 class="api">Description</h2>`, "Parent Object:", "read/write", "Introduced in version"…),
never off fragile CSS classes. Every page is parsed inside try/except — one bad page records a
`parse_error` and the batch continues.

Outputs:
  docs.jsonl                       one JSON record per page
  md/<Object>/<member>.md          readable markdown, grep-friendly

Usage:
  python parse_html.py                 # full batch (idempotent: skips if docs.jsonl complete)
  python parse_html.py --force         # re-parse everything
  python parse_html.py --inventory     # Stage-2 classification table only, no parsing
  python parse_html.py --dry-run       # parse 5 of each kind, print JSON, write nothing
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
CHM = os.path.join(HERE, "chm_html")
DOCS_JSONL = os.path.join(HERE, "docs.jsonl")
MD_DIR = os.path.join(HERE, "md")

# ---------------------------------------------------------------------------
# Filename classification (Stage 2)
# ---------------------------------------------------------------------------

def classify(filename: str) -> str:
    stem = filename[:-4] if filename.lower().endswith(".htm") else filename
    if stem.endswith("_Sample"):
        return "sample"
    if stem.endswith("_UM"):
        return "concept"
    if "_" in stem:
        return "member"
    return "object_or_enum"


def split_name(filename: str):
    """Return (object_folder, member_slug, doc_id)."""
    stem = filename[:-4]
    if stem.endswith("_Sample"):
        return ("_Samples", stem, stem)
    if stem.endswith("_UM"):
        return ("_Concepts", stem, stem)
    if "_" in stem:
        obj, member = stem.split("_", 1)
        return (obj, member, stem)
    return (stem, "", stem)


# ---------------------------------------------------------------------------
# Small HTML helpers
# ---------------------------------------------------------------------------

def clean_text(s: str) -> str:
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def pre_text(pre) -> str:
    """Extract a <pre> code block, turning <br> into newlines."""
    for br in pre.find_all("br"):
        br.replace_with("\n")
    return clean_text(pre.get_text())


def parse_table(table):
    """Generic api-list table -> list of dicts keyed by the header-row column names."""
    rows = table.find_all("tr", recursive=False) or table.find_all("tr")
    if not rows:
        return []
    headers = [clean_text(td.get_text(" ")) for td in rows[0].find_all("td")]
    if not headers:
        headers = [clean_text(td.get_text(" ")) for td in rows[0].find_all(["th", "td"])]
    out = []
    for tr in rows[1:]:
        cells = [clean_text(td.get_text(" ")) for td in tr.find_all("td")]
        if not cells:
            continue
        out.append({headers[i] if i < len(headers) else f"col{i}": cells[i]
                    for i in range(len(cells))})
    return out


def section_nodes(body):
    """Split body into ordered sections keyed by <h2> label. Non-mutating: groups node
    references, never detaches them from the tree (so later soup.find() still works).

    Returns (meta_text, {label: [nodes]}). meta_text is everything between the <h1> and the
    first <h2> (holds 'Parent Object', 'Derived from', namespace, header file).
    """
    sections = defaultdict(list)
    meta_parts = []
    current = None  # None => still in the pre-h2 meta block (after h1)
    seen_h1 = False

    for node in list(body.children):
        name = getattr(node, "name", None)
        if name == "h1":
            seen_h1 = True
            continue
        if name == "h2":
            current = clean_text(node.get_text(" "))
            sections.setdefault(current, [])
            continue
        if current is None:
            if seen_h1:
                txt = node.get_text(" ") if hasattr(node, "get_text") else str(node)
                meta_parts.append(txt)
        else:
            sections[current].append(node)
    return clean_text(" ".join(meta_parts)), sections


def sec_text(nodes) -> str:
    return clean_text(" ".join(n.get_text(" ") for n in nodes if hasattr(n, "get_text")))


def sec_table(nodes):
    for n in nodes:
        if getattr(n, "name", None) == "table":
            return n
        if hasattr(n, "find_all"):
            t = n.find("table")
            if t:
                return t
    return None


def sec_paras(nodes):
    out = []
    for n in nodes:
        if getattr(n, "name", None) == "p":
            out.append(n)
        elif hasattr(n, "find_all"):
            out.extend(n.find_all("p"))
    return out


# ---------------------------------------------------------------------------
# Per-page parse (Stage 3)
# ---------------------------------------------------------------------------

KIND_FROM_SUFFIX = {
    "Method": "Method", "Property": "Property", "Event": "Event",
    "Object": "Object", "Enumerator": "Enum", "Sample": "Sample",
}


def parse_page(path: str) -> dict:
    filename = os.path.basename(path)
    html = open(path, encoding="utf-8", errors="replace").read()
    soup = BeautifulSoup(html, "lxml")

    rec = {"doc_id": split_name(filename)[2], "source_htm": filename}

    title_tag = soup.find("title")
    title_full = clean_text(title_tag.get_text(" ")) if title_tag else ""
    h1 = soup.find("h1")
    if h1:
        title_full = clean_text(h1.get_text(" ")) or title_full
    rec["title_full"] = title_full

    last_word = title_full.split()[-1] if title_full else ""
    page_kind = KIND_FROM_SUFFIX.get(last_word)
    file_class = classify(filename)
    if page_kind is None:
        page_kind = {"sample": "Sample", "concept": "Concept",
                     "member": "Member", "object_or_enum": "Object"}[file_class]
    rec["page_kind"] = page_kind

    obj_folder, member_slug, _ = split_name(filename)
    body = soup.body or soup
    meta_text, sections = section_nodes(body)

    # --- meta block: parent object / derived-from / namespace / header file ---
    m = re.search(r"Parent Object:\s*([A-Za-z0-9_]+)", meta_text)
    parent_object = m.group(1) if m else ""
    if not parent_object and file_class == "member":
        parent_object = obj_folder
    rec["parent_object"] = parent_object

    m = re.search(r"Derived from:\s*([A-Za-z0-9_]+)", meta_text)
    rec["derived_from"] = m.group(1) if m else ""

    # namespace marker can sit in the meta block OR inside the Description (enums)
    ns_search = meta_text
    if sections.get("Description"):
        ns_search += " " + sec_text(sections["Description"])
    m = re.search(r'namespace\s+"([^"]+)"', ns_search)
    rec["api_namespace"] = m.group(1) if m else ""
    m = re.search(r"header file is\s*<([^>]+)>", ns_search) or \
        re.search(r"header file is\s*&lt;([^&]+)&gt;", ns_search)
    rec["header_file"] = m.group(1).strip() if m else ""

    rec["object"] = parent_object or obj_folder
    rec["member"] = member_slug

    # --- Description ---
    desc = ""
    if sections.get("Description"):
        paras = sec_paras(sections["Description"])
        chosen = [clean_text(p.get_text(" ")) for p in paras
                  if not clean_text(p.get_text(" ")).startswith("Defined in namespace")]
        desc = "\n\n".join(t for t in chosen if t)
        if not desc:
            desc = re.sub(r"\s*Defined in namespace.*$", "",
                          sec_text(sections["Description"])).strip()
    rec["description"] = desc

    # --- Syntax (Python / C++) — query by stable tab ids ---
    def syntax_for(div_id):
        div = soup.find("div", id=div_id)
        if not div:
            return ""
        pre = div.find("pre")
        return pre_text(pre) if pre else ""
    rec["syntax_python"] = syntax_for("Python")
    rec["syntax_cpp"] = syntax_for("C++")

    # --- Parameters ---
    params = []
    t = sec_table(sections.get("Parameters", []))
    for row in (parse_table(t) if t else []):
        params.append({
            "name": row.get("Name", ""),
            "type": row.get("Type", ""),
            "description": row.get("Description", ""),
        })
    rec["parameters"] = params

    # --- Return Value ---
    returns = ""
    t = sec_table(sections.get("Return Value", []))
    rows = parse_table(t) if t else []
    if rows:
        r0 = rows[0]
        returns = clean_text(f"{r0.get('Type','')} — {r0.get('Description','')}".strip(" —"))
    rec["returns"] = returns

    # --- Property Value: access (read-only / read-write) + value type ---
    access = ""
    pv = sections.get("Property Value")
    pv_text = sec_text(pv) if pv else meta_text
    if re.search(r"read[\s/\-]*only", pv_text, re.I):
        access = "read-only"
    elif re.search(r"read[\s/\-]*write", pv_text, re.I):
        access = "read-write"
    rec["access"] = access
    if pv and not returns:
        rec["returns"] = pv_text

    # --- Enum values ---
    values = []
    t = sec_table(sections.get("Values", []))
    for row in (parse_table(t) if t else []):
        values.append({
            "name": row.get("Name", ""),
            "value": row.get("Value", ""),
            "description": row.get("Description", ""),
        })
    rec["values"] = values

    # --- Version ---
    version = ""
    ver = sections.get("Version")
    vtext = sec_text(ver) if ver else meta_text
    m = re.search(r"Introduced in version\s+([^\n©<]+)", vtext)
    if m:
        version = m.group(1).strip()
    rec["version_added"] = version

    # --- denormalized search field ---
    parts = [title_full, desc, rec["syntax_python"]]
    parts += [f"{p['name']} {p['type']} {p['description']}" for p in params]
    parts += [f"{v['name']} {v['description']}" for v in values]
    if rec["returns"]:
        parts.append(rec["returns"])
    rec["search_doc"] = clean_text("\n".join(p for p in parts if p))

    rec["md_path"] = os.path.join("md", obj_folder, (member_slug or obj_folder) + ".md")
    return rec


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def render_md(rec: dict) -> str:
    L = [f"# {rec['title_full']}", ""]
    meta = [rec["page_kind"]]
    if rec["parent_object"]:
        meta.append(f"Parent: `{rec['parent_object']}`")
    if rec["derived_from"]:
        meta.append(f"Derived from: `{rec['derived_from']}`")
    if rec["api_namespace"]:
        meta.append(f"namespace `{rec['api_namespace']}`")
    if rec["access"]:
        meta.append(rec["access"])
    if rec["version_added"]:
        meta.append(f"since {rec['version_added']}")
    L += ["**" + " · ".join(meta) + "**", ""]
    if rec["header_file"]:
        L += [f"Header: `{rec['header_file']}`", ""]

    if rec["description"]:
        L += ["## Description", "", rec["description"], ""]

    if rec["syntax_python"]:
        L += ["## Syntax (Python)", "", "```python", rec["syntax_python"], "```", ""]
    if rec["syntax_cpp"]:
        L += ["## Syntax (C++)", "", "```cpp", rec["syntax_cpp"], "```", ""]

    if rec["parameters"]:
        L += ["## Parameters", "", "| Name | Type | Description |", "| --- | --- | --- |"]
        for p in rec["parameters"]:
            d = p["description"].replace("\n", " ").replace("|", "\\|")
            L.append(f"| `{p['name']}` | {p['type']} | {d} |")
        L.append("")

    if rec["returns"]:
        L += ["## Return Value", "", rec["returns"], ""]

    if rec["values"]:
        L += ["## Values", "", "| Name | Value | Description |", "| --- | --- | --- |"]
        for v in rec["values"]:
            d = v["description"].replace("\n", " ").replace("|", "\\|")
            L.append(f"| `{v['name']}` | {v['value']} | {d} |")
        L.append("")

    L += ["---", f"Source: `chm_html/{rec['source_htm']}`"]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------

def list_pages():
    return sorted(f for f in os.listdir(CHM) if f.lower().endswith(".htm"))


def inventory():
    files = list_pages()
    by_class = Counter(classify(f) for f in files)
    print(f"Total htm pages: {len(files)}")
    print("\nBy file class:")
    for k, v in by_class.most_common():
        print(f"  {k:18s} {v:6d}")
    # namespace prefix breakdown via parsed namespace is expensive; approximate via parse of
    # a sample is unnecessary — report file-class table (per plan) and a quick prefix guess.
    print("\nNote: no manifest.jsonl in this project -> corpus = all htm pages.")
    objects = by_class.get("object_or_enum", 0)
    members = by_class.get("member", 0)
    print(f"\nCHECK (Stage 2): object/enum={objects} (>=1600?), member={members} (>=15000?)")
    ok = objects >= 1600 and members >= 15000
    print("PASS (Stage 2)" if ok else "WARN (Stage 2): counts differ from plan thresholds")
    return files


def dry_run():
    files = list_pages()
    buckets = defaultdict(list)
    for f in files:
        buckets[classify(f)].append(f)
    for cls, flist in buckets.items():
        print(f"\n================= DRY-RUN: {cls} =================")
        for f in flist[:5]:
            try:
                rec = parse_page(os.path.join(CHM, f))
                slim = {k: rec[k] for k in ("doc_id", "title_full", "page_kind",
                        "parent_object", "api_namespace", "header_file", "access",
                        "version_added", "parameters", "returns", "values")}
                slim["syntax_python"] = rec["syntax_python"][:120]
                slim["description"] = rec["description"][:160]
                print(json.dumps(slim, ensure_ascii=False, indent=2))
            except Exception as e:  # noqa: BLE001
                print(f"  PARSE ERROR {f}: {e}")


def full_batch(force: bool):
    files = list_pages()
    if not force and os.path.exists(DOCS_JSONL):
        n = sum(1 for _ in open(DOCS_JSONL))
        if n >= len(files):
            print(f"docs.jsonl already complete ({n} records >= {len(files)} pages) — skip. "
                  f"Use --force to re-parse.")
            return
    errors = []
    written = 0
    with open(DOCS_JSONL, "w", encoding="utf-8") as out:
        for i, f in enumerate(files, 1):
            try:
                rec = parse_page(os.path.join(CHM, f))
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                md_abs = os.path.join(HERE, rec["md_path"])
                os.makedirs(os.path.dirname(md_abs), exist_ok=True)
                with open(md_abs, "w", encoding="utf-8") as mf:
                    mf.write(render_md(rec))
                written += 1
            except Exception as e:  # noqa: BLE001 — one bad page must not abort the batch
                err = {"doc_id": f[:-4], "source_htm": f, "parse_error": repr(e)}
                out.write(json.dumps(err, ensure_ascii=False) + "\n")
                errors.append(err)
            if i % 2000 == 0:
                print(f"  ...{i}/{len(files)}")
    total = written + len(errors)
    rate = len(errors) / total * 100 if total else 0
    print(f"\nParsed OK: {written}   parse_error: {len(errors)}   "
          f"error rate: {rate:.2f}%   (threshold < 2%)")
    for e in errors[:3]:
        print("  example:", e["source_htm"], e["parse_error"])
    print("PASS (Stage 3)" if rate < 2 else "FAIL (Stage 3): error rate >= 2%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if not os.path.isdir(CHM):
        sys.exit("chm_html/ missing — run extract_chm.sh first.")
    if args.inventory:
        inventory()
    elif args.dry_run:
        dry_run()
    else:
        inventory()
        full_batch(args.force)


if __name__ == "__main__":
    main()
