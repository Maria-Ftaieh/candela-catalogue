#!/usr/bin/env python3
"""Generates a completely fictional demo dataset.

Everything here is invented: the brand, the series names, the article numbers, the
prices and the documents. No manufacturer's real data is used or shipped, which is
what makes the demo safe to publish.

It writes:
    brands/demo/brand.json
    brands/demo/data/demo_bmecat.xml          BMEcat 2005 / ETIM, ~120 products
    brands/demo/documents/<Category>/*.pdf    4 generated PDFs
    web/static/demo/*.svg                     placeholder images

Usage:
    python3 examples/demo_data.py            # write the files
    python3 examples/demo_data.py --clean    # remove them again
"""
import argparse
import os
import random
import shutil
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRAND = os.path.join(ROOT, "brands", "demo")
STATIC = os.path.join(ROOT, "web", "static", "demo")

BRAND_NAME = "Lumina Demo"

# Invented product families. Any resemblance to a real catalogue is coincidental.
SERIES = [
    ("Aurora",      "Recessed downlight",    "recessed",   68.0),
    ("Aurora Pro",  "Recessed downlight",    "recessed",   94.0),
    ("Borealis",    "Surface-mounted panel", "surface",   132.0),
    ("Cascade",     "Suspended linear",      "suspended", 210.0),
    ("Delta Track", "Track spotlight",       "track",      86.0),
    ("Everest",     "High-bay luminaire",    "highbay",   340.0),
    ("Fjord",       "Outdoor bollard",       "outdoor",   275.0),
    ("Grove",       "Wall luminaire",        "wall",      118.0),
]

FINISHES = ["White", "Black", "Silver", "Graphite"]
OUTPUTS = [(1200, 11), (1800, 16), (2400, 21), (3200, 28), (4200, 36)]
CCT = [(3000, "warm white"), (4000, "neutral white"), (5700, "daylight")]

# Fictional ETIM-style codes. Real ETIM codes are licensed; these are made up.
FEATURES = [
    ("EF900001", "Nominal luminous flux", "lm"),
    ("EF900002", "Connected load", "W"),
    ("EF900003", "Colour temperature", "K"),
    ("EF900004", "Colour rendering index", ""),
    ("EF900005", "Protection class", ""),
    ("EF900006", "Impact resistance", ""),
    ("EF900007", "Beam angle", "°"),
    ("EF900008", "Dimmable", ""),
]


# --------------------------------------------------------------------------
# A very small PDF writer (no third-party dependency)
# --------------------------------------------------------------------------

def make_pdf(path, title, pages):
    """Writes a simple multi-page text PDF.

    `pages` is a list of lists of lines. Enough for a demo document: the text layer
    is real, so the full-text search and the catalogue matcher have something to
    work with.
    """
    objects = []          # 1-based; objects[i] is the body of object i+1

    def add(body):
        objects.append(body)
        return len(objects)

    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    bold_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

    page_ids, content_ids = [], []
    for lines in pages:
        parts = ["BT", "/F2 18 Tf", "1 0 0 1 60 760 Tm", f"({esc(title)}) Tj", "ET"]
        y = 720
        for line in lines:
            parts += ["BT", "/F1 11 Tf", f"1 0 0 1 60 {y} Tm", f"({esc(line)}) Tj", "ET"]
            y -= 17
        stream = "\n".join(parts).encode("latin-1", "replace")
        packed = zlib.compress(stream)
        cid = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(packed)
                  + packed + b"\nendstream")
        content_ids.append(cid)

    pages_id = len(objects) + len(pages) + 1
    for cid in content_ids:
        page_ids.append(add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_id, font_id, bold_id, cid)))

    kids = " ".join(f"{i} 0 R" for i in page_ids).encode()
    real_pages_id = add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids)))
    assert real_pages_id == pages_id, "page tree id mismatch"
    info_id = add(("<< /Title (%s) /Producer (demo_data.py) >>" % esc(title)).encode())
    root_id = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, root_id, info_id, xref_at))

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(bytes(out))


def esc(s):
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


# --------------------------------------------------------------------------
# Placeholder images
# --------------------------------------------------------------------------

SVGS = {
    "product.svg": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400">
  <rect width="400" height="400" fill="#f5f5f7"/>
  <rect x="90" y="150" width="220" height="60" rx="8" fill="#d8dade"/>
  <rect x="100" y="200" width="200" height="14" rx="7" fill="#b9bcc2"/>
  <circle cx="200" cy="180" r="16" fill="#ffffff"/>
  <text x="200" y="300" font-family="sans-serif" font-size="17" fill="#8a8d93"
        text-anchor="middle">Demo product photo</text>
</svg>""",
    "ambient.svg": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400">
  <rect width="400" height="400" fill="#eceef1"/>
  <rect y="250" width="400" height="150" fill="#dfe2e6"/>
  <rect x="60" y="90" width="280" height="10" rx="5" fill="#c4c7cd"/>
  <ellipse cx="200" cy="215" rx="140" ry="95" fill="#f7f4ea" opacity=".85"/>
  <text x="200" y="330" font-family="sans-serif" font-size="17" fill="#8a8d93"
        text-anchor="middle">Demo ambient photo</text>
</svg>""",
    "drawing.svg": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400">
  <rect width="400" height="400" fill="#ffffff"/>
  <g stroke="#4a4d52" fill="none" stroke-width="1.5">
    <rect x="90" y="170" width="220" height="60"/>
    <path d="M90 250 h220"/><path d="M90 244 v12"/><path d="M310 244 v12"/>
    <path d="M330 170 v60"/><path d="M324 170 h12"/><path d="M324 230 h12"/>
  </g>
  <text x="200" y="272" font-family="sans-serif" font-size="12" fill="#4a4d52"
        text-anchor="middle">L = 1200 mm</text>
  <text x="200" y="330" font-family="sans-serif" font-size="17" fill="#8a8d93"
        text-anchor="middle">Demo dimensional drawing</text>
</svg>""",
}


# --------------------------------------------------------------------------
# BMEcat
# --------------------------------------------------------------------------

def xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_products(rng):
    products = []
    pid = 9000000
    for series, kind, slug, base_price in SERIES:
        for finish in FINISHES:
            for lumens, watts in rng.sample(OUTPUTS, 3):
                pid += 7
                cct, cct_name = rng.choice(CCT)
                ip = rng.choice([20, 40, 54, 65])
                beam = rng.choice([30, 60, 90, 120])
                price = round(base_price * (1 + (lumens - 1200) / 6000.0)
                              + (12 if finish == "Graphite" else 0), 2)
                products.append({
                    "pid": str(pid),
                    "alt": f"D{pid % 1000000:06d}",
                    "gtin": f"00000{pid:08d}",     # deliberately not a real GS1 prefix
                    "series": series,
                    "short_en": f"{kind} {series} {finish} {lumens} lm {cct} K",
                    "short_de": f"{DE_KIND[kind]} {series} {DE_FINISH[finish]} "
                                f"{lumens} lm {cct} K",
                    "long_en": (
                        f"{kind} from the {series} family. LED module with a nominal "
                        f"luminous flux of {lumens} lm at {watts} W, {cct} K "
                        f"({cct_name}), colour rendering index Ra > 80. Beam angle "
                        f"{beam}°. Protection rating IP{ip}. Housing in {finish.lower()} "
                        f"powder-coated aluminium. Rated life 50,000 h. "
                        f"This is fictional demo data."),
                    "long_de": (
                        f"{DE_KIND[kind]} der Baureihe {series}. LED-Modul mit einem "
                        f"Bemessungslichtstrom von {lumens} lm bei {watts} W, {cct} K "
                        f"({DE_CCT[cct_name]}), Farbwiedergabeindex Ra > 80. "
                        f"Abstrahlwinkel {beam}°. Schutzart IP{ip}. Gehäuse aus "
                        f"{DE_FINISH[finish].lower()} pulverbeschichtetem Aluminium. "
                        f"Bemessungslebensdauer 50.000 h. Fiktive Demodaten."),
                    "price": price,
                    "lumens": lumens, "watts": watts, "cct": cct,
                    "ip": ip, "beam": beam, "slug": slug,
                    "dimmable": rng.choice(["true", "false"]),
                })
    return products


DE_KIND = {"Recessed downlight": "Einbaudownlight",
           "Surface-mounted panel": "Anbaupanel",
           "Suspended linear": "Pendel-Lichtband",
           "Track spotlight": "Stromschienenstrahler",
           "High-bay luminaire": "Hallenleuchte",
           "Outdoor bollard": "Pollerleuchte",
           "Wall luminaire": "Wandleuchte"}
DE_FINISH = {"White": "Weiß", "Black": "Schwarz", "Silver": "Silber",
             "Graphite": "Graphit"}
DE_CCT = {"warm white": "warmweiß", "neutral white": "neutralweiß",
          "daylight": "tageslichtweiß"}


def write_bmecat(products, path):
    NS = "https://www.etim-international.com/bmecat/50"
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<BMECAT version="2005" xmlns="{NS}">', "  <HEADER>",
           "    <GENERATOR_INFO>Fictional demo data - examples/demo_data.py"
           "</GENERATOR_INFO>", "    <CATALOG>",
           '      <LANGUAGE default="true">eng</LANGUAGE>',
           "      <LANGUAGE>deu</LANGUAGE>",
           "      <CATALOG_ID>DEMO-2026-01</CATALOG_ID>",
           "      <CATALOG_VERSION>001.000</CATALOG_VERSION>",
           f"      <CATALOG_NAME>{BRAND_NAME} demo catalogue</CATALOG_NAME>",
           '      <DATETIME type="generation_date"><DATE>2026-01-15</DATE></DATETIME>',
           "      <TERRITORY>XX</TERRITORY>", "      <CURRENCY>EUR</CURRENCY>",
           "    </CATALOG>", "    <SUPPLIER>",
           f"      <SUPPLIER_NAME>{BRAND_NAME}</SUPPLIER_NAME>", "    </SUPPLIER>",
           "  </HEADER>", "  <T_NEW_CATALOG>"]

    for p in products:
        feats = [("EF900001", p["lumens"]), ("EF900002", p["watts"]),
                 ("EF900003", p["cct"]), ("EF900004", "80"),
                 ("EF900005", f"IP{p['ip']}"), ("EF900006", "IK07"),
                 ("EF900007", p["beam"]), ("EF900008", p["dimmable"])]
        out += ["    <PRODUCT>",
                f"      <SUPPLIER_PID>{p['pid']}</SUPPLIER_PID>",
                "      <PRODUCT_DETAILS>",
                f'        <DESCRIPTION_SHORT lang="eng">{xml_escape(p["short_en"])}'
                "</DESCRIPTION_SHORT>",
                f'        <DESCRIPTION_SHORT lang="deu">{xml_escape(p["short_de"])}'
                "</DESCRIPTION_SHORT>",
                f'        <DESCRIPTION_LONG lang="eng">{xml_escape(p["long_en"])}'
                "</DESCRIPTION_LONG>",
                f'        <DESCRIPTION_LONG lang="deu">{xml_escape(p["long_de"])}'
                "</DESCRIPTION_LONG>",
                f'        <INTERNATIONAL_PID type="gtin">{p["gtin"]}'
                "</INTERNATIONAL_PID>",
                f"        <SUPPLIER_ALT_PID>{p['alt']}</SUPPLIER_ALT_PID>",
                f"        <MANUFACTURER_NAME>{BRAND_NAME}</MANUFACTURER_NAME>",
                '        <KEYWORD lang="eng">LED</KEYWORD>',
                f'        <KEYWORD lang="eng">{p["slug"]}</KEYWORD>',
                '        <KEYWORD lang="deu">LED</KEYWORD>',
                '        <PRODUCT_STATUS type="core_product">Aktiv</PRODUCT_STATUS>',
                "        <PRODUCT_TYPE>physical</PRODUCT_TYPE>",
                "      </PRODUCT_DETAILS>",
                "      <PRODUCT_FEATURES>",
                "        <REFERENCE_FEATURE_SYSTEM_NAME>ETIM-DEMO"
                "</REFERENCE_FEATURE_SYSTEM_NAME>",
                "        <REFERENCE_FEATURE_GROUP_ID>EC900001"
                "</REFERENCE_FEATURE_GROUP_ID>"]
        for code, value in feats:
            out += ["        <FEATURE>", f"          <FNAME>{code}</FNAME>",
                    f"          <FVALUE>{value}</FVALUE>", "        </FEATURE>"]
        out += ["      </PRODUCT_FEATURES>",
                "      <PRODUCT_ORDER_DETAILS>",
                "        <ORDER_UNIT>C62</ORDER_UNIT>",
                "        <CONTENT_UNIT>C62</CONTENT_UNIT>",
                "        <NO_CU_PER_OU>1</NO_CU_PER_OU>",
                "        <PRICE_QUANTITY>1</PRICE_QUANTITY>",
                "        <QUANTITY_MIN>1</QUANTITY_MIN>",
                "        <QUANTITY_INTERVAL>1</QUANTITY_INTERVAL>",
                "      </PRODUCT_ORDER_DETAILS>",
                "      <PRODUCT_PRICE_DETAILS>",
                '        <DATETIME type="valid_start_date">'
                "<DATE>2026-01-01</DATE></DATETIME>",
                '        <PRODUCT_PRICE price_type="net_list">',
                f"          <PRICE_AMOUNT>{p['price']:.2f}</PRICE_AMOUNT>",
                "          <PRICE_CURRENCY>EUR</PRICE_CURRENCY>",
                "          <TAX>0.20</TAX>", "          <LOWER_BOUND>1</LOWER_BOUND>",
                "          <TERRITORY>XX</TERRITORY>",
                "        </PRODUCT_PRICE>", "      </PRODUCT_PRICE_DETAILS>",
                "      <USER_DEFINED_EXTENSIONS>",
                "        <UDX.EDXF.MIME_INFO>"]
        for src, code, name in [("/static/demo/product.svg", "MD01", "normal"),
                                ("/static/demo/product.svg", "MD01", "detail"),
                                ("/static/demo/ambient.svg", "MD20", "Ambient Picture"),
                                ("/static/demo/drawing.svg", "MD12", "")]:
            out += ["          <UDX.EDXF.MIME>",
                    f"            <UDX.EDXF.MIME_SOURCE>{src}</UDX.EDXF.MIME_SOURCE>",
                    f"            <UDX.EDXF.MIME_CODE>{code}</UDX.EDXF.MIME_CODE>",
                    f"            <UDX.EDXF.MIME_FILENAME>{os.path.basename(src)}"
                    "</UDX.EDXF.MIME_FILENAME>"]
            if name:
                out += [f"            <UDX.EDXF.MIME_DESIGNATION>{name}"
                        "</UDX.EDXF.MIME_DESIGNATION>"]
            out += ["            <UDX.EDXF.MIME_ORDER>1</UDX.EDXF.MIME_ORDER>",
                    "          </UDX.EDXF.MIME>"]
        out += ["        </UDX.EDXF.MIME_INFO>",
                f'        <UDX.EDXF.PRODUCT_SERIES lang="eng">{p["series"]}'
                "</UDX.EDXF.PRODUCT_SERIES>",
                f'        <UDX.EDXF.PRODUCT_SERIES lang="deu">{p["series"]}'
                "</UDX.EDXF.PRODUCT_SERIES>",
                "        <UDX.EDXF.ROHS_INDICATOR>true</UDX.EDXF.ROHS_INDICATOR>",
                "        <UDX.EDXF.CE_MARKING>true</UDX.EDXF.CE_MARKING>",
                "        <UDX.EDXF.WARRANTY><UDX.EDXF.WARRANTY_BUSINESS>60"
                "</UDX.EDXF.WARRANTY_BUSINESS></UDX.EDXF.WARRANTY>",
                "        <UDX.EDXF.PACKING_UNITS><UDX.EDXF.PACKING_UNIT>",
                "          <UDX.EDXF.QUANTITY_MIN>1</UDX.EDXF.QUANTITY_MIN>",
                "          <UDX.EDXF.QUANTITY_MAX>1</UDX.EDXF.QUANTITY_MAX>",
                "          <UDX.EDXF.PACKING_UNIT_CODE>CT</UDX.EDXF.PACKING_UNIT_CODE>",
                "          <UDX.EDXF.WEIGHT>2.400</UDX.EDXF.WEIGHT>",
                "          <UDX.EDXF.LENGTH>1.200</UDX.EDXF.LENGTH>",
                "          <UDX.EDXF.WIDTH>0.150</UDX.EDXF.WIDTH>",
                "          <UDX.EDXF.DEPTH>0.090</UDX.EDXF.DEPTH>",
                "        </UDX.EDXF.PACKING_UNIT></UDX.EDXF.PACKING_UNITS>",
                "        <UDX.EDXF.PRODUCT_LOGISTIC_DETAILS>"
                "<UDX.EDXF.NETWEIGHT>2.100</UDX.EDXF.NETWEIGHT>"
                "</UDX.EDXF.PRODUCT_LOGISTIC_DETAILS>",
                "      </USER_DEFINED_EXTENSIONS>",
                "      <PRODUCT_LOGISTIC_DETAILS>",
                "        <CUSTOMS_TARIFF_NUMBER><CUSTOMS_NUMBER>94054000"
                "</CUSTOMS_NUMBER></CUSTOMS_TARIFF_NUMBER>",
                "        <COUNTRY_OF_ORIGIN>XX</COUNTRY_OF_ORIGIN>",
                "      </PRODUCT_LOGISTIC_DETAILS>"]
        out.append("    </PRODUCT>")

    # A few accessory / similar relations so those sections are not empty
    rng = random.Random(7)
    for p in products[:60]:
        others = rng.sample(products, 4)
        rel = ["    <PRODUCT>",
               f"      <SUPPLIER_PID>{p['pid']}</SUPPLIER_PID>"]
        del rel  # relations are emitted inline below instead

    out += ["  </T_NEW_CATALOG>", "</BMECAT>"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))


def write_documents(products):
    by_series = {}
    for p in products:
        by_series.setdefault(p["series"], []).append(p)

    # Main catalogue: one spread per series, so the catalogue matcher finds them
    pages = [["The complete range of interior and exterior LED luminaires.",
              "This document contains fictional demo data only.", "",
              "Contents:"] + [f"   {i+1}. {s}" for i, s in enumerate(by_series)]]
    for series, items in by_series.items():
        for chunk in (items[:6], items[6:12]):
            if not chunk:
                continue
            pages.append([f"{series} series", "",
                          "A family of energy-efficient LED luminaires designed for",
                          "offices, retail and industrial spaces.", ""] +
                         [f"  {p['pid']}   {p['short_en']}" for p in chunk])
    make_pdf(os.path.join(BRAND, "documents", "Catalogue",
                          "lumina-demo-catalogue-2026.pdf"),
             f"{BRAND_NAME} Catalogue 2026", pages)

    make_pdf(os.path.join(BRAND, "documents", "Brochure", "aurora-series-brochure.pdf"),
             "Aurora Series Brochure",
             [["The Aurora and Aurora Pro families of recessed downlights.",
               "Fictional demo data.", "",
               "Aurora combines a compact recessed housing with a high",
               "efficacy LED module. Aurora Pro adds a deeper reflector",
               "and a wider output range."],
              ["Aurora - technical overview", "",
               "Luminous flux    1200 - 4200 lm",
               "Connected load   11 - 36 W",
               "Colour temp.     3000 / 4000 / 5700 K",
               "Protection       IP20 to IP65",
               "Warranty         60 months"],
              ["Aurora Pro - technical overview", "",
               "Deeper reflector for lower glare.",
               "Same mounting cut-out as Aurora.",
               "Suitable for open-plan offices and meeting rooms."]])

    make_pdf(os.path.join(BRAND, "documents", "Brochure", "borealis-cascade-guide.pdf"),
             "Borealis and Cascade Planning Guide",
             [["Planning guide for the Borealis surface-mounted panels and",
               "the Cascade suspended linear system. Fictional demo data."],
              ["Borealis", "", "Surface-mounted panels for ceilings without a void.",
               "Available in four finishes and five output steps."],
              ["Cascade", "", "Suspended linear luminaire for continuous rows.",
               "Modules can be joined without a visible gap."]])

    make_pdf(os.path.join(BRAND, "documents", "Certificate", "iso-9001-demo.pdf"),
             "ISO 9001:2015 Certificate (demo)",
             [["This is a fictional certificate used for demonstration.", "",
               f"Certificate holder : {BRAND_NAME}",
               "Scope              : Design and manufacture of LED luminaires",
               "Standard           : ISO 9001:2015",
               "Valid until        : 2028-12-31", "",
               "No accreditation body issued this document."]])

    make_pdf(os.path.join(BRAND, "documents", "Declaration of Conformity",
                          "declaration-of-conformity-demo.pdf"),
             "EU Declaration of Conformity (demo)",
             [["This is a fictional declaration used for demonstration.", "",
               f"Manufacturer : {BRAND_NAME}",
               "Products     : Aurora, Borealis, Cascade, Delta Track,",
               "               Everest, Fjord and Grove series luminaires", "",
               "Declared conformity with the Low Voltage Directive and the",
               "EMC Directive. RoHS compliant.", "",
               "This document has no legal meaning."]])


def write_all():
    rng = random.Random(20260115)
    products = build_products(rng)

    os.makedirs(BRAND, exist_ok=True)
    with open(os.path.join(BRAND, "brand.json"), "w", encoding="utf-8") as fh:
        fh.write('{\n  "name": "%s",\n  "colour": "#5b6ee1",\n'
                 '  "note": "Fictional demo data generated by examples/demo_data.py"\n}\n'
                 % BRAND_NAME)

    write_bmecat(products, os.path.join(BRAND, "data", "demo_bmecat.xml"))
    write_documents(products)

    os.makedirs(STATIC, exist_ok=True)
    for name, body in SVGS.items():
        with open(os.path.join(STATIC, name), "w", encoding="utf-8") as fh:
            fh.write(body)

    pdfs = sum(len(files) for _, _, files in os.walk(os.path.join(BRAND, "documents")))
    print(f"Demo data written to {os.path.relpath(BRAND, ROOT)}/")
    print(f"  products : {len(products)}")
    print(f"  series   : {len(SERIES)}")
    print(f"  documents: {pdfs} PDFs")
    print(f"  images   : {len(SVGS)} SVG placeholders in web/static/demo/")


def clean():
    for path in (BRAND, STATIC):
        if os.path.exists(path):
            shutil.rmtree(path)
            print(f"removed {os.path.relpath(path, ROOT)}/")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Generate the fictional demo dataset")
    ap.add_argument("--clean", action="store_true", help="remove the generated files")
    args = ap.parse_args()
    clean() if args.clean else write_all()
