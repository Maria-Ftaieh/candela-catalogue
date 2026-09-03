#!/usr/bin/env python3
"""User accounts, sessions and authorisation.

Users live in a **separate database** (data/users.db). The main database
(catalogue.db) is rebuilt from scratch every month by etl/build_db.py, so accounts
could not live there — they would be wiped on every update.

Passwords are stored with scrypt from the standard library (no extra dependency).
Only the SHA-256 digest of a session token is stored, never the token itself, so
reading the database does not let anyone steal a session.
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER_DB = os.environ.get("USER_DB", os.path.join(ROOT, "data", "users.db"))

COOKIE = "session"
SESSION_DAYS = 30          # session lifetime
LOCK_ATTEMPTS = 5          # failures against one username before it locks
LOCK_MINUTES = 15          # how long the lock lasts
IP_ATTEMPTS = 30           # failures from one IP (password spraying)

# OWASP Password Storage recommendation: scrypt n=2^17, r=8, p=1 (~134 MB, ~300 ms).
# Older records keep verifying with their own parameters and are silently upgraded
# on the next successful login (see authenticate).
SCRYPT = dict(n=2**17, r=8, p=1)

_local = threading.local()


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS user (
  id             INTEGER PRIMARY KEY,
  username       TEXT NOT NULL UNIQUE COLLATE NOCASE,
  full_name      TEXT,
  email          TEXT,
  password_hash  TEXT NOT NULL,
  role           TEXT NOT NULL DEFAULT 'user',      -- 'admin' | 'user'
  active         INTEGER NOT NULL DEFAULT 1,
  must_change    INTEGER NOT NULL DEFAULT 0,        -- force a new password on first login
  created_at     TEXT,
  created_by     TEXT,
  last_login     TEXT,
  login_count    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS session (
  token_hash  TEXT PRIMARY KEY,      -- SHA-256(token); the token itself is never stored
  user_id     INTEGER NOT NULL,
  created_at  TEXT, last_seen TEXT, expires_at TEXT,
  ip TEXT, user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_user ON session(user_id);
CREATE TABLE IF NOT EXISTS login_attempt (
  username TEXT, ip TEXT, at TEXT, success INTEGER
);
CREATE INDEX IF NOT EXISTS idx_attempt ON login_attempt(username, at);
CREATE INDEX IF NOT EXISTS idx_attempt_ip ON login_attempt(ip, at);
"""


def db():
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(USER_DB), exist_ok=True)
        fresh = not os.path.exists(USER_DB)
        conn = sqlite3.connect(USER_DB, check_same_thread=False)
        if fresh:
            os.chmod(USER_DB, 0o600)   # it holds password hashes
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.commit()
        _local.conn = conn
    return conn


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.isoformat()


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------

def _maxmem(n, r, p):
    """OpenSSL's memory ceiling; without it n=2^17 fails at the default 32 MB."""
    return 128 * n * r * p + (1 << 20)


def hash_password(password):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, dklen=32,
                       maxmem=_maxmem(**SCRYPT), **SCRYPT)
    return "scrypt${n}${r}${p}${s}${h}".format(
        s=base64.b64encode(salt).decode(), h=base64.b64encode(h).decode(), **SCRYPT)


def verify_password(password, record):
    """Verifies using the parameters stored in the record, so old rows still work."""
    try:
        alg, n, r, p, salt, digest = record.split("$")
        if alg != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        expected = base64.b64decode(digest)
        h = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                           n=n, r=r, p=p, dklen=len(expected),
                           maxmem=_maxmem(n, r, p))
        return hmac.compare_digest(h, expected)
    except Exception:
        return False


def hash_is_current(record):
    """Was this record produced with today's cost parameters?"""
    try:
        alg, n, r, p, _, _ = record.split("$")
        return (alg == "scrypt" and int(n) >= SCRYPT["n"]
                and int(r) >= SCRYPT["r"] and int(p) >= SCRYPT["p"])
    except Exception:
        return False


def password_problem(password):
    """Simple but useful rules. Returns an error message, or None when fine."""
    if len(password or "") < 10:
        return "The password must be at least 10 characters."
    if password.lower() in {"password12", "password1234", "1234567890"}:
        return "That password is too easy to guess."
    return None


def generate_password(words=3):
    """Readable, easy to type, still strong."""
    wordlist = ["lantern", "luminaire", "lumen", "kelvin", "photometry", "mounting",
                "catalogue", "certificate", "optic", "reflector", "ballast", "sensor",
                "daylight", "prism", "ceiling", "diffuser", "circuit", "cable"]
    return "-".join(secrets.choice(wordlist) for _ in range(words)) \
        + "-" + str(secrets.randbelow(9000) + 1000)


USERNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{1,30})[a-z0-9]$")


def username_valid(name):
    return bool(USERNAME_RE.match((name or "").lower()))


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------

def get_user(username):
    return db().execute("SELECT * FROM user WHERE username = ?",
                        (username,)).fetchone()


def get_user_by_id(uid):
    return db().execute("SELECT * FROM user WHERE id = ?", (uid,)).fetchone()


def list_users():
    return db().execute("""
        SELECT u.*, (SELECT count(*) FROM session s
                     WHERE s.user_id = u.id AND s.expires_at > ?) AS open_sessions
        FROM user u ORDER BY u.role DESC, u.username""",
        (iso(utcnow()),)).fetchall()


def add_user(username, password, full_name="", email="", role="user",
             created_by="", must_change=1):
    username = (username or "").strip().lower()
    if not username_valid(username):
        raise ValueError("The username must be 3-32 characters, start and end with a "
                         "letter or digit, and may contain . _ - in between.")
    if get_user(username):
        raise ValueError(f"The username '{username}' already exists.")
    problem = password_problem(password)
    if problem:
        raise ValueError(problem)
    if role not in ("admin", "user"):
        raise ValueError("Invalid role.")
    conn = db()
    cur = conn.execute("""INSERT INTO user
        (username, full_name, email, password_hash, role, created_at, created_by,
         must_change) VALUES (?,?,?,?,?,?,?,?)""",
        (username, full_name.strip(), email.strip(), hash_password(password), role,
         iso(utcnow()), created_by, 1 if must_change else 0))
    conn.commit()
    return cur.lastrowid


def change_password(uid, new_password, clear_flag=True, keep_token=None):
    """Changes the password and **ends every other session**.

    People change their password mainly when they suspect it was compromised, so the
    change must also drop the attacker's open session. The caller's own session is
    kept alive via `keep_token`; without it, that one ends too.
    """
    problem = password_problem(new_password)
    if problem:
        raise ValueError(problem)
    conn = db()
    conn.execute("UPDATE user SET password_hash = ?, must_change = ? WHERE id = ?",
                 (hash_password(new_password), 0 if clear_flag else 1, uid))
    if keep_token:
        conn.execute("DELETE FROM session WHERE user_id = ? AND token_hash <> ?",
                     (uid, _digest(keep_token)))
    else:
        conn.execute("DELETE FROM session WHERE user_id = ?", (uid,))
    conn.commit()


def update_user(uid, **fields):
    allowed = {"full_name", "email", "role", "active", "must_change"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    conn = db()
    conn.execute(f"UPDATE user SET {','.join(k+'=?' for k in fields)} WHERE id = ?",
                 (*fields.values(), uid))
    if fields.get("active") == 0:
        conn.execute("DELETE FROM session WHERE user_id = ?", (uid,))  # log out at once
    conn.commit()


def delete_user(uid):
    conn = db()
    conn.execute("DELETE FROM session WHERE user_id = ?", (uid,))
    conn.execute("DELETE FROM user WHERE id = ?", (uid,))
    conn.commit()


def admin_count():
    return db().execute(
        "SELECT count(*) FROM user WHERE role='admin' AND active=1").fetchone()[0]


# --------------------------------------------------------------------------
# Brute force protection
# --------------------------------------------------------------------------

def is_locked(username):
    """(locked, minutes_remaining)"""
    since = iso(utcnow() - timedelta(minutes=LOCK_MINUTES))
    rows = db().execute("""
        SELECT at, success FROM login_attempt
        WHERE username = ? AND at > ? ORDER BY at DESC""",
        (username, since)).fetchall()
    failures = 0
    for r in rows:
        if r["success"]:
            break              # only attempts since the last success count
        failures += 1
    if failures < LOCK_ATTEMPTS:
        return False, 0
    latest = datetime.fromisoformat(rows[0]["at"])
    left = LOCK_MINUTES - int((utcnow() - latest).total_seconds() // 60)
    return True, max(1, left)


def ip_locked(ip):
    """Many failures from one IP means password spraying.

    A per-username lock alone is not enough: an attacker can try one password
    against fifty usernames and never trip it.
    """
    if not ip:
        return False, 0
    since = iso(utcnow() - timedelta(minutes=LOCK_MINUTES))
    rows = db().execute("""
        SELECT at FROM login_attempt
        WHERE ip = ? AND success = 0 AND at > ? ORDER BY at DESC""",
        (ip, since)).fetchall()
    if len(rows) < IP_ATTEMPTS:
        return False, 0
    latest = datetime.fromisoformat(rows[0]["at"])
    return True, max(1, LOCK_MINUTES - int((utcnow() - latest).total_seconds() // 60))


def record_attempt(username, ip, success):
    conn = db()
    conn.execute("INSERT INTO login_attempt VALUES (?,?,?,?)",
                 (username, ip, iso(utcnow()), 1 if success else 0))
    conn.execute("DELETE FROM login_attempt WHERE at < ?",
                 (iso(utcnow() - timedelta(days=7)),))
    conn.commit()


def clear_locks(ip=None, username=None):
    """Deletes failed-attempt records, i.e. lifts a lock.

    An office behind a single NAT address can lock itself out collectively; this
    opens it again without waiting.
    """
    conn = db()
    if ip:
        n = conn.execute("DELETE FROM login_attempt WHERE ip = ?", (ip,)).rowcount
    elif username:
        n = conn.execute("DELETE FROM login_attempt WHERE username = ?",
                         (username.lower(),)).rowcount
    else:
        n = conn.execute("DELETE FROM login_attempt").rowcount
    conn.commit()
    return n


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(uid, ip="", user_agent=""):
    token = secrets.token_urlsafe(32)
    conn = db()
    conn.execute("""INSERT INTO session
        (token_hash, user_id, created_at, last_seen, expires_at, ip, user_agent)
        VALUES (?,?,?,?,?,?,?)""",
        (_digest(token), uid, iso(utcnow()), iso(utcnow()),
         iso(utcnow() + timedelta(days=SESSION_DAYS)), ip, (user_agent or "")[:200]))
    conn.execute("UPDATE user SET last_login = ?, login_count = login_count + 1 "
                 "WHERE id = ?", (iso(utcnow()), uid))
    conn.execute("DELETE FROM session WHERE expires_at < ?", (iso(utcnow()),))
    conn.commit()
    return token


def session_user(token):
    if not token:
        return None
    conn = db()
    s = conn.execute("SELECT * FROM session WHERE token_hash = ? AND expires_at > ?",
                     (_digest(token), iso(utcnow()))).fetchone()
    if not s:
        return None
    u = get_user_by_id(s["user_id"])
    if not u or not u["active"]:
        return None
    conn.execute("UPDATE session SET last_seen = ? WHERE token_hash = ?",
                 (iso(utcnow()), s["token_hash"]))
    conn.commit()
    return u


def end_session(token):
    if token:
        conn = db()
        conn.execute("DELETE FROM session WHERE token_hash = ?", (_digest(token),))
        conn.commit()


def end_all_sessions(uid):
    conn = db()
    conn.execute("DELETE FROM session WHERE user_id = ?", (uid,))
    conn.commit()


def authenticate(username, password, ip="", user_agent=""):
    """Returns (user, error_message). The messages never leak whether a user exists."""
    username = (username or "").strip().lower()
    generic = "Incorrect username or password."
    if not username or not password:
        return None, generic

    # Limits are checked BEFORE the expensive scrypt call: this stops brute force and
    # also stops the hashing cost from becoming a denial-of-service lever.
    locked, left = is_locked(username)
    if locked:
        return None, f"Too many failed attempts. Try again in {left} minutes."
    ip_lock, ip_left = ip_locked(ip)
    if ip_lock:
        record_attempt(username, ip, False)
        return None, ("Too many failed attempts from this address. "
                      f"Try again in {ip_left} minutes.")

    u = get_user(username)
    # Hash even when the user does not exist, so response time reveals nothing.
    record = u["password_hash"] if u else hash_password("dummy-password")
    correct = verify_password(password, record)

    if not u or not correct:
        record_attempt(username, ip, False)
        return None, generic
    if not u["active"]:
        record_attempt(username, ip, False)
        return None, "This account has been disabled. Please contact an administrator."
    record_attempt(username, ip, True)

    # Upgrade a hash made with older, cheaper parameters during this login.
    if not hash_is_current(u["password_hash"]):
        conn = db()
        conn.execute("UPDATE user SET password_hash = ? WHERE id = ?",
                     (hash_password(password), u["id"]))
        conn.commit()
        u = get_user_by_id(u["id"])
    return u, None


# --------------------------------------------------------------------------
# CSRF
# --------------------------------------------------------------------------

def csrf_token(session_token):
    """Bound to the session. The cookie is SameSite=Lax; this is the second layer."""
    return hmac.new(_secret_key(), (session_token or "").encode(),
                    hashlib.sha256).hexdigest()


def csrf_valid(session_token, given):
    return bool(given) and hmac.compare_digest(csrf_token(session_token), given)


def _secret_key():
    """A persistent per-server key, generated on first use."""
    path = os.path.join(os.path.dirname(USER_DB), ".secret_key")
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600), "wb") as fh:
            fh.write(secrets.token_bytes(32))
    with open(path, "rb") as fh:
        return fh.read()
