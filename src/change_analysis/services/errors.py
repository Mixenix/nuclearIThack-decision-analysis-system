"""Errors shared by the whole service layer."""

from __future__ import annotations


class ServiceError(RuntimeError):
    """Base class for user-visible service failures."""


class AccessDenied(ServiceError):
    """The object exists but belongs to somebody else (or does not exist)."""

    def __init__(self, what: str = "object") -> None:
        super().__init__(f"access denied: {what}")


class NotFound(ServiceError):
    """The object does not exist."""


class InvalidRequest(ServiceError):
    """The caller asked for something the contract does not allow."""
