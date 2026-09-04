#!/usr/bin/env python3
"""Product and document catalogue — web search interface.

Run with:
    ./run.sh
    or: .venv/bin/uvicorn web.app:app --reload
"""
import csv
import io
import os
import re
import sqlite3
import threading
from typing import Annotated, Optional
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BeforeValidator
from starlette.concurrency import run_in_threadpool

from web import auth


def _to_number(convert):
    """Builds a validator that turns unparseable numeric parameters into None.

    It covers two cases:
      * `?pmin=&pmax=` — an HTML form submits empty fields too. An empty string is
        not a number, so FastAPI rejected the request with 422 and the user could
        not search at all while leaving the price filter blank.
      * `?pmin=abc` — a hand-edited URL. These are *filters*, so the filter is
        simply ignored instead of showing the user a raw JSON error.
    """
    def validate(v):
        if v is None:
            return None
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return None
            if "," in v and "." not in v:
                v = v.replace(",", ".")   # decimal comma
            try:
                return convert(v)
            except ValueError:
                return None
        return v
    return validate


Number = Annotated[Optional[float], BeforeValidator(_to_number(float))]
Integer = Annotated[Optional[int], BeforeValidator(_to_number(lambda x: int(float(x))))]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("CATALOGUE_DB", os.path.join(ROOT, "data", "catalogue.db"))
WEB = os.path.join(ROOT, "web")
PAGE_SIZE = 50
CSV_MAX = 5000

# Public demo instance: credentials are shown on the sign-in page and every
# destructive action is refused, so a visitor cannot lock the demo for everyone.
DEMO_MODE = os.environ.get("DEMO_MODE", "").lower() not in ("", "0", "false", "no")
DEMO_USER = os.environ.get("DEMO_USER", "demo")
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "demo1234demo")

app = FastAPI(title="Catalogue", docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(WEB, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(WEB, "templates"))

# Paths that do not require a login
OPEN_PATHS = {"/login", "/health", "/favicon.ico"}

_local = threading.local()
_cache = {}
_cache_stamp = None
# RLock: a cached function (brand_names) calls another cached one (brands). A plain
# Lock would make the same thread wait for a lock it already holds, and hang.
_cache_lock = threading.RLock()


@app.middleware("http")
async def session_middleware(request: Request, call_next):
    """Ties every request to a signed-in user.

    Authentication lives in the application rather than in the reverse proxy, so
    that there are per-person accounts, roles and revocable sessions.
    """
    path = request.url.path
    if path.startswith("/static/") or path in OPEN_PATHS:
        return await call_next(request)

    token = request.cookies.get(auth.COOKIE)
    user = await run_in_threadpool(auth.session_user, token)
    if not user:
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        response = RedirectResponse(f"/login?next={quote(target, safe='')}",
                                    status_code=303)
        response.delete_cookie(auth.COOKIE, path="/")
        return response

    # A new account must choose its own password before doing anything else
    if user["must_change"] and path not in ("/account", "/logout"):
        return RedirectResponse("/account?first=1", status_code=303)

    request.state.user = user
    request.state.token = token
    return await call_next(request)


def current_user(request):
    return getattr(request.state, "user", None)


def require_admin(request):
    u = current_user(request)
    if not u or u["role"] != "admin":
        raise HTTPException(403, "This page requires administrator rights.")
    return u


def _db_stamp():
    """The database file's mtime. It changes when the ETL rebuilds it."""
    try:
        return os.path.getmtime(DB_PATH)
    except OSError:
        return None


def cached(key, produce):
    """Keeps a result until the database changes (used for static filter lists).

    After the monthly ETL the file changes, so the cache clears itself and the
    server does not need restarting.
    """
    global _cache_stamp
    stamp = _db_stamp()
    with _cache_lock:
        if stamp != _cache_stamp:
            _cache.clear()
            _cache_stamp = stamp
        if key not in _cache:
            _cache[key] = produce()
        return _cache[key]


def db():
    """A read-only connection per thread.

    When the ETL swaps in a new file (os.replace) the old connection still points
    at the old inode, so the connection is refreshed when the mtime changes.
    """
    stamp = _db_stamp()
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "stamp", None) != stamp:
        conn.close()
        conn = None
    if conn is None:
        uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _local.conn = conn
        _local.stamp = stamp
    return conn


def _qs_replace(qp, **changes):
    """Keeps the current query string but changes a few parameters (for links)."""
    from urllib.parse import urlencode
    d = dict(qp)
    d.update(changes)
    return urlencode({k: v for k, v in d.items() if v not in (None, "")})


def _series_name(name):
    """Cleans a series name for display: '765... E-Line' -> 'E-Line'.

    Links keep using the raw value, because that is what the join runs on.
    """
    return re.sub(r"^\d*\.\.\.\s*", "", name or "").strip() or name


templates.env.filters["series_name"] = _series_name
templates.env.filters["replace_lang"] = lambda qp, lang: _qs_replace(qp, lang=lang)
templates.env.filters["set_page"] = lambda qp, page: _qs_replace(qp, page=page)


# --------------------------------------------------------------------------
# Search helpers
# --------------------------------------------------------------------------

def fts_query(q):
    """Turns user text into a safe FTS5 query.

    FTS5 special characters (", *, :, -, ^) would break the query, so every word is
    quoted and given a prefix star: `lumega* 600*`.
    """
    tokens = re.findall(r"\w+", q, flags=re.UNICODE)
    return " ".join(f'"{t}"*' for t in tokens if t)


def looks_like_code(q):
    """Article-number-like queries get an exact lookup first."""
    s = q.strip()
    return s.isdigit() and len(s) >= 5


LANGS = {"en": ("short_en", "long_en", "series_en", "type_descr_en", "keywords_en"),
         "de": ("short_de", "long_de", "series_de", "type_descr_de", "keywords_de")}


def lang_cols(lang):
    return LANGS.get(lang, LANGS["en"])


def build_filters(series, etim, pmin, pmax, status, doc, catalog="", brand=""):
    """Shared WHERE fragments and their parameters."""
    where, params = [], []
    if brand:
        where.append("p.brand = ?")
        params.append(brand)
    if series:
        where.append("(p.series_en = ? OR p.series_de = ?)")
        params += [series, series]
    if etim:
        where.append("p.etim_class = ?")
        params.append(etim)
    if pmin is not None:
        where.append("p.price_amount >= ?")
        params.append(pmin)
    if pmax is not None:
        where.append("p.price_amount <= ?")
        params.append(pmax)
    if status:
        where.append("p.status = ?")
        params.append(status)
    if doc:
        where.append("EXISTS (SELECT 1 FROM product_mime m "
                     "WHERE m.product_id = p.id AND m.code = ?)")
        params.append(doc)
    if catalog:
        where.append("EXISTS (SELECT 1 FROM doc_series ds "
                     "WHERE ds.series = p.series_en AND ds.brand = p.brand)")
    return where, params


def search_products(q, lang, series, etim, pmin, pmax, status, doc, limit, offset,
                    catalog="", brand=""):
    """Returns (rows, total)."""
    short_c, _long_c, series_c, type_c, _kw = lang_cols(lang)
    where, params = build_filters(series, etim, pmin, pmax, status, doc, catalog, brand)

    select = (f"p.id, p.brand, p.supplier_pid, p.gtin, p.alt_pid, "
              f"p.{short_c} AS title, p.{series_c} AS series, p.{type_c} AS type_name, "
              f"p.price_amount, p.price_currency, p.etim_class, p.status")

    if q and looks_like_code(q):
        w = where + ["(p.supplier_pid = ? OR p.gtin = ? OR p.alt_pid = ?)"]
        pr = params + [q.strip(), q.strip(), q.strip()]
        sql = f"SELECT {select} FROM product p WHERE {' AND '.join(w)}"
        rows = db().execute(sql + f" LIMIT {limit} OFFSET {offset}", pr).fetchall()
        if rows or offset:
            total = db().execute(
                f"SELECT count(*) FROM product p WHERE {' AND '.join(w)}", pr
            ).fetchone()[0]
            return rows, total
        # Not found as a code: fall through to the normal search.

    if q:
        match = fts_query(q)
        if not match:
            return [], 0
        w = ["product_fts MATCH ?"] + where
        pr = [match] + params
        base = ("FROM product_fts f JOIN product p ON p.id = f.rowid "
                f"WHERE {' AND '.join(w)}")
        rows = db().execute(
            f"SELECT {select} {base} ORDER BY bm25(product_fts) LIMIT ? OFFSET ?",
            pr + [limit, offset]).fetchall()
        total = db().execute(f"SELECT count(*) {base}", pr).fetchone()[0]
        return rows, total

    # No query: filters only
    w = where or ["1=1"]
    base = f"FROM product p WHERE {' AND '.join(w)}"
    rows = db().execute(
        f"SELECT {select} {base} ORDER BY p.supplier_pid LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()
    total = db().execute(f"SELECT count(*) {base}", params).fetchone()[0]
    return rows, total


def search_docs(q, limit=50):
    if not q:
        return db().execute(
            "SELECT id, brand, title, category, filename, pages, bytes, sha1, "
            "NULL AS excerpt FROM doc ORDER BY brand, category, title LIMIT ?",
            (limit,)).fetchall()
    match = fts_query(q)
    if not match:
        return []
    return db().execute("""
        SELECT d.id, d.brand, d.title, d.category, d.filename, d.pages, d.bytes, d.sha1,
               snippet(doc_fts, 1, '<mark>', '</mark>', ' … ', 18) AS excerpt
        FROM doc_fts f JOIN doc d ON d.id = f.rowid
        WHERE doc_fts MATCH ? ORDER BY bm25(doc_fts) LIMIT ?""",
        (match, limit)).fetchall()


# --------------------------------------------------------------------------
# Shared template data
# --------------------------------------------------------------------------

def catalogue_info():
    """General metadata (the brand='' rows)."""
    return cached("catalogue", lambda: {
        r["key"]: r["value"]
        for r in db().execute("SELECT key, value FROM catalog_info WHERE brand = ''")})


def brands():
    """Brand list with product/document counts, used by filters and badges."""
    def produce():
        return db().execute("""
            SELECT b.code, b.name, b.colour,
                   (SELECT count(*) FROM product p WHERE p.brand = b.code) AS products,
                   (SELECT count(*) FROM doc d     WHERE d.brand = b.code) AS documents,
                   (SELECT value FROM catalog_info c
                    WHERE c.brand = b.code AND c.key = 'CATALOG_VERSION') AS version
            FROM brand b ORDER BY b.sort_order, b.name""").fetchall()
    return cached("brands", produce)


def brand_names():
    return cached("brand_names", lambda: {b["code"]: b["name"] for b in brands()})


def series_list(limit=400):
    return cached("series", lambda: _series_list(limit))


def _series_list(limit):
    return db().execute(
        "SELECT series_en AS name, count(*) n FROM product "
        "WHERE series_en IS NOT NULL GROUP BY 1 ORDER BY n DESC, name LIMIT ?",
        (limit,)).fetchall()


def doc_types():
    return cached("doc_types", _doc_types)


def _doc_types():
    return db().execute("""
        SELECT mc.code, mc.label, count(DISTINCT m.product_id) n
        FROM mime_code mc JOIN product_mime m ON m.code = mc.code
        WHERE mc.kind IN ('doc','photometry','bim')
        GROUP BY 1,2 ORDER BY mc.label""").fetchall()


def product_catalogues(series, brand):
    """Catalogues and brochures linked to the product's series.

    The same PDF can sit in more than one directory, so rows are de-duplicated by
    sha1; the MIN/MAX aggregate keeps the fields of the highest-scoring row.
    """
    if not series:
        return []
    return db().execute("""
        SELECT d.id AS doc_id, d.title, d.category, d.pages AS total_pages,
               MAX(ds.score) AS score, ds.strength, ds.page_count,
               ds.first_page, ds.pages
        FROM doc_series ds JOIN doc d ON d.id = ds.doc_id
        WHERE ds.series = ? AND ds.brand = ?
        GROUP BY d.sha1
        ORDER BY score DESC, d.title""", (series, brand)).fetchall()


# Image kinds shown in the gallery, and their order
GALLERY_ORDER = {"MD01": 1, "MD20": 2, "MD12": 3, "MD46": 4}


def product_images(pid):
    """Images embedded on the product page.

    MD01 comes in two sizes: 'normal' (~300px, shown inline) and 'detail' (~1200px,
    opened on click). The small thumbnail (MD47) is not used in the gallery; it is
    already listed among the files.
    """
    rows = db().execute("""
        SELECT m.code, m.designation, m.source, m.filename
        FROM product_mime m JOIN mime_code mc ON mc.code = m.code
        WHERE m.product_id = ? AND mc.kind = 'image' AND m.code <> 'MD47'
        ORDER BY m.code, m.ord""", (pid,)).fetchall()

    detail = next((r["source"] for r in rows
                   if r["code"] == "MD01" and r["designation"] == "detail"), None)
    images, seen = [], set()
    for r in rows:
        if r["code"] == "MD01" and r["designation"] == "detail":
            continue                       # it is the large version of 'normal'
        if r["source"] in seen:
            continue
        seen.add(r["source"])
        if r["code"] == "MD01":
            label, full = "Product photo", detail or r["source"]
        elif r["code"] == "MD20":
            label, full = "Ambient photo", r["source"]
        elif r["code"] == "MD12":
            label, full = "Dimensional drawing", r["source"]
        else:
            label, full = "360° image", r["source"]
        images.append({"source": r["source"], "full": full, "label": label,
                       "code": r["code"], "filename": r["filename"]})
    images.sort(key=lambda g: GALLERY_ORDER.get(g["code"], 9))
    return images


def doc_series_map():
    """Document -> the series it covers (shown as badges)."""
    def produce():
        grouped = {}
        for r in db().execute("""
                SELECT ds.doc_id, ds.brand, ds.series, ds.strength, ds.page_count,
                       (SELECT count(*) FROM product p
                        WHERE p.series_en = ds.series AND p.brand = ds.brand) AS products
                FROM doc_series ds
                ORDER BY ds.score DESC, ds.series"""):
            grouped.setdefault(r["doc_id"], []).append(r)
        return grouped
    return cached("doc_series", produce)


def common(request, lang):
    token = getattr(request.state, "token", None)
    return {"request": request, "lang": lang, "info": catalogue_info(),
            "user": current_user(request),
            "demo_mode": DEMO_MODE,
            "csrf": auth.csrf_token(token)}


# --------------------------------------------------------------------------
# Search, products, documents
# --------------------------------------------------------------------------

@app.get("/")
def home(request: Request,
         q: str = "", lang: str = "en", page: Integer = 1,
         series: str = "", etim: str = "", status: str = "", doc: str = "",
         catalog: str = "", brand: str = "", pmin: Number = None, pmax: Number = None,
         tab: str = "products"):
    page = max(1, page or 1)
    offset = (page - 1) * PAGE_SIZE

    products, total = search_products(q, lang, series, etim, pmin, pmax, status, doc,
                                      PAGE_SIZE, offset, catalog, brand)
    documents = search_docs(q) if q or tab == "documents" else []

    ctx = common(request, lang)
    ctx.update(
        q=q, tab=tab, page=page, page_size=PAGE_SIZE,
        products=products, total=total, documents=documents,
        series_list=series_list(), doc_types=doc_types(),
        f={"series": series, "etim": etim, "status": status, "doc": doc,
           "catalog": catalog, "brand": brand, "pmin": pmin, "pmax": pmax},
        brands=brands(), brand_names=brand_names(),
        last_page=max(1, -(-total // PAGE_SIZE)),
    )
    return templates.TemplateResponse(request, "search.html", ctx)


@app.get("/product/{pid}")
def product(request: Request, pid: str, lang: str = "en", brand: str = ""):
    # An article number is unique within a brand only; the same number may exist
    # under more than one brand.
    if brand:
        p = db().execute("SELECT * FROM product WHERE supplier_pid = ? AND brand = ?",
                         (pid, brand)).fetchone()
    else:
        matches = db().execute(
            "SELECT * FROM product WHERE supplier_pid = ? ORDER BY brand", (pid,)
        ).fetchall()
        p = matches[0] if matches else None
    if not p:
        raise HTTPException(404, f"Product not found: {pid}")
    i = p["id"]

    features = db().execute(
        "SELECT fname, fvalue, details, funit FROM product_feature "
        "WHERE product_id = ? ORDER BY fname, value_idx", (i,)).fetchall()

    files = db().execute("""
        SELECT m.code, COALESCE(mc.label, m.code) AS label,
               COALESCE(mc.kind,'other') AS kind,
               m.designation, m.filename, m.source, m.lang, m.issue_date
        FROM product_mime m LEFT JOIN mime_code mc ON mc.code = m.code
        WHERE m.product_id = ?
        ORDER BY mc.kind, mc.label, m.ord""", (i,)).fetchall()

    grouped = {}
    for d in files:
        grouped.setdefault(d["label"], []).append(d)

    def related(kind):
        return db().execute("""
            SELECT r.prod_id_to, r.descr,
                   p2.short_en, p2.short_de, p2.price_amount, p2.price_currency
            FROM product_ref r
            LEFT JOIN product p2 ON p2.supplier_pid = r.prod_id_to
            WHERE r.product_id = ? AND r.ref_type = ?
            ORDER BY r.prod_id_to LIMIT 200""", (i, kind)).fetchall()

    used_by = db().execute("""
        SELECT p2.supplier_pid, p2.short_en, p2.short_de
        FROM product_ref r JOIN product p2 ON p2.id = r.product_id
        WHERE r.prod_id_to = ? AND r.ref_type = 'accessories'
        ORDER BY p2.supplier_pid LIMIT 100""", (pid,)).fetchall()

    ctx = common(request, lang)
    ctx.update(
        p=p, features=features, file_groups=grouped,
        catalogues=product_catalogues(p["series_en"], p["brand"]),
        images=product_images(i),
        brand_names=brand_names(),
        accessories=related("accessories"), similar=related("similar"),
        used_by=used_by,
        packing=db().execute("SELECT * FROM packing_unit WHERE product_id = ? "
                             "ORDER BY CAST(qty_min AS REAL)", (i,)).fetchall(),
        characteristics=db().execute(
            "SELECT * FROM characteristic WHERE product_id = ?", (i,)).fetchall(),
        prices=db().execute("SELECT * FROM product_price WHERE product_id = ? "
                            "ORDER BY CAST(lower_bound AS REAL)", (i,)).fetchall(),
    )
    return templates.TemplateResponse(request, "product.html", ctx)


@app.get("/documents")
def documents(request: Request, q: str = "", lang: str = "en", brand: str = ""):
    ctx = common(request, lang)
    # The same PDF sitting in two directories is flagged in the list.
    duplicates = {r[0] for r in db().execute(
        "SELECT sha1 FROM doc GROUP BY sha1 HAVING count(*) > 1")}
    rows = [d for d in search_docs(q, limit=500) if not brand or d["brand"] == brand]
    ctx.update(q=q, documents=rows, duplicates=duplicates,
               doc_series=doc_series_map(), brand=brand,
               brands=brands(), brand_names=brand_names(),
               categories=db().execute(
                   "SELECT category, count(*) n FROM doc GROUP BY 1 ORDER BY n DESC"
               ).fetchall())
    return templates.TemplateResponse(request, "documents.html", ctx)


@app.get("/document/{doc_id}")
def document(doc_id: int, download: int = 0):
    d = db().execute("SELECT path, filename FROM doc WHERE id = ?",
                     (doc_id,)).fetchone()
    if not d:
        raise HTTPException(404, "Document not found")
    full = os.path.join(ROOT, d["path"])
    if not os.path.isfile(full):
        raise HTTPException(404, f"File missing on disk: {d['path']}")
    return FileResponse(
        full, media_type="application/pdf",
        filename=d["filename"] if download else None,
        headers=None if download else
        {"Content-Disposition": f'inline; filename="{d["filename"]}"'})


@app.get("/api/search")
def api_search(q: str = "", lang: str = "en", limit: Integer = 50,
               offset: Integer = 0, series: str = "", etim: str = "", status: str = "",
               doc: str = "", catalog: str = "", brand: str = "", pmin: Number = None,
               pmax: Number = None):
    limit = min(max(1, limit or 50), 500)
    offset = max(0, offset or 0)
    rows, total = search_products(q, lang, series, etim, pmin, pmax, status, doc,
                                  limit, offset, catalog, brand)
    return JSONResponse({
        "total": total, "limit": limit, "offset": offset,
        "results": [dict(r) for r in rows],
    })


@app.get("/export.csv")
def export_csv(q: str = "", lang: str = "en", series: str = "", etim: str = "",
               status: str = "", doc: str = "", catalog: str = "", brand: str = "",
               pmin: Number = None, pmax: Number = None):
    rows, _ = search_products(q, lang, series, etim, pmin, pmax, status, doc,
                              CSV_MAX, 0, catalog, brand)
    names = brand_names()
    buf = io.StringIO()
    buf.write("﻿")  # BOM so Excel reads UTF-8 correctly
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["Brand", "Article no", "EAN", "Alt no", "Title", "Series", "Type",
                "Price", "Currency", "ETIM class", "Status"])
    for r in rows:
        w.writerow([names.get(r["brand"], r["brand"]),
                    r["supplier_pid"], r["gtin"], r["alt_pid"], r["title"],
                    r["series"], r["type_name"], r["price_amount"],
                    r["price_currency"], r["etim_class"], r["status"]])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="catalogue_search.csv"'})


# --------------------------------------------------------------------------
# Login, logout, account
# --------------------------------------------------------------------------

@app.get("/login")
def login_page(request: Request, next: str = "/", error: str = ""):
    if auth.session_user(request.cookies.get(auth.COOKIE)):
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "request": request, "next": next, "error": error,
        "info": catalogue_info(), "setup": auth.admin_count() == 0,
        "demo_mode": DEMO_MODE, "demo_user": DEMO_USER,
        "demo_password": DEMO_PASSWORD})


@app.post("/login")
async def do_login(request: Request, username: str = Form(""),
                   password: str = Form(""), next: str = Form("/")):
    ip = request.client.host if request.client else ""
    u, error = await run_in_threadpool(
        auth.authenticate, username, password, ip,
        request.headers.get("user-agent", ""))
    if error:
        return templates.TemplateResponse(request, "login.html", {
            "request": request, "next": next, "error": error,
            "username": username, "info": catalogue_info(),
            "setup": auth.admin_count() == 0, "demo_mode": DEMO_MODE,
            "demo_user": DEMO_USER, "demo_password": DEMO_PASSWORD},
            status_code=401)

    token = await run_in_threadpool(auth.start_session, u["id"], ip,
                                    request.headers.get("user-agent", ""))
    if not next.startswith("/") or next.startswith("//"):
        next = "/"          # no open redirects
    response = RedirectResponse(next or "/", status_code=303)
    response.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400,
                        httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path="/")
    return response


@app.post("/logout")
def logout(request: Request):
    auth.end_session(request.cookies.get(auth.COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE, path="/")
    return response


@app.get("/account")
def account(request: Request, first: int = 0, message: str = "", error: str = ""):
    return templates.TemplateResponse(request, "account.html", {
        **common(request, "en"), "first": first, "message": message, "error": error})


@app.post("/account")
def account_save(request: Request, current: str = Form(""), new: str = Form(""),
                 repeat: str = Form(""), csrf: str = Form("")):
    u = current_user(request)
    if not auth.csrf_valid(getattr(request.state, "token", None), csrf):
        raise HTTPException(400, "Session check failed, please reload the page.")
    if DEMO_MODE:
        return _blocked_in_demo("/account")
    if not auth.verify_password(current, u["password_hash"]):
        return RedirectResponse("/account?error=Current+password+is+wrong.",
                                status_code=303)
    if new != repeat:
        return RedirectResponse("/account?error=The+new+passwords+do+not+match.",
                                status_code=303)
    try:
        # Our own session survives; the user's other devices are signed out.
        auth.change_password(u["id"], new,
                             keep_token=getattr(request.state, "token", None))
    except ValueError as e:
        return RedirectResponse(f"/account?error={quote(str(e))}", status_code=303)
    return RedirectResponse(
        "/account?message=" + quote("Password updated. Your sessions on other "
                                    "devices have been signed out."), status_code=303)


# --------------------------------------------------------------------------
# Administration (admins only)
# --------------------------------------------------------------------------

def update_status():
    """Reads the state file left behind by etl/fetch_trilux.py."""
    directory = os.environ.get("TRILUX_SECRET_DIR", "/etc/trilux")
    try:
        import json
        with open(os.path.join(directory, "state.json")) as fh:
            s = json.load(fh)
    except Exception:
        return {}
    # The session file's timestamp shows when the login was last refreshed.
    try:
        from datetime import datetime
        ts = os.path.getmtime(os.path.join(directory, "session.json"))
        s["session_date"] = datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    except OSError:
        s["session_date"] = None
    return s


@app.get("/admin")
def admin(request: Request, message: str = "", error: str = "",
          new_password: str = "", new_username: str = ""):
    require_admin(request)
    return templates.TemplateResponse(request, "admin.html", {
        **common(request, "en"), "users": auth.list_users(),
        "update": update_status(),
        "message": message, "error": error,
        "new_password": new_password, "new_username": new_username,
        "suggested": auth.generate_password()})


def _require_csrf(request, csrf):
    if not auth.csrf_valid(getattr(request.state, "token", None), csrf):
        raise HTTPException(400, "Session check failed, please reload the page.")


DEMO_REFUSAL = ("This is a public demo — accounts cannot be changed here. "
                "Everything else works as it would in a real installation.")


def _blocked_in_demo(target="/admin"):
    """Refuse a state-changing action on the public demo, politely."""
    return RedirectResponse(f"{target}?error={quote(DEMO_REFUSAL)}", status_code=303)


@app.post("/admin/add")
def admin_add(request: Request, username: str = Form(""), full_name: str = Form(""),
              email: str = Form(""), role: str = Form("user"),
              password: str = Form(""), csrf: str = Form("")):
    me = require_admin(request)
    _require_csrf(request, csrf)
    if DEMO_MODE:
        return _blocked_in_demo()
    password = password.strip() or auth.generate_password()
    try:
        auth.add_user(username, password, full_name, email, role,
                      created_by=me["username"])
    except ValueError as e:
        return RedirectResponse(f"/admin?error={quote(str(e))}", status_code=303)
    return RedirectResponse(
        f"/admin?message={quote('Account created. Pass these on;')}"
        f"&new_username={quote(username.strip().lower())}"
        f"&new_password={quote(password)}", status_code=303)


@app.post("/admin/{uid}/action")
def admin_action(request: Request, uid: int, action: str = Form(""),
                 role: str = Form(""), csrf: str = Form("")):
    me = require_admin(request)
    _require_csrf(request, csrf)
    if DEMO_MODE:
        return _blocked_in_demo()
    target = auth.get_user_by_id(uid)
    if not target:
        raise HTTPException(404, "User not found.")

    def is_last_admin():
        return target["role"] == "admin" and auth.admin_count() <= 1

    if action == "disable":
        if is_last_admin():
            return RedirectResponse(
                "/admin?error=You+cannot+disable+the+last+administrator.",
                status_code=303)
        auth.update_user(uid, active=0)
        m = f"{target['username']} has been disabled."
    elif action == "enable":
        auth.update_user(uid, active=1)
        m = f"{target['username']} has been re-enabled."
    elif action == "role":
        if role not in ("admin", "user"):
            raise HTTPException(400, "Invalid role.")
        if role == "user" and is_last_admin():
            return RedirectResponse(
                "/admin?error=You+cannot+demote+the+last+administrator.",
                status_code=303)
        auth.update_user(uid, role=role)
        m = f"{target['username']} is now '{role}'."
    elif action == "password":
        new = auth.generate_password()
        auth.change_password(uid, new, clear_flag=False)
        auth.update_user(uid, must_change=1)
        auth.end_all_sessions(uid)
        return RedirectResponse(
            f"/admin?message={quote('Password reset. Pass these on;')}"
            f"&new_username={quote(target['username'])}"
            f"&new_password={quote(new)}", status_code=303)
    elif action == "signout":
        auth.end_all_sessions(uid)
        m = f"{target['username']} has been signed out everywhere."
    elif action == "delete":
        if target["id"] == me["id"]:
            return RedirectResponse("/admin?error=You+cannot+delete+your+own+account.",
                                    status_code=303)
        if is_last_admin():
            return RedirectResponse(
                "/admin?error=You+cannot+delete+the+last+administrator.",
                status_code=303)
        auth.delete_user(uid)
        m = f"{target['username']} has been deleted."
    else:
        raise HTTPException(400, "Unknown action.")
    return RedirectResponse(f"/admin?message={quote(m)}", status_code=303)


@app.get("/health")
def health():
    n = db().execute("SELECT count(*) FROM product").fetchone()[0]
    d = db().execute("SELECT count(*) FROM doc").fetchone()[0]
    return {"status": "ok", "products": n, "documents": d, "db": DB_PATH}
