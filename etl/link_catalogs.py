#!/usr/bin/env python3
"""Links catalogue/brochure PDFs to product series.

Manufacturer catalogues do not print article numbers; they talk about product
*series* (Sonnos, Tugra, E-Line Pro...). This script finds which series appear on
which catalogue page and fills the `doc_series` table, so a product page can say
"this product appears in that catalogue, page 12".

It never opens a PDF: it reads the `doc_page` table that etl/index_docs.py filled,
so it finishes in seconds.

Usage:
    python3 etl/link_catalogs.py
    python3 etl/link_catalogs.py --report    # list the matches per document
"""
import argparse
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "data", "catalogue.db")

# Only these categories are linked to series. Certificates, codes of conduct and
# declarations are company-wide documents and belong to no single series.
CATALOGUE_CATEGORIES = ("Catalogue", "Brochure")

# Series names that are also ordinary English words. They are excluded, otherwise
# they match on every page and produce noise. Add a name here if it misbehaves.
GENERIC_WORDS = {
    "rail", "next", "fit", "pro", "plus", "city", "light", "base",
    "line", "solo", "trio", "duo", "star", "point", "flat", "one",
}

# Thresholds
MIN_LENGTH = 4       # shorter series names are dropped ("74R")
MIN_PAGES = 2        # if not in the file name, it must appear on at least this many pages
PRIMARY_PAGES = 8    # appearing on this many pages means the document is about it
FILENAME_SCORE = 50


def norm(s):
    """'765... E-Line' -> 'E-Line'. Series names can carry a numeric code prefix."""
    s = re.sub(r"^\d*\.\.\.\s*", "", s)
    return re.sub(r"\s+", " ", s).strip()


def split_parts(name):
    """Split a series name; also breaks camelCase and letter/digit boundaries.

    'PolaronIQ W' -> ['Polaron', 'IQ', 'W']   (catalogues write 'Polaron IQ')
    'Yonos D/H'   -> ['Yonos', 'D', 'H']
    """
    out = []
    for p in re.findall(r"[A-Za-zÀ-ÿ]+|\d+", name):
        out += re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+", p) or [p]
    return out


# A trailing variant suffix: "40", "600", "70N", "D", "H"
VARIANT = re.compile(r"^(\d+[A-Za-z]?|[A-Za-z])$")


def family(name):
    """Strip variant suffixes to get the family name: 'Yonos D/H' -> 'Yonos'.

    Catalogues usually use the family name ('Yonos') while the database holds the
    variant ('Yonos D/H', 'Yonos BE H'). The family name lets the two meet.
    """
    t = split_parts(name)
    while len(t) > 1 and VARIANT.match(t[-1]):
        t.pop()
    return " ".join(t)


def numeric_only(s):
    return bool(re.fullmatch(r"[\d\s/.-]+", s))


def build_pattern(name):
    r"""Word-bounded regex that tolerates separators.

    Parts are joined with `[\s\-/.]*`, so 'E-Line Pro', 'E Line Pro' and
    'E-LinePro' are all caught by the same pattern.
    """
    body = r"[\s\-/.]*".join(re.escape(t) for t in split_parts(name))
    return re.compile(r"(?<![\w-])" + body + r"(?![\w-])", re.IGNORECASE)


def page_summary(pages):
    """[1,2,3,7,9,10] -> '1-3, 7, 9-10'"""
    if not pages:
        return ""
    pl = sorted(set(pages))
    groups, start, end = [], pl[0], pl[0]
    for x in pl[1:]:
        if x == end + 1:
            end = x
        else:
            groups.append((start, end))
            start = end = x
    groups.append((start, end))
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in groups)


def prepare_candidates(conn, brand=None):
    """(patterns, candidate -> raw series names).

    Two layers of candidates are produced:
      1. Full series name  ('Lumena Plus 80') -> that series only
      2. Family name       ('Lumena Plus')    -> every series in the family
    Layer 2 matters because catalogues very often use the family name.

    Patterns are sorted longest first so that the 'E-Line' inside 'E-Line Pro' is
    not counted a second time — the longest match wins.
    """
    # Candidates are de-duplicated by their part list: 'E-Line' and the family name
    # 'E Line' build the same pattern and must be one candidate, otherwise they
    # split the matches between them and both look incomplete.
    candidates = {}

    def add(name, series):
        parts = tuple(split_parts(name))
        if not parts:
            return
        rec = candidates.setdefault(parts, {"name": name, "series": set()})
        rec["series"].update(series)
        if "-" in name and "-" not in rec["name"]:
            rec["name"] = name        # prefer the 'E-Line' spelling over 'E Line'

    query = "SELECT DISTINCT series_en FROM product WHERE series_en IS NOT NULL"
    params = ()
    if brand:
        query += " AND brand = ?"
        params = (brand,)

    families = {}
    for (series,) in conn.execute(query, params):
        name = norm(series)
        if not name:
            continue
        add(name, {series})
        families.setdefault(family(name), set()).add(series)
    for f, series in families.items():
        add(f, series)

    chosen = {}
    for parts, rec in candidates.items():
        name = rec["name"]
        if (numeric_only(name) or len(name) < MIN_LENGTH
                or name.lower() in GENERIC_WORDS):
            continue
        chosen[name] = rec["series"]
    ordered = sorted(chosen,
                     key=lambda n: (len("".join(split_parts(n))), len(split_parts(n))),
                     reverse=True)
    return [(n, build_pattern(n)) for n in ordered], chosen


def scan(text, patterns):
    """Series appearing in the text (non-overlapping, longest match wins)."""
    if not text:
        return set()
    found, taken = set(), []
    for name, pattern in patterns:
        for m in pattern.finditer(text):
            a, b = m.span()
            if any(a < y and x < b for x, y in taken):
                continue    # it sits inside a longer series name
            taken.append((a, b))
            found.add(name)
            break           # once per page is enough
    return found


def run(db_path, report=False):
    t0 = time.time()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS doc_series (
        doc_id INTEGER NOT NULL, brand TEXT NOT NULL DEFAULT '', series TEXT NOT NULL,
        page_count INTEGER, first_page INTEGER, pages TEXT, in_filename INTEGER,
        strength TEXT, score REAL);
      DELETE FROM doc_series;
    """)

    if not conn.execute("SELECT count(*) FROM doc_page").fetchone()[0]:
        sys.exit("doc_page is empty. Run etl/index_docs.py --force first.")

    # Separate pattern set per brand: one brand's catalogue must not match another
    # brand's series (both could have a series called "Basic").
    brands = [r[0] for r in conn.execute("SELECT code FROM brand ORDER BY code")]
    if not brands:
        brands = [r[0] for r in conn.execute(
            "SELECT DISTINCT brand FROM doc WHERE brand <> ''")]
    cache = {b: prepare_candidates(conn, b) for b in brands}
    print("Patterns per brand: " + ", ".join(
        f"{b}={len(cache[b][0])}" for b in brands) + "\n")

    documents = conn.execute(
        "SELECT id, filename, title, category, brand FROM doc "
        "WHERE category IN (%s) ORDER BY brand, id"
        % ",".join("?" * len(CATALOGUE_CATEGORIES)), CATALOGUE_CATEGORIES).fetchall()

    rows = []
    unmatched = []
    for d in documents:
        patterns, raw = cache.get(d["brand"], ([], {}))
        if not patterns:
            continue

        # The file name is a strong signal: '22_57-GB-int_TUGRA.pdf' -> Tugra
        name_text = re.sub(r"[_\-.]+", " ", os.path.splitext(d["filename"])[0])
        in_name = scan(name_text, patterns)

        on_page = {}
        for p in conn.execute(
                "SELECT page_no, text FROM doc_page WHERE doc_id=? ORDER BY page_no",
                (d["id"],)):
            for name in scan(p["text"], patterns):
                on_page.setdefault(name, []).append(p["page_no"])

        kept = 0
        for name in set(on_page) | in_name:
            pages = on_page.get(name, [])
            named = 1 if name in in_name else 0
            if not named and len(pages) < MIN_PAGES:
                continue    # a single passing mention, usually a comparison table
            score = len(pages) + FILENAME_SCORE * named
            strength = ("primary" if named or len(pages) >= PRIMARY_PAGES
                        else "mentioned")
            for original in sorted(raw[name]):   # one candidate can cover several
                rows.append((d["id"], d["brand"], original, len(pages),
                             min(pages) if pages else None,
                             page_summary(pages), named, strength, score))
            kept += 1

        if report or not kept:
            label = d["filename"][:52]
            if kept:
                top = sorted(((len(on_page.get(n, [])), n)
                              for n in set(on_page) | in_name), reverse=True)[:6]
                print(f"  {label:<54} {kept:>3} series  "
                      + ", ".join(f"{n}({c}p)" for c, n in top))
            else:
                print(f"  {label:<54}   — no series found")
        if not kept:
            unmatched.append(d["filename"])

    conn.executemany("""INSERT INTO doc_series
        (doc_id, brand, series, page_count, first_page, pages, in_filename, strength,
         score) VALUES (?,?,?,?,?,?,?,?,?)""", rows)
    conn.executescript("""
      CREATE INDEX IF NOT EXISTS idx_docser_series ON doc_series(brand, series);
      CREATE INDEX IF NOT EXISTS idx_docser_doc    ON doc_series(doc_id);
    """)
    conn.execute("INSERT OR REPLACE INTO catalog_info (brand,key,value) VALUES ('',?,?)",
                 ("CATALOGS_LINKED_AT",
                  datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")))
    conn.commit()

    covered = conn.execute("""
        SELECT count(*) FROM product p
        WHERE EXISTS (SELECT 1 FROM doc_series ds
                      WHERE ds.series = p.series_en AND ds.brand = p.brand)
    """).fetchone()[0]
    total = conn.execute("SELECT count(*) FROM product").fetchone()[0]
    series_count = conn.execute(
        "SELECT count(DISTINCT series) FROM doc_series").fetchone()[0]
    doc_count = conn.execute(
        "SELECT count(DISTINCT doc_id) FROM doc_series").fetchone()[0]

    print(f"\nDone ({time.time()-t0:.1f} s)")
    print(f"  link rows              : {len(rows):,}")
    print(f"  documents matched      : {doc_count}/{len(documents)}")
    print(f"  series matched         : {series_count}")
    print(f"  products with catalogue: {covered:,}/{total:,} "
          f"({100*covered/total:.0f}%)" if total else "")
    if unmatched:
        print(f"\n  {len(unmatched)} documents matched no series "
              f"(usually control-gear or general brochures):")
        for u in unmatched:
            print(f"    - {u}")
    conn.close()


def main():
    ap = argparse.ArgumentParser(description="Link catalogues to product series")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--report", action="store_true", help="print matches per document")
    args = ap.parse_args()
    if not os.path.exists(args.db):
        sys.exit(f"Database missing: {args.db}")
    run(args.db, args.report)


if __name__ == "__main__":
    main()
