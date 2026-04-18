"""
TradingAgents MCP Server — Cloud Run ready.

Self-contained OAuth 2.1 PKCE via mcp-oauth-template, plus an async job queue
backed by Redis so analyses that take minutes don't block the MCP response.

Architecture:

    claude.ai -------[OAuth]-----> /authorize, /token
                 \
                  -[MCP call]----> /mcp
                                     |
    start_analysis(ticker, date)  --+--> Redis (job=queued)
                                     |
                                     +--> asyncio.create_task(run_job)        (dev)
                                     +--> Cloud Tasks -> /internal/run-job    (prod)

    get_analysis_status / get_analysis_result poll Redis.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

import fastmcp
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_server import create_app as create_app_base

from jobs import create_job, get_client, get_job, list_jobs, new_job_id, update_job
from worker import run_job

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080")
WORKER_SECRET = os.environ.get("WORKER_SECRET", "")
CLOUD_TASKS_QUEUE = os.environ.get("CLOUD_TASKS_QUEUE")
CLOUD_TASKS_LOCATION = os.environ.get("CLOUD_TASKS_LOCATION")
CLOUD_TASKS_PROJECT = os.environ.get("CLOUD_TASKS_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
CLOUD_TASKS_SERVICE_ACCOUNT = os.environ.get("CLOUD_TASKS_SERVICE_ACCOUNT")


# ---------------------------------------------------------------------------
# TradingAgents config (dataflow layer; sync tools use this directly)
# ---------------------------------------------------------------------------

def _configure_tradingagents() -> None:
    from tradingagents.dataflows.config import set_config
    from tradingagents.default_config import DEFAULT_CONFIG

    cfg = DEFAULT_CONFIG.copy()
    cfg["llm_provider"] = os.environ.get(
        "TRADINGAGENTS_LLM_PROVIDER", cfg.get("llm_provider", "openai")
    )
    if os.environ.get("TRADINGAGENTS_DEEP_THINK_LLM"):
        cfg["deep_think_llm"] = os.environ["TRADINGAGENTS_DEEP_THINK_LLM"]
    if os.environ.get("TRADINGAGENTS_QUICK_THINK_LLM"):
        cfg["quick_think_llm"] = os.environ["TRADINGAGENTS_QUICK_THINK_LLM"]
    set_config(cfg)


_configure_tradingagents()

from tradingagents.dataflows.interface import route_to_vendor  # noqa: E402

mcp = fastmcp.FastMCP(
    "tradingagents",
    instructions=(
        "Multi-agent trading-research toolkit. Granular data tools "
        "(get_stock_data, get_indicators, get_fundamentals, get_news, "
        "get_global_news with topic query for event-driven themes, etc.) "
        "run synchronously. start_analysis queues a full LangGraph analysis "
        "(~3-10 minutes) — poll get_analysis_status every 15-30 s and fetch "
        "get_analysis_result when state=done."
    ),
)


# ---------------------------------------------------------------------------
# Sync data tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    """OHLCV price data for a ticker. Dates yyyy-mm-dd."""
    return route_to_vendor("get_stock_data", symbol, start_date, end_date)


@mcp.tool()
def get_indicators(symbol: str, indicator: str, curr_date: str, look_back_days: int = 30) -> str:
    """Technical indicator (rsi, macd, bbands, ...). One per call."""
    indicators = [i.strip().lower() for i in indicator.split(",") if i.strip()]
    out: list[str] = []
    for ind in indicators:
        try:
            out.append(route_to_vendor("get_indicators", symbol, ind, curr_date, look_back_days))
        except ValueError as exc:
            out.append(str(exc))
    return "\n\n".join(out)


@mcp.tool()
def get_fundamentals(ticker: str, curr_date: str) -> str:
    """Fundamental data snapshot."""
    return route_to_vendor("get_fundamentals", ticker, curr_date)


@mcp.tool()
def get_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: Optional[str] = None) -> str:
    """Balance sheet (freq: annual|quarterly)."""
    return route_to_vendor("get_balance_sheet", ticker, freq, curr_date)


@mcp.tool()
def get_cashflow(ticker: str, freq: str = "quarterly", curr_date: Optional[str] = None) -> str:
    """Cash flow statement (freq: annual|quarterly)."""
    return route_to_vendor("get_cashflow", ticker, freq, curr_date)


@mcp.tool()
def get_income_statement(ticker: str, freq: str = "quarterly", curr_date: Optional[str] = None) -> str:
    """Income statement (freq: annual|quarterly)."""
    return route_to_vendor("get_income_statement", ticker, freq, curr_date)


@mcp.tool()
def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """Ticker-specific news in the date range."""
    return route_to_vendor("get_news", ticker, start_date, end_date)


@mcp.tool()
def get_global_news(
    curr_date: str,
    query: str = "",
    look_back_days: int = 7,
    limit: int = 15,
) -> str:
    """Global/macro news steered by a free-text topic query.

    Call multiple times with distinct queries to cover different angles
    (e.g. once for commodity-specific themes, once for geopolitics).
    Empty query falls back to a rotating generic macro set.
    """
    return route_to_vendor("get_global_news", curr_date, query, look_back_days, limit)


@mcp.tool()
def get_insider_transactions(ticker: str) -> str:
    """Insider transactions for a ticker."""
    return route_to_vendor("get_insider_transactions", ticker)


# ---------------------------------------------------------------------------
# Async analysis
# ---------------------------------------------------------------------------

def _redis_client():
    if not REDIS_URL:
        raise RuntimeError("REDIS_URL env var is required for analysis jobs")
    return get_client(REDIS_URL)


async def _enqueue_worker(job_id: str) -> None:
    """Hand the job off to whoever is going to execute it."""
    if CLOUD_TASKS_QUEUE and CLOUD_TASKS_LOCATION and CLOUD_TASKS_PROJECT:
        await _enqueue_cloud_tasks(job_id)
    else:
        # Dev / minimal deploy: fire-and-forget in-process async task.
        # Requires Cloud Run to be configured with --no-cpu-throttling.
        asyncio.create_task(run_job(job_id))


async def _enqueue_cloud_tasks(job_id: str) -> None:
    from google.cloud import tasks_v2

    client = tasks_v2.CloudTasksAsyncClient()
    parent = client.queue_path(CLOUD_TASKS_PROJECT, CLOUD_TASKS_LOCATION, CLOUD_TASKS_QUEUE)
    url = f"{BASE_URL.rstrip('/')}/internal/run-job"
    headers = {"Content-Type": "application/json"}
    if WORKER_SECRET:
        headers["X-Worker-Secret"] = WORKER_SECRET
    task: dict = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": url,
            "headers": headers,
            "body": json.dumps({"job_id": job_id}).encode(),
        },
    }
    if CLOUD_TASKS_SERVICE_ACCOUNT:
        task["http_request"]["oidc_token"] = {
            "service_account_email": CLOUD_TASKS_SERVICE_ACCOUNT,
            "audience": url,
        }
    await client.create_task(parent=parent, task=task)
    logger.info("Cloud Task enqueued for job %s", job_id)


@mcp.tool()
async def start_analysis(
    ticker: str,
    date: str,
    analysts: Optional[list[str]] = None,
    max_debate_rounds: Optional[int] = None,
    deep_think_llm: Optional[str] = None,
    quick_think_llm: Optional[str] = None,
) -> dict:
    """Queue a full TradingAgents analysis. Returns job_id for polling.

    Expected runtime 3–10 min. Poll get_analysis_status every 15–30 s;
    when state=done call get_analysis_result.
    """
    client = _redis_client()
    job_id = new_job_id()
    payload = {
        "kind": "analysis",
        "ticker": ticker,
        "date": date,
        "analysts": analysts or ["market", "social", "news", "fundamentals"],
        "max_debate_rounds": max_debate_rounds,
        "deep_think_llm": deep_think_llm,
        "quick_think_llm": quick_think_llm,
    }
    create_job(client, job_id, payload)
    await _enqueue_worker(job_id)
    return {"job_id": job_id, "state": "queued"}


@mcp.tool()
def get_analysis_status(job_id: str) -> dict:
    """Poll the job. `progress` is a structured object with phase / step /
    active_model / recent_llm_errors / etc."""
    job = get_job(_redis_client(), job_id)
    if not job:
        return {"error": "not_found", "job_id": job_id}
    raw_progress = job.get("progress")
    try:
        progress = json.loads(raw_progress) if raw_progress else None
    except (ValueError, TypeError):
        progress = raw_progress
    return {
        "job_id": job_id,
        "state": job.get("state"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "progress": progress,
        "error": job.get("error"),
    }


@mcp.tool()
def get_analysis_result(job_id: str) -> dict:
    """Return the full decision + reports + node trace of a completed job."""
    job = get_job(_redis_client(), job_id)
    if not job:
        return {"error": "not_found", "job_id": job_id}
    if job.get("state") != "done":
        return {"error": "not_ready", "state": job.get("state"), "job_id": job_id}
    return {"job_id": job_id, **json.loads(job.get("result", "{}"))}


@mcp.tool()
def list_analyses(limit: int = 20) -> list[dict]:
    """List recent jobs (most recent first)."""
    jobs = list_jobs(_redis_client(), limit=limit)
    return [
        {
            "job_id": j.get("id"),
            "state": j.get("state"),
            "created_at": j.get("created_at"),
            "payload": json.loads(j.get("payload", "{}")),
        }
        for j in jobs
    ]


@mcp.tool()
def cancel_analysis(job_id: str) -> dict:
    """Cancel a queued job. Running jobs continue to completion."""
    client = _redis_client()
    job = get_job(client, job_id)
    if not job:
        return {"error": "not_found"}
    if job.get("state") != "queued":
        return {"error": "cannot_cancel", "state": job.get("state")}
    update_job(client, job_id, state="cancelled")
    return {"job_id": job_id, "state": "cancelled"}


@mcp.tool()
async def reflect_and_remember(source_job_id: str, position_return: float) -> dict:
    """Queue a reflection on a completed analysis, given the realized return."""
    client = _redis_client()
    source = get_job(client, source_job_id)
    if not source or source.get("state") != "done":
        return {"error": "source_job_not_done"}
    refl_id = new_job_id()
    payload = {
        "kind": "reflect",
        "source_job_id": source_job_id,
        "position_return": float(position_return),
    }
    create_job(client, refl_id, payload)
    await _enqueue_worker(refl_id)
    return {"job_id": refl_id, "state": "queued"}


# ---------------------------------------------------------------------------
# Internal worker endpoint (Cloud Tasks target)
# ---------------------------------------------------------------------------

async def run_job_endpoint(request: Request) -> JSONResponse:
    """POST target for Cloud Tasks. Runs a single job synchronously.

    Protected by a shared secret OR (recommended) by a Cloud Run IAM policy
    that only allows the service account used to create the Cloud Task.
    """
    if WORKER_SECRET:
        if request.headers.get("X-Worker-Secret") != WORKER_SECRET:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_request"}, status_code=400)
    job_id = data.get("job_id")
    if not job_id:
        return JSONResponse({"error": "missing_job_id"}, status_code=400)
    await run_job(job_id)
    return JSONResponse({"status": "ok", "job_id": job_id})


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app():
    return create_app_base(
        mcp=mcp,
        extra_routes=[
            Route("/internal/run-job", run_job_endpoint, methods=["POST"]),
        ],
        base_url=BASE_URL,
        title="TradingAgents MCP",
    )
