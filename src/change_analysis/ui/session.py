"""Session helpers for the web layer.

The browser holds only an opaque session token.  Everything else — who the user
is, which runs exist, what a page may show — is read from the database on every
page load, so a refresh (or a stolen tab) never inherits another user's state.
"""

from __future__ import annotations

from typing import Any

from nicegui import app, ui

from ..auth import service as auth

TOKEN_KEY = "session_token"


def current_user() -> dict[str, Any] | None:
    token = app.storage.user.get(TOKEN_KEY)
    if not token:
        return None
    user = auth.user_for_session(token)
    if user is None:
        app.storage.user.pop(TOKEN_KEY, None)
    return user


def login(user_id: str) -> None:
    token = auth.create_session(user_id)
    app.storage.user[TOKEN_KEY] = token


def logout() -> None:
    token = app.storage.user.pop(TOKEN_KEY, None)
    auth.drop_session(token)


def require_user() -> dict[str, Any] | None:
    user = current_user()
    if user is None:
        ui.navigate.to("/login")
        return None
    return user


def user_id(user: dict[str, Any]) -> str:
    return str(user["id"])
