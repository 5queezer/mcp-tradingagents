"""Redis-backed job queue helpers for the TradingAgents MCP service."""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Optional

import redis

JOB_PREFIX = "tradingagents:job:"
QUEUE_KEY = "tradingagents:queue"
JOB_TTL_SECONDS = 30 * 24 * 3600


_client: Optional[redis.Redis] = None


def get_client(url: str) -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(url, decode_responses=True)
    return _client


def new_job_id() -> str:
    return uuid.uuid4().hex


def create_job(client: redis.Redis, job_id: str, payload: dict[str, Any]) -> None:
    key = JOB_PREFIX + job_id
    client.hset(key, mapping={
        "id": job_id,
        "state": "queued",
        "created_at": str(int(time.time())),
        "payload": json.dumps(payload),
    })
    client.expire(key, JOB_TTL_SECONDS)


def get_job(client: redis.Redis, job_id: str) -> Optional[dict[str, str]]:
    data = client.hgetall(JOB_PREFIX + job_id)
    return data or None


def update_job(client: redis.Redis, job_id: str, **fields: Any) -> None:
    key = JOB_PREFIX + job_id
    mapping = {k: v if isinstance(v, str) else str(v) for k, v in fields.items()}
    client.hset(key, mapping=mapping)


def list_jobs(client: redis.Redis, limit: int = 20) -> list[dict[str, str]]:
    jobs: list[dict[str, str]] = []
    for key in client.scan_iter(match=JOB_PREFIX + "*", count=200):
        data = client.hgetall(key)
        if data:
            jobs.append(data)
    jobs.sort(key=lambda j: int(j.get("created_at", "0")), reverse=True)
    return jobs[:limit]
