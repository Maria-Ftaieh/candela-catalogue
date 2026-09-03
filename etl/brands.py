#!/usr/bin/env python3
"""Discovers the brand directories.

Layout (see brands/README.md):

    brands/<code>/brand.json        display name, colour (optional)
    brands/<code>/data/             BMEcat XML (optional)
    brands/<code>/documents/<Category>/*.pdf

Adding a brand needs no code change; creating the directory is enough.
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRAND_ROOT = os.path.join(ROOT, "brands")


class Brand:
    def __init__(self, code, directory):
        self.code = code
        self.directory = directory
        self.data_dir = os.path.join(directory, "data")
        self.doc_dir = os.path.join(directory, "documents")
        settings = {}
        path = os.path.join(directory, "brand.json")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    settings = json.load(fh)
            except Exception:
                settings = {}
        self.name = settings.get("name") or code.replace("-", " ").title()
        self.colour = settings.get("colour") or settings.get("color") or ""
        self.site = settings.get("site") or ""
        self.note = settings.get("note") or ""
        self.sort_order = int(settings.get("sort_order", 0) or 0)

    def find_xml(self):
        """The largest .xml under data/ — that is the full catalogue."""
        candidates = []
        for root, _dirs, files in os.walk(self.data_dir):
            for f in files:
                if f.lower().endswith(".xml"):
                    full = os.path.join(root, f)
                    candidates.append((os.path.getsize(full), full))
        return max(candidates)[1] if candidates else None

    def __repr__(self):
        return f"<Brand {self.code} ({self.name})>"


def discover_brands(root=BRAND_ROOT):
    """Brands in display order. Directories starting with '_' are templates."""
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        directory = os.path.join(root, name)
        if os.path.isdir(directory) and not name.startswith((".", "_")):
            out.append(Brand(name.lower(), directory))
    out.sort(key=lambda b: (b.sort_order, b.name.lower()))
    return out


def save_brands(conn, brands):
    conn.execute("DELETE FROM brand")
    conn.executemany(
        "INSERT INTO brand (code, name, colour, site, note, sort_order) "
        "VALUES (?,?,?,?,?,?)",
        [(b.code, b.name, b.colour, b.site, b.note, b.sort_order) for b in brands])
    conn.commit()
