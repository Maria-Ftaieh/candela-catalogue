#!/usr/bin/env python3
"""Downloads the current BMEcat package from the TRILUX portal.

The download page is behind SAML SSO (the identity provider is Salesforce) and the
login form is drawn entirely in JavaScript, so a real browser (Playwright/Chromium)
is required — curl or requests cannot log in.

The session is opened once and stored in /etc/trilux/session.json, then reused, so
subsequent runs normally do not log in at all.

SAFETY RULE: the TRILUX account locks after 5 failed attempts. This script makes AT
MOST ONE login attempt per run and stops itself after 2 consecutive failures; it
will not try again until `--reset` is passed. It therefore cannot lock the account.

    python3 etl/fetch_trilux.py --check    # is there a new version? (no download)
    python3 etl/fetch_trilux.py            # download and install a new version
    python3 etl/fetch_trilux.py --rebuild  # download + rebuild the database
    python3 etl/fetch_trilux.py --reset    # clear the login lock
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET_DIR = os.environ.get("TRILUX_SECRET_DIR", "/etc/trilux")
ENV_FILE = os.path.join(SECRET_DIR, "trilux.env")
SESSION_FILE = os.path.join(SECRET_DIR, "session.json")
STATE_FILE = os.path.join(SECRET_DIR, "state.json")

# The package is placed into this brand directory (see brands/README.md)
BRAND = "trilux"
DATA_DIR = os.path.join(ROOT, "brands", BRAND, "data")

PAGE = "https://www.trilux.com/en/service/downloads/product-data/gross-prices/"
BASE = "https://www.trilux.com"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0.0.0 Safari/537.36")

MAX_CONSECUTIVE_FAILURES = 2   # well below the portal's limit of 5
LINK_PATTERN = re.compile(r"(bmecat|etim)", re.I)


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {"consecutive_failures": 0}


def write_state(**fields):
    s = read_state()
    s.update(fields)
    os.makedirs(SECRET_DIR, exist_ok=True)
    with open(os.open(STATE_FILE, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600),
              "w") as fh:
        json.dump(s, fh, indent=2, ensure_ascii=False)
    return s


def read_credentials():
    if not os.path.exists(ENV_FILE):
        sys.exit(f"Credentials file missing: {ENV_FILE}\n"
                 "It must contain TRILUX_USER and TRILUX_PASS (chmod 600).")
    env = {}
    with open(ENV_FILE) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    if not env.get("TRILUX_USER") or not env.get("TRILUX_PASS"):
        sys.exit(f"TRILUX_USER / TRILUX_PASS missing from {ENV_FILE}.")
    return env["TRILUX_USER"], env["TRILUX_PASS"]


def installed_versions():
    """BMEcat directories currently under brands/trilux/data/."""
    if not os.path.isdir(DATA_DIR):
        return set()
    return {d for d in os.listdir(DATA_DIR)
            if os.path.isdir(os.path.join(DATA_DIR, d)) and "BMECAT" in d.upper()}


def find_link(allow_login=True):
    """Returns (download_url, title, logged_in, cookies)."""
    from playwright.sync_api import sync_playwright

    have_session = os.path.exists(SESSION_FILE)
    logged_in = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            storage_state=SESSION_FILE if have_session else None,
            locale="de-DE", user_agent=UA, accept_downloads=True,
            viewport={"width": 1400, "height": 900})
        pg = ctx.new_page()
        pg.goto(PAGE, wait_until="networkidle", timeout=90000)

        if "signin" in pg.url:
            if not allow_login:
                browser.close()
                sys.exit("Session expired and logging in is disabled (--check).")

            st = read_state()
            if st.get("consecutive_failures", 0) >= MAX_CONSECUTIVE_FAILURES:
                browser.close()
                sys.exit(
                    f"STOPPED: {st['consecutive_failures']} consecutive login "
                    "failures.\nThe TRILUX account locks after 5 attempts, so "
                    "automatic retries are disabled.\nVerify the password by hand, "
                    "then run:\n  python3 etl/fetch_trilux.py --reset")

            user, password = read_credentials()
            print("No/expired session — making a single login attempt...")
            write_state(last_login_attempt=now())
            try:
                pg.fill("#email", user)
                pg.fill("#password", password)
                with pg.expect_navigation(wait_until="networkidle", timeout=90000):
                    pg.click("button[type=submit]")
                pg.wait_for_timeout(3000)
            except Exception as exc:
                write_state(
                    consecutive_failures=read_state().get("consecutive_failures", 0) + 1,
                    last_error=f"during login: {exc}")
                browser.close()
                raise SystemExit(f"Error during login: {exc}")

            if "signin" in pg.url:
                n = read_state().get("consecutive_failures", 0) + 1
                shot = os.path.join(tempfile.gettempdir(), "trilux_login_failed.png")
                try:
                    pg.screenshot(path=shot)
                except Exception:
                    shot = "(no screenshot)"
                write_state(consecutive_failures=n, last_error="login rejected")
                browser.close()
                raise SystemExit(
                    f"LOGIN FAILED ({n}/{MAX_CONSECUTIVE_FAILURES}). Screenshot: {shot}\n"
                    "The password may have changed, or the account may be asking for "
                    "an extra verification step (MFA).")

            logged_in = True
            ctx.storage_state(path=SESSION_FILE)
            os.chmod(SESSION_FILE, 0o600)
            write_state(consecutive_failures=0, last_successful_login=now(),
                        last_error=None)
            print("Login succeeded, session saved.")

        url = title = None
        for a in pg.query_selector_all("a[href*='fileadmin']"):
            h = a.get_attribute("href") or ""
            if h.lower().endswith(".zip") and LINK_PATTERN.search(h):
                url = h if h.startswith("http") else BASE + h
                title = re.sub(r"\s+", " ", (a.inner_text() or "")).strip()
                break

        cookies = ctx.cookies()
        browser.close()

    if not url:
        raise SystemExit("Download link not found on the page. "
                         "TRILUX may have changed the page layout.")
    return url, title, logged_in, cookies


def download(url, cookies, target):
    """Streams the large file to disk instead of loading it into memory."""
    import requests

    s = requests.Session()
    s.headers["User-Agent"] = UA
    for c in cookies:
        s.cookies.set(c["name"], c["value"], domain=c.get("domain"),
                      path=c.get("path", "/"))
    r = s.get(url, stream=True, timeout=120)
    r.raise_for_status()
    total = int(r.headers.get("content-length", 0))
    got = 0
    t0 = time.time()
    with open(target, "wb") as fh:
        for chunk in r.iter_content(1 << 20):
            fh.write(chunk)
            got += len(chunk)
            if total and got % (20 << 20) < (1 << 20):
                speed = got / max(.1, time.time() - t0) / 1e6
                print(f"  {got/1e6:7.0f} / {total/1e6:.0f} MB  ({speed:.1f} MB/s)",
                      flush=True)
    print(f"  downloaded: {got/1e6:.0f} MB in {time.time()-t0:.0f} s")
    return got


def install(zip_path, folder_name):
    """Extracts to a temporary directory, verifies it, then swaps it into place."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="trilux_", dir=DATA_DIR)
    try:
        with zipfile.ZipFile(zip_path) as z:
            # Zip-slip guard
            for name in z.namelist():
                dest = os.path.realpath(os.path.join(tmp, name))
                if not dest.startswith(os.path.realpath(tmp) + os.sep):
                    raise SystemExit(f"Unsafe zip entry, aborting: {name}")
            z.extractall(tmp)

        xmls = [os.path.join(d, f) for d, _dirs, files in os.walk(tmp)
                for f in files if f.lower().endswith(".xml")]
        if not xmls:
            raise SystemExit("No XML inside the zip — the download may be corrupt.")
        largest = max(xmls, key=os.path.getsize)
        print(f"  package verified: {os.path.basename(largest)} "
              f"({os.path.getsize(largest)/1e9:.2f} GB)")

        entries = os.listdir(tmp)
        source = (os.path.join(tmp, entries[0])
                  if len(entries) == 1 and os.path.isdir(os.path.join(tmp, entries[0]))
                  else tmp)

        target = os.path.join(DATA_DIR, folder_name)
        backup = target + ".old"
        if os.path.exists(target):
            if os.path.exists(backup):
                shutil.rmtree(backup)
            os.rename(target, backup)
        shutil.move(source, target)
        if os.path.exists(backup):
            shutil.rmtree(backup)
            print("  previous version removed")
        print(f"  installed: {target}")
        return target
    finally:
        if os.path.exists(tmp):
            shutil.rmtree(tmp, ignore_errors=True)


def pipeline():
    for script in ("build_db.py", "index_docs.py", "link_catalogs.py"):
        print(f"\n--- {script} ---", flush=True)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "etl", script)])
        if r.returncode:
            raise SystemExit(f"{script} failed ({r.returncode}).")


def main():
    ap = argparse.ArgumentParser(description="Download the TRILUX BMEcat package")
    ap.add_argument("--check", action="store_true",
                    help="only report whether a new version exists")
    ap.add_argument("--force", action="store_true", help="download even if unchanged")
    ap.add_argument("--rebuild", action="store_true",
                    help="rebuild the database after downloading")
    ap.add_argument("--reset", action="store_true", help="clear the login lock")
    args = ap.parse_args()

    if args.reset:
        write_state(consecutive_failures=0, last_error=None)
        print("Login lock cleared.")
        return

    url, title, logged_in, cookies = find_link(allow_login=not args.check)
    filename = url.rsplit("/", 1)[-1]
    folder = filename[:-4] if filename.lower().endswith(".zip") else filename
    installed = installed_versions()
    is_new = folder not in installed

    print(f"\nPackage on portal : {filename}")
    print(f"Title             : {(title or '')[:80]}")
    print(f"Installed         : {', '.join(sorted(installed)) or '(none)'}")
    print(f"Status            : {'NEW VERSION AVAILABLE' if is_new else 'up to date'}")

    write_state(last_check=now(), portal_file=filename, portal_url=url,
                new_available=is_new, logged_in=logged_in)

    if args.check:
        return
    if not is_new and not args.force:
        print("\nNothing to do — the installed version matches the portal.")
        if args.rebuild:
            print("(The data did not change, so the database was not rebuilt. "
                  "Use --force --rebuild to override.)")
        return

    print(f"\nDownloading: {url}")
    os.makedirs(DATA_DIR, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".zip", dir=DATA_DIR, delete=False) as tf:
        tmp_zip = tf.name
    try:
        size = download(url, cookies, tmp_zip)
        install(tmp_zip, folder)
        write_state(last_download=now(), last_downloaded=filename, last_size=size)
    finally:
        if os.path.exists(tmp_zip):
            os.remove(tmp_zip)

    if args.rebuild:
        pipeline()
        write_state(last_rebuild=now())
        print("\nDatabase updated.")
    else:
        print("\nFiles are in place. To rebuild the database:")
        print("  .venv/bin/python etl/fetch_trilux.py --rebuild")


if __name__ == "__main__":
    main()
