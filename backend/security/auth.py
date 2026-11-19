"""Operator authentication.

* Requests from this PC (loopback) are the operator - always allowed.
* Other machines (a laptop / tablet on the LAN) need FESTCAST_ADMIN_PASSWORD; with no password set, remote
  administration is disabled entirely.
* Phones and TVs only ever reach the token-gated public routes (/join, /tv, /api/public).
* OBS-side pages (/obs/*) require loopback + the local key."""
from __future__ import annotations

import hmac
import secrets
import time

from fastapi import HTTPException, Request

LOOPBACK = {"127.0.0.1", "::1", "localhost"}
SESSION_COOKIE = "fc_session"
SESSION_TTL = 7 * 24 * 3600


class Auth:
    def __init__(self, password: str) -> None:
        self.password = password
        self.sessions: dict[str, float] = {}
        self.failures: dict[str, list[float]] = {}

    @staticmethod
    def is_local(request: Request) -> bool:
        return bool(request.client and request.client.host in LOOPBACK)

    def is_admin(self, request: Request) -> bool:
        if self.is_local(request):
            return True
        tok = request.cookies.get(SESSION_COOKIE)
        exp = self.sessions.get(tok or "")
        return bool(exp and exp > time.time())

    def login(self, request: Request, password: str) -> str:
        ip = request.client.host if request.client else "?"
        recent = [t for t in self.failures.get(ip, []) if time.time() - t < 300]
        if len(recent) >= 8:
            raise HTTPException(429, "Too many attempts, wait 5 minutes")
        if not self.password:
            raise HTTPException(403, "Remote access is disabled (set FESTCAST_ADMIN_PASSWORD on the production PC)")
        if not hmac.compare_digest(password.encode(), self.password.encode()):
            self.failures[ip] = recent + [time.time()]
            raise HTTPException(401, "Wrong password")
        tok = secrets.token_urlsafe(32)
        self.sessions[tok] = time.time() + SESSION_TTL
        return tok

    def logout(self, request: Request) -> None:
        self.sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
