#!/usr/bin/env python3
"""BMEcat XML -> SQLite.

Reads the (potentially multi-gigabyte) BMEcat file as a stream with iterparse,
writes it into normalised tables and builds the FTS5 index used for searching.
Only one <PRODUCT> is held in memory at a time, so memory use stays flat.

Usage:
    python3 etl/build_db.py                      # all brands under brands/
    python3 etl/build_db.py --xml path/file.xml  # override the first brand's file
"""
import argparse
import os
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from brands import discover_brands, save_brands  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "data", "catalogue.db")
SCHEMA = os.path.join(ROOT, "etl", "schema.sql")

# What the MIME codes mean: derived from the MIME_DESIGNATION distribution in the data.
MIME_CODES = [
    ("MD01", "Product photo", "image"),
    ("MD04", "Manufacturer product page", "link"),
    ("MD05", "REACH declaration", "doc"),
    ("MD12", "Dimensional drawing", "image"),
    ("MD14", "Installation instructions", "doc"),
    ("MD19", "Photometric data (LDT/IES/ULD)", "photometry"),
    ("MD20", "Ambient photo", "image"),
    ("MD21", "User manual", "doc"),
    ("MD22", "Product data sheet", "doc"),
    ("MD37", "Revit / BIM file", "bim"),
    ("MD39", "Installation video", "video"),
    ("MD45", "Feature video", "video"),
    ("MD46", "360° image", "image"),
    ("MD47", "Thumbnail", "image"),
    ("MD49", "RoHS declaration", "doc"),
    ("MD52", "CE declaration of conformity", "doc"),
    ("MD54", "Environmental product declaration (EPD)", "doc"),
    ("MD56", "Warranty conditions", "doc"),
]


def ln(tag):
    """Strip the namespace and return the local tag name."""
    return tag.rsplit("}", 1)[-1]


def txt(el):
    if el is None or el.text is None:
        return None
    v = el.text.strip()
    return v or None


def lang_of(el):
    return el.get("lang")


def num(v):
    if v is None:
        return None
    try:
        return float(v.replace(",", "."))
    except (ValueError, AttributeError):
        return None


def boolint(v):
    if v is None:
        return None
    return 1 if v.strip().lower() == "true" else 0


class Loader:
    """Batches INSERTs so that millions of rows do not mean millions of round trips."""

    def __init__(self, conn):
        self.conn = conn
        self.buf = {}
        self.counts = {}

    def add(self, table, cols, row):
        key = (table, cols)
        self.buf.setdefault(key, []).append(row)
        self.counts[table] = self.counts.get(table, 0) + 1
        if len(self.buf[key]) >= 5000:
            self.flush_table(key)

    def flush_table(self, key):
        table, cols = key
        rows = self.buf.get(key)
        if not rows:
            return
        ph = ",".join("?" * len(cols))
        self.conn.executemany(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph})", rows)
        self.buf[key] = []

    def flush(self):
        for key in list(self.buf):
            self.flush_table(key)


PRODUCT_COLS = (
    "id", "brand", "supplier_pid", "alt_pid", "gtin", "manufacturer_pid",
    "manufacturer_name", "type_descr_de", "type_descr_en", "short_de", "short_en",
    "long_de", "long_en", "tender_de", "tender_en", "series_de", "series_en",
    "variation_de", "variation_en", "keywords_de", "keywords_en", "status",
    "product_type", "etim_system", "etim_class", "order_unit", "content_unit",
    "no_cu_per_ou", "price_quantity", "quantity_min", "quantity_interval",
    "price_amount", "price_currency", "price_tax", "price_type", "price_valid_from",
    "price_territory", "discount_group", "net_weight", "pack_length", "pack_width",
    "pack_depth", "pack_weight", "customs_number", "country_of_origin", "rohs",
    "ce_marking", "battery_contained", "product_to_stock", "warranty_business",
    "warranty_consumer", "shelf_life", "reach_info", "reach_listdate", "valid_from",
)


def parse_product(elem, pid, ld, brand):
    """Spread a single <PRODUCT> node across the tables."""
    p = {c: None for c in PRODUCT_COLS}
    p["id"] = pid
    p["brand"] = brand
    kw = {"deu": [], "eng": []}

    for sec in elem:
        name = ln(sec.tag)

        if name == "SUPPLIER_PID":
            p["supplier_pid"] = txt(sec)

        elif name == "PRODUCT_DETAILS":
            for c in sec:
                n, v, lg = ln(c.tag), txt(c), lang_of(c)
                if n == "DESCRIPTION_SHORT":
                    p["short_de" if lg == "deu" else "short_en"] = v
                elif n == "DESCRIPTION_LONG":
                    p["long_de" if lg == "deu" else "long_en"] = v
                elif n == "MANUFACTURER_TYPE_DESCR":
                    p["type_descr_de" if lg == "deu" else "type_descr_en"] = v
                elif n == "INTERNATIONAL_PID":
                    p["gtin"] = v
                elif n == "SUPPLIER_ALT_PID":
                    p["alt_pid"] = v
                elif n == "MANUFACTURER_PID":
                    p["manufacturer_pid"] = v
                elif n == "MANUFACTURER_NAME":
                    p["manufacturer_name"] = v
                elif n == "KEYWORD" and v:
                    kw.setdefault(lg or "eng", []).append(v)
                    ld.add("product_keyword", ("product_id", "lang", "keyword"),
                           (pid, lg, v))
                elif n == "PRODUCT_STATUS":
                    p["status"] = v
                elif n == "PRODUCT_TYPE":
                    p["product_type"] = v

        elif name == "PRODUCT_FEATURES":
            for c in sec:
                n = ln(c.tag)
                if n == "REFERENCE_FEATURE_SYSTEM_NAME":
                    p["etim_system"] = txt(c)
                elif n == "REFERENCE_FEATURE_GROUP_ID":
                    p["etim_class"] = txt(c)
                elif n == "FEATURE":
                    fname = details = funit = None
                    vals = []
                    for f in c:
                        fn = ln(f.tag)
                        if fn == "FNAME":
                            fname = txt(f)
                        elif fn == "FVALUE":
                            vals.append(txt(f))
                        elif fn == "FVALUE_DETAILS":
                            details = txt(f)
                        elif fn == "FUNIT":
                            funit = txt(f)
                    if fname:
                        for i, v in enumerate(vals or [None]):
                            ld.add("product_feature",
                                   ("product_id", "fname", "value_idx", "fvalue",
                                    "details", "funit"),
                                   (pid, fname, i, v, details, funit))

        elif name == "PRODUCT_ORDER_DETAILS":
            m = {"ORDER_UNIT": "order_unit", "CONTENT_UNIT": "content_unit",
                 "NO_CU_PER_OU": "no_cu_per_ou", "PRICE_QUANTITY": "price_quantity",
                 "QUANTITY_MIN": "quantity_min",
                 "QUANTITY_INTERVAL": "quantity_interval"}
            for c in sec:
                k = m.get(ln(c.tag))
                if k:
                    p[k] = txt(c)

        elif name == "PRODUCT_PRICE_DETAILS":
            valid_from = None
            for c in sec:
                n = ln(c.tag)
                if n == "DATETIME" and c.get("type") == "valid_start_date":
                    for d in c:
                        if ln(d.tag) == "DATE":
                            valid_from = txt(d)
                elif n == "PRODUCT_PRICE":
                    row = {"price_type": c.get("price_type")}
                    for d in c:
                        dn, dv = ln(d.tag), txt(d)
                        if dn == "PRICE_AMOUNT":
                            row["amount"] = num(dv)
                        elif dn == "PRICE_CURRENCY":
                            row["currency"] = dv
                        elif dn == "TAX":
                            row["tax"] = num(dv)
                        elif dn == "LOWER_BOUND":
                            row["lower_bound"] = dv
                        elif dn == "TERRITORY":
                            row["territory"] = dv
                    ld.add("product_price",
                           ("product_id", "price_type", "amount", "currency", "tax",
                            "lower_bound", "territory", "valid_from"),
                           (pid, row.get("price_type"), row.get("amount"),
                            row.get("currency"), row.get("tax"),
                            row.get("lower_bound"), row.get("territory"), valid_from))
                    # The first tier is also written onto the product row.
                    if p["price_amount"] is None:
                        p["price_amount"] = row.get("amount")
                        p["price_currency"] = row.get("currency")
                        p["price_tax"] = row.get("tax")
                        p["price_type"] = row.get("price_type")
                        p["price_territory"] = row.get("territory")
            p["price_valid_from"] = valid_from

        elif name == "PRODUCT_REFERENCE":
            rt = sec.get("type")
            to = descr = None
            for c in sec:
                n = ln(c.tag)
                if n == "PROD_ID_TO":
                    to = txt(c)
                elif n == "REFERENCE_DESCR":
                    descr = txt(c)
            ld.add("product_ref", ("product_id", "ref_type", "prod_id_to", "descr"),
                   (pid, rt, to, descr))

        elif name == "PRODUCT_LOGISTIC_DETAILS":
            for c in sec:
                n = ln(c.tag)
                if n == "CUSTOMS_TARIFF_NUMBER":
                    for d in c:
                        if ln(d.tag) == "CUSTOMS_NUMBER":
                            p["customs_number"] = txt(d)
                elif n == "COUNTRY_OF_ORIGIN":
                    p["country_of_origin"] = txt(c)

        elif name == "USER_DEFINED_EXTENSIONS":
            parse_udx(sec, p, pid, ld)

    p["keywords_de"] = ", ".join(kw.get("deu", [])) or None
    p["keywords_en"] = ", ".join(kw.get("eng", [])) or None
    ld.add("product", PRODUCT_COLS, tuple(p[c] for c in PRODUCT_COLS))


def parse_udx(sec, p, pid, ld):
    smallest = None  # the smallest packing unit feeds the dimensions on the product row
    for c in sec:
        n, v, lg = ln(c.tag), txt(c), lang_of(c)

        if n == "UDX.EDXF.MIME_INFO":
            for m in c:
                row = {"lang": None}
                for d in m:
                    dn, dv = ln(d.tag), txt(d)
                    if dn == "UDX.EDXF.MIME_SOURCE":
                        row["source"] = dv
                        row["lang"] = lang_of(d) or row["lang"]
                    elif dn == "UDX.EDXF.MIME_CODE":
                        row["code"] = dv
                    elif dn == "UDX.EDXF.MIME_FILENAME":
                        row["filename"] = dv
                    elif dn == "UDX.EDXF.MIME_DESIGNATION":
                        row["designation"] = dv
                    elif dn == "UDX.EDXF.MIME_ORDER":
                        row["ord"] = dv
                    elif dn == "UDX.EDXF.MIME_ISSUE_DATE":
                        row["issue_date"] = dv
                ld.add("product_mime",
                       ("product_id", "code", "designation", "filename", "source",
                        "lang", "ord", "issue_date"),
                       (pid, row.get("code"), row.get("designation"),
                        row.get("filename"), row.get("source"), row.get("lang"),
                        row.get("ord"), row.get("issue_date")))

        elif n == "UDX.EDXF.TENDER_TEXT":
            p["tender_de" if lg == "deu" else "tender_en"] = v
        elif n == "UDX.EDXF.PRODUCT_SERIES":
            p["series_de" if lg == "deu" else "series_en"] = v
        elif n == "UDX.EDXF.PRODUCT_VARIATION":
            p["variation_de" if lg == "deu" else "variation_en"] = v
        elif n == "UDX.EDXF.VALID_FROM":
            p["valid_from"] = v
        elif n == "UDX.EDXF.SHELF_LIFE_PERIOD":
            p["shelf_life"] = v
        elif n == "UDX.EDXF.BATTERY_CONTAINED":
            p["battery_contained"] = boolint(v)
        elif n == "UDX.EDXF.ROHS_INDICATOR":
            p["rohs"] = boolint(v)
        elif n == "UDX.EDXF.CE_MARKING":
            p["ce_marking"] = boolint(v)
        elif n == "UDX.EDXF.PRODUCT_TO_STOCK":
            p["product_to_stock"] = boolint(v)

        elif n == "UDX.EDXF.DISCOUNT_GROUP":
            for d in c:
                if ln(d.tag) == "UDX.EDXF.DISCOUNT_GROUP_MANUFACTURER":
                    p["discount_group"] = txt(d)

        elif n == "UDX.EDXF.REACH":
            for d in c:
                dn, dv = ln(d.tag), txt(d)
                if dn == "UDX.EDXF.REACH.INFO":
                    p["reach_info"] = dv
                elif dn == "UDX.EDXF.REACH.LISTDATE":
                    p["reach_listdate"] = dv

        elif n == "UDX.EDXF.WARRANTY":
            for d in c:
                dn, dv = ln(d.tag), txt(d)
                if dn == "UDX.EDXF.WARRANTY_BUSINESS":
                    p["warranty_business"] = dv
                elif dn == "UDX.EDXF.WARRANTY_CONSUMER":
                    p["warranty_consumer"] = dv

        elif n == "UDX.EDXF.PRODUCT_LOGISTIC_DETAILS":
            for d in c:
                if ln(d.tag) == "UDX.EDXF.NETWEIGHT":
                    p["net_weight"] = num(txt(d))

        elif n == "UDX.EDXF.PACKING_UNITS":
            for u in c:
                row = {}
                m = {"UDX.EDXF.PACKING_UNIT_CODE": "code",
                     "UDX.EDXF.QUANTITY_MIN": "qty_min",
                     "UDX.EDXF.QUANTITY_MAX": "qty_max",
                     "UDX.EDXF.PACKING_PARTS": "parts",
                     "UDX.EDXF.PACKAGE_BREAK": "pkg_break",
                     "UDX.EDXF.VOLUME": "volume", "UDX.EDXF.WEIGHT": "weight",
                     "UDX.EDXF.LENGTH": "length", "UDX.EDXF.WIDTH": "width",
                     "UDX.EDXF.DEPTH": "depth", "UDX.EDXF.GTIN": "gtin"}
                for d in u:
                    k = m.get(ln(d.tag))
                    if k:
                        row[k] = txt(d)
                ld.add("packing_unit",
                       ("product_id", "code", "qty_min", "qty_max", "parts",
                        "pkg_break", "volume", "weight", "length", "width", "depth",
                        "gtin"),
                       (pid, row.get("code"), row.get("qty_min"), row.get("qty_max"),
                        row.get("parts"), row.get("pkg_break"), num(row.get("volume")),
                        num(row.get("weight")), num(row.get("length")),
                        num(row.get("width")), num(row.get("depth")), row.get("gtin")))
                q = num(row.get("qty_min")) or 0
                if smallest is None or q < smallest[0]:
                    smallest = (q, row)

        elif n == "UDX.EDXF.PRODUCT_CHARACTERISTICS":
            for ch in c:
                code = cname = vde = ven = None
                for d in ch:
                    dn, dv, dlg = ln(d.tag), txt(d), lang_of(d)
                    if dn == "UDX.EDXF.PRODUCT_CHARACTERISTIC_CODE":
                        code = dv
                    elif dn == "UDX.EDXF.PRODUCT_CHARACTERISTIC_NAME":
                        cname = dv
                    elif dn.startswith("UDX.EDXF.PRODUCT_CHARACTERISTIC_VALUE"):
                        if dlg == "deu":
                            vde = dv
                        else:
                            ven = dv
                ld.add("characteristic",
                       ("product_id", "code", "name", "value_de", "value_en"),
                       (pid, code, cname, vde, ven))

    if smallest:
        r = smallest[1]
        p["pack_length"] = num(r.get("length"))
        p["pack_width"] = num(r.get("width"))
        p["pack_depth"] = num(r.get("depth"))
        p["pack_weight"] = num(r.get("weight"))


def parse_header(elem, conn, brand):
    info = {}
    for c in elem.iter():
        n, v = ln(c.tag), txt(c)
        if not v:
            continue
        if n in ("CATALOG_ID", "CATALOG_VERSION", "CATALOG_NAME", "TERRITORY",
                 "CURRENCY", "SUPPLIER_NAME", "GENERATOR_INFO"):
            info.setdefault(n, v)
        elif n == "DATE":
            info.setdefault("GENERATION_DATE", v)
        elif n == "LANGUAGE":
            info["LANGUAGES"] = (info.get("LANGUAGES", "") + " " + v).strip()
    conn.executemany(
        "INSERT OR REPLACE INTO catalog_info (brand,key,value) VALUES (?,?,?)",
        [(brand, k, v) for k, v in info.items()])
    return info


def load_brand(conn, ld, brand, xml_path, start_id):
    """Load one brand's BMEcat file; returns the last product id used."""
    pid = start_id
    t0 = time.time()
    print(f"\n### {brand.name} ({brand.code})")
    print(f"  source: {os.path.relpath(xml_path, ROOT)} "
          f"({os.path.getsize(xml_path)/1e9:.2f} GB)")
    ctx = ET.iterparse(xml_path, events=("end",))
    for _ev, elem in ctx:
        tag = ln(elem.tag)
        if tag == "PRODUCT":
            pid += 1
            try:
                parse_product(elem, pid, ld, brand.code)
            except Exception as exc:   # one bad record must not stop the load
                print(f"  ! product #{pid} skipped: {exc}", file=sys.stderr)
            elem.clear()
            if (pid - start_id) % 5000 == 0:
                print(f"  {pid - start_id:>6} products ({time.time()-t0:5.0f} s)",
                      flush=True)
        elif tag == "HEADER":
            parse_header(elem, conn, brand.code)
            elem.clear()
    count = pid - start_id
    conn.executemany(
        "INSERT OR REPLACE INTO catalog_info (brand,key,value) VALUES (?,?,?)", [
            (brand.code, "SOURCE_FILE", os.path.basename(xml_path)),
            (brand.code, "SOURCE_BYTES", str(os.path.getsize(xml_path))),
            (brand.code, "PRODUCT_COUNT", str(count)),
        ])
    print(f"  {count} products, {time.time()-t0:.0f} s")
    return pid


def build(db_path, single_xml=None):
    t0 = time.time()
    tmp = db_path + ".tmp"
    for f in (tmp, tmp + "-journal", tmp + "-wal"):
        if os.path.exists(f):
            os.remove(f)

    conn = sqlite3.connect(tmp)
    conn.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; "
                       "PRAGMA cache_size=-200000; PRAGMA temp_store=MEMORY;")
    with open(SCHEMA, encoding="utf-8") as fh:
        conn.executescript(fh.read())
    conn.executemany("INSERT INTO mime_code (code,label,kind) VALUES (?,?,?)",
                     MIME_CODES)

    ld = Loader(conn)
    brands = discover_brands()
    if not brands:
        sys.exit("No brand directories under brands/. See brands/README.md")
    save_brands(conn, brands)
    print(f"Target : {db_path}")
    print(f"Brands : {', '.join(b.name for b in brands)}")

    pid = 0
    with_data = 0
    for b in brands:
        xml_path = single_xml if (single_xml and b is brands[0]) else b.find_xml()
        if not xml_path:
            print(f"\n### {b.name} ({b.code})")
            print("  no XML under data/ — only its documents will be indexed.")
            continue
        pid = load_brand(conn, ld, b, xml_path, pid)
        with_data += 1

    ld.flush()
    conn.commit()
    if not with_data:
        print("\nWarning: no product data found for any brand.")
    print(f"\nParsing done: {pid} products, {time.time()-t0:.0f} s")
    for t, n in sorted(ld.counts.items()):
        print(f"  {t:<18} {n:>9,}")

    conn.executemany(
        "INSERT OR REPLACE INTO catalog_info (brand,key,value) VALUES (?,?,?)", [
            ("", "IMPORTED_AT",
             datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")),
            ("", "PRODUCT_COUNT", str(pid)),
            ("", "BRAND_COUNT", str(len(brands))),
            ("", "PRICE_VALID", (conn.execute(
                "SELECT price_valid_from FROM product WHERE price_valid_from IS NOT NULL"
                " LIMIT 1").fetchone() or [""])[0]),
        ])

    print("Building indexes...")
    conn.executescript("""
      CREATE INDEX idx_prod_brand     ON product(brand);
      CREATE INDEX idx_prod_gtin      ON product(gtin);
      CREATE INDEX idx_prod_alt       ON product(alt_pid);
      CREATE INDEX idx_prod_class     ON product(etim_class);
      CREATE INDEX idx_prod_series_en ON product(series_en);
      CREATE INDEX idx_prod_price     ON product(price_amount);
      CREATE INDEX idx_feat_prod      ON product_feature(product_id);
      CREATE INDEX idx_feat_nv        ON product_feature(fname, fvalue);
      CREATE INDEX idx_mime_prod      ON product_mime(product_id);
      CREATE INDEX idx_mime_code      ON product_mime(code, product_id);
      CREATE INDEX idx_ref_prod       ON product_ref(product_id);
      CREATE INDEX idx_ref_to         ON product_ref(prod_id_to);
      CREATE INDEX idx_pack_prod      ON packing_unit(product_id);
      CREATE INDEX idx_char_prod      ON characteristic(product_id);
      CREATE INDEX idx_kw_prod        ON product_keyword(product_id);
      CREATE INDEX idx_doc_page       ON doc_page(doc_id, page_no);
      CREATE INDEX idx_docser_series  ON doc_series(brand, series);
      CREATE INDEX idx_docser_doc     ON doc_series(doc_id);
    """)
    conn.commit()

    print("Building full-text index (FTS5)...")
    conn.executescript("""
      CREATE VIRTUAL TABLE product_fts USING fts5(
        supplier_pid, alt_pid, gtin, series_de, series_en,
        type_descr_de, type_descr_en, short_de, short_en,
        long_de, long_en, keywords_de, keywords_en,
        content='product', content_rowid='id',
        tokenize="unicode61 remove_diacritics 2",
        prefix='2 3 4'
      );
      INSERT INTO product_fts(product_fts) VALUES('rebuild');

      CREATE VIRTUAL TABLE doc_fts USING fts5(
        title, body,
        tokenize="unicode61 remove_diacritics 2",
        prefix='2 3 4'
      );
    """)
    conn.commit()

    print("ANALYZE + VACUUM...")
    conn.executescript("ANALYZE;")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()

    os.replace(tmp, db_path)
    print(f"\nDone: {db_path} ({os.path.getsize(db_path)/1e6:.0f} MB) — "
          f"{time.time()-t0:.0f} s total")


def main():
    ap = argparse.ArgumentParser(description="BMEcat XML -> SQLite")
    ap.add_argument("--xml", help="override the first brand's XML (for testing)")
    ap.add_argument("--db", default=DEFAULT_DB, help="target SQLite file")
    args = ap.parse_args()

    if args.xml and not os.path.exists(args.xml):
        sys.exit(f"XML not found: {args.xml}")
    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    build(args.db, single_xml=args.xml)


if __name__ == "__main__":
    main()
