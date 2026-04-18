"""
Redis-backed TokenStore + ClientStore (drop-in replacements).

State survives Cloud Run cold starts, scale-to-zero, and multi-instance fan-out.
Uses the standard Redis protocol (any TLS URL works — Upstash, Redis Cloud,
self-hosted). Values are JSON-encoded so the hash fields stay human-readable
for debugging via `redis-cli`.

Swap in via:

    from mcp_server.redis_stores import RedisTokenStore, RedisClientStore
    app = create_app(
        mcp=mcp,
        token_store=RedisTokenStore(os.environ["REDIS_URL"]),
        client_store=RedisClientStore(os.environ["REDIS_URL"]),
    )
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict
from typing import Optional

import redis

from .auth import AccessToken, AuthCode, OAuthClient, _validate_redirect_uri

_CODE_PREFIX = "mcp-oauth:code:"
_TOKEN_PREFIX = "mcp-oauth:token:"
_CLIENT_PREFIX = "mcp-oauth:client:"

_CODE_TTL = 300      # 5 min — matches in-memory AuthCode.expires default
_TOKEN_TTL = 3600    # 1 h
_CLIENT_TTL = 90 * 24 * 3600   # 90 days — tokens get refreshed sooner


class RedisTokenStore:
    """Redis-backed auth codes + access tokens."""

    def __init__(self, url: str):
        self._r = redis.Redis.from_url(url, decode_responses=True)

    # -- Auth codes --

    def create_code(self, challenge: str, redirect_uri: str, state: str, sub: str) -> str:
        code = secrets.token_urlsafe(32)
        payload = AuthCode(
            challenge=challenge,
            redirect_uri=redirect_uri,
            state=state,
            sub=sub,
        )
        self._r.setex(_CODE_PREFIX + code, _CODE_TTL, json.dumps(asdict(payload)))
        return code

    def consume_code(self, code: str) -> Optional[AuthCode]:
        key = _CODE_PREFIX + code
        raw = self._r.get(key)
        if not raw:
            return None
        self._r.delete(key)
        data = json.loads(raw)
        entry = AuthCode(**data)
        if time.time() < entry.expires:
            return entry
        return None

    # -- Access tokens --

    def create_token(self, sub: str) -> str:
        token = secrets.token_urlsafe(48)
        payload = AccessToken(sub=sub)
        self._r.setex(
            _TOKEN_PREFIX + token,
            _TOKEN_TTL,
            json.dumps(asdict(payload)),
        )
        return token

    def validate_token(self, token: str) -> Optional[AccessToken]:
        raw = self._r.get(_TOKEN_PREFIX + token)
        if not raw:
            return None
        data = json.loads(raw)
        entry = AccessToken(**data)
        if time.time() < entry.expires:
            return entry
        # Expired — Redis TTL already dropped it, just return None.
        return None

    def revoke_token(self, token: str) -> None:
        self._r.delete(_TOKEN_PREFIX + token)


class RedisClientStore:
    """Redis-backed OAuth client registry (RFC 7591)."""

    def __init__(self, url: str):
        self._r = redis.Redis.from_url(url, decode_responses=True)

    def register(self, redirect_uris: list[str], client_name: str = "") -> OAuthClient:
        for uri in redirect_uris:
            if not _validate_redirect_uri(uri):
                raise ValueError(
                    f"Invalid redirect_uri: {uri!r} — must be https (or http for localhost)"
                )
        client_id = secrets.token_urlsafe(16)
        client = OAuthClient(
            client_id=client_id,
            redirect_uris=redirect_uris,
            client_name=client_name,
        )
        self._r.setex(
            _CLIENT_PREFIX + client_id,
            _CLIENT_TTL,
            json.dumps(asdict(client)),
        )
        return client

    def get(self, client_id: str) -> Optional[OAuthClient]:
        raw = self._r.get(_CLIENT_PREFIX + client_id)
        if not raw:
            return None
        return OAuthClient(**json.loads(raw))
