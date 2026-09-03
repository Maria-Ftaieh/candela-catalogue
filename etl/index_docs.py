#!/usr/bin/env python3
"""Indexes the local PDF documents.

Extracts the text of every PDF page by page and writes it to the FTS5 index, so
the documents become searchable by content. Files whose sha1 has not changed are
skipped, which keeps the monthly run fast.

Usage:
    python3 etl/index_docs.py
    python3 etl/index_docs.py --force     # re-read everything
"""
import argparse
import hashlib
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from brands import discover_brands  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "data", "catalogue.db")

# The directory name becomes the category. This map only normalises a few common
# spellings; with brands/<code>/documents/<Category>/ it is rarely needed.
CATEGORIES = {
    "cataloges": "Catalogue", "catalogues": "Catalogue", "catalogs": "Catalogue",
    "brouchers": "Brochure", "brochures": "Brochure",
    "certificates": "Certificate",
    "code of conducts": "Code of Conduct",
    "declaration of conformity": "Declaration of Conformity",
    "manuals": "Manual", "datasheets": "Data Sheet",
}


def sha1_of(path, chunk=1 << 20):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def nice_title(filename, meta_title):
    """Use the PDF metadata title when it looks usable, otherwise the file name."""
    if meta_title:
        t = meta_title.strip()
        # Some PDFs put a file path or producer junk in the title field.
        if 3 < len(t) < 120 and not t.lower().endswith((".pdf", ".indd", ".qxd")):
            return t
    t = os.path.splitext(filename)[0]
    t = re.sub(r"[_]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def extract(path):
    """Return (page_texts, title).

    Pages are returned separately: the full text is joined from them and they are
    also stored in `doc_page` for catalogue<->product linking, so the PDF is only
    ever opened once.
    """
    from pypdf import PdfReader

    reader = PdfReader(path)
    meta_title = None
    try:
        if reader.metadata and reader.metadata.title:
            meta_title = str(reader.metadata.title)
    except Exception:
        pass

    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            parts.append("")
    return parts, meta_title


def join_pages(parts):
    text = re.sub(r"[ \t]+", " ", "\n".join(parts))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def find_pdfs(root):
    """(relative_path, category, brand_code) triples.

    Reads both the brand and the category from the
    brands/<code>/documents/<Category>/ layout; the directory name is the category.
    """
    out = []
    for b in discover_brands():
        if not os.path.isdir(b.doc_dir):
            continue
        for dirpath, dirs, files in os.walk(b.doc_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in sorted(files):
                if not f.lower().endswith(".pdf"):
                    continue
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, root)
                inner = os.path.relpath(full, b.doc_dir)
                top = inner.split(os.sep)[0] if os.sep in inner else "Other"
                out.append((rel, CATEGORIES.get(top.lower(), top), b.code))
    return sorted(out)


def main():
    ap = argparse.ArgumentParser(description="Index the PDF documents")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--root", default=ROOT, help="root to search for PDFs")
    ap.add_argument("--force", action="store_true", help="re-read unchanged files too")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"Database missing: {args.db}\nRun etl/build_db.py first.")

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA synchronous=OFF")
    # build_db.py creates these; recreate them if an older database is in use.
    conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(
                      title, body,
                      tokenize="unicode61 remove_diacritics 2", prefix='2 3 4')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS doc_page (
                      doc_id INTEGER NOT NULL, page_no INTEGER NOT NULL, text TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_page ON doc_page(doc_id, page_no)")

    known = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT path, sha1, id FROM doc")}
    pdfs = find_pdfs(args.root)
    print(f"{len(pdfs)} PDFs found.\n")

    t0 = time.time()
    added = updated = skipped = 0
    empty = []

    for rel, category, brand in pdfs:
        full = os.path.join(args.root, rel)
        digest = sha1_of(full)
        prev = known.get(rel)

        if prev and prev[0] == digest and not args.force:
            # Same content; no need to re-extract, just refresh the labels.
            conn.execute("UPDATE doc SET category=?, brand=? WHERE id=?",
                         (category, brand, prev[1]))
            skipped += 1
            print(f"  = {rel}  (unchanged)")
            continue

        try:
            parts, meta_title = extract(full)
        except Exception as exc:
            print(f"  ! {rel}: unreadable ({exc})", file=sys.stderr)
            parts, meta_title = [], None
        text, pages = join_pages(parts), (len(parts) or None)

        title = nice_title(os.path.basename(rel), meta_title)
        if len(text) < 200:
            empty.append(rel)

        row = (brand, rel, category, os.path.basename(rel), title,
               os.path.getsize(full), pages, digest,
               datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"))

        if prev:
            doc_id = prev[1]
            conn.execute("""UPDATE doc SET brand=?, category=?, filename=?, title=?,
                            bytes=?, pages=?, sha1=?, indexed_at=? WHERE id=?""",
                         (row[0],) + row[2:] + (doc_id,))
            conn.execute("DELETE FROM doc_fts WHERE rowid=?", (doc_id,))
            updated += 1
            mark = "~"
        else:
            cur = conn.execute("""INSERT INTO doc
                     (brand, path, category, filename, title, bytes, pages, sha1,
                      indexed_at) VALUES (?,?,?,?,?,?,?,?,?)""", row)
            doc_id = cur.lastrowid
            added += 1
            mark = "+"

        conn.execute("INSERT INTO doc_fts (rowid, title, body) VALUES (?,?,?)",
                     (doc_id, title, text))
        conn.execute("DELETE FROM doc_page WHERE doc_id = ?", (doc_id,))
        conn.executemany(
            "INSERT INTO doc_page (doc_id, page_no, text) VALUES (?,?,?)",
            [(doc_id, i, t) for i, t in enumerate(parts, 1) if t and t.strip()])
        print(f"  {mark} {rel}  [{brand}/{category}] {pages or '?'} pages, "
              f"{len(text):,} chars")

    conn.commit()
    conn.execute("INSERT OR REPLACE INTO catalog_info (brand,key,value) VALUES ('',?,?)",
                 ("DOCS_INDEXED_AT",
                  datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")))
    conn.commit()
    total = conn.execute("SELECT count(*) FROM doc").fetchone()[0]
    conn.execute("INSERT INTO doc_fts(doc_fts) VALUES('optimize')")
    conn.commit()
    conn.close()

    print(f"\nDone ({time.time()-t0:.0f} s): {added} added, {updated} updated, "
          f"{skipped} skipped — {total} documents in total.")
    if empty:
        print("\nWarning — no text could be extracted from these PDFs (probably "
              "scanned images); only their titles are searchable:")
        for e in empty:
            print(f"  - {e}")


if __name__ == "__main__":
    main()
