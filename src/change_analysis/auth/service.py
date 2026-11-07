"""Local accounts and server-side sessions.

Argon2 for hashes (never hand-rolled crypto), opaque random session tokens in
``ca.sessions``.  Authorisation decisions are *not* made here — the service layer
checks ownership on every object.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from ..db.connection import query, tx

_hasher = PasswordHasher()
SESSION_TTL = timedelta(hours=24)


def create_user(login: str, password: str, display_name: str = "") -> str:
    login = login.strip()
    if not login:
        raise ValueError("login must not be empty")
    with tx() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ca.users (login, password_hash, display_name)"
            " VALUES (%s, %s, %s) RETURNING id",
            (login, _hasher.hash(password), display_name or login),
        )
        return str(cur.fetchone()["id"])


def ensure_user(login: str, password: str, display_name: str = "") -> str:
    existing = query("SELECT id FROM ca.users WHERE login = %s", (login,))
    if existing:
        return str(existing[0]["id"])
    return create_user(login, password, display_name)


def set_password(login: str, password: str) -> bool:
    with tx() as conn, conn.cursor() as cur:
        cur.execute("UPDATE ca.users SET password_hash = %s WHERE login = %s",
                    (_hasher.hash(password), login))
        return cur.rowcount > 0


def authenticate(login: str, password: str) -> str | None:
    rows = query("SELECT id, password_hash FROM ca.users WHERE login = %s", (login.strip(),))
    if not rows:
        return None
    try:
        _hasher.verify(rows[0]["password_hash"], password)
    except VerifyMismatchError:
        return None
    except Exception:  # noqa: BLE001 - malformed hash must not crash the page
        return None
    return str(rows[0]["id"])


def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + SESSION_TTL
    with tx() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ca.sessions (id, user_id, expires_at) VALUES (%s, %s, %s)",
            (token, user_id, expires),
        )
    return token


def user_for_session(token: str | None) -> dict | None:
    if not token:
        return None
    rows = query(
        "SELECT u.id, u.login, u.display_name FROM ca.sessions s"
        " JOIN ca.users u ON u.id = s.user_id"
        " WHERE s.id = %s AND s.expires_at > now()",
        (token,),
    )
    if not rows:
        return None
    return {"id": str(rows[0]["id"]), "login": rows[0]["login"],
            "display_name": rows[0]["display_name"]}


def drop_session(token: str | None) -> None:
    if token:
        with tx() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM ca.sessions WHERE id = %s", (token,))


def purge_expired() -> int:
    with tx() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM ca.sessions WHERE expires_at <= now()")
        return cur.rowcount
