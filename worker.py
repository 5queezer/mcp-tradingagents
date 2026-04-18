"""
Analysis worker — runs a single TradingAgents job start-to-finish.

Exposed as a plain async function so it can be driven from:
  - an in-process asyncio.create_task (simplest, for low volume)
  - an HTTP endpoint that Cloud Tasks POSTs to (recommended for production)

Progress is streamed to Redis as each LangGraph node completes. LLM retries
and fallback-model switches are written out on every on_llm_start /
on_llm_error callback so pollers see "rate_limited" state immediately
instead of watching a dead-looking status.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import traceback
from typing import Any, Optional
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from jobs import get_client, get_job, update_job


_PHASE_LABEL = {
    "Market Analyst": "Market Analyst",
    "Social Analyst": "Social Analyst",
    "News Analyst": "News Analyst",
    "Fundamentals Analyst": "Fundamentals Analyst",
    "tools_market": "Market Analyst (tools)",
    "tools_social": "Social Analyst (tools)",
    "tools_news": "News Analyst (tools)",
    "tools_fundamentals": "Fundamentals Analyst (tools)",
    "Msg Clear Market": "Market Analyst (cleanup)",
    "Msg Clear Social": "Social Analyst (cleanup)",
    "Msg Clear News": "News Analyst (cleanup)",
    "Msg Clear Fundamentals": "Fundamentals Analyst (cleanup)",
    "Bull Researcher": "Bull Researcher (debate)",
    "Bear Researcher": "Bear Researcher (debate)",
    "Research Manager": "Research Manager",
    "Trader": "Trader",
    "Aggressive Analyst": "Risk Debate — Aggressive",
    "Conservative Analyst": "Risk Debate — Conservative",
    "Neutral Analyst": "Risk Debate — Neutral",
    "Portfolio Manager": "Portfolio Manager",
}

_REPORT_LABEL = {
    "market_report": "Market Analysis",
    "sentiment_report": "Social Sentiment",
    "news_report": "News Analysis",
    "fundamentals_report": "Fundamentals Analysis",
    "investment_plan": "Research Team Plan",
    "trader_investment_plan": "Trader Plan",
    "final_trade_decision": "Final Trade Decision",
}


class _ProgressTracker:
    def __init__(self, client, job_id: str):
        self.client = client
        self.job_id = job_id
        self.state: dict[str, Any] = {}
        self.history: list[str] = []
        self.step = 0
        self.current_node: Optional[str] = None
        self.last_llm_model: Optional[str] = None
        self.active_fallback: Optional[str] = None
        self.llm_errors: list[dict[str, str]] = []

    def write(self) -> None:
        reports_done = [
            _REPORT_LABEL[section]
            for section in _REPORT_LABEL
            if self.state.get(section)
        ]
        debate_round = self.state.get("investment_debate_state", {}).get("count", 0)
        risk_round = self.state.get("risk_debate_state", {}).get("count", 0)
        progress = {
            "current_node": self.current_node,
            "phase": _PHASE_LABEL.get(self.current_node, self.current_node)
            if self.current_node
            else None,
            "step": self.step,
            "reports_done": reports_done,
            "investment_debate_count": debate_round,
            "risk_debate_count": risk_round,
            "recent_nodes": self.history[-8:],
            "active_model": self.last_llm_model,
            "active_fallback": self.active_fallback,
            "recent_llm_errors": self.llm_errors[-3:],
            "updated_at": int(time.time()),
        }
        update_job(self.client, self.job_id, progress=json.dumps(progress))


class _ProgressCallback(BaseCallbackHandler):
    def __init__(self, tracker: _ProgressTracker, primary_model: str, fallback_models: list[str]):
        self.tracker = tracker
        self.primary_model = primary_model
        self.fallback_models = fallback_models

    def _model_from(self, serialized: Optional[dict], metadata: Optional[dict]) -> Optional[str]:
        if metadata:
            name = metadata.get("ls_model_name") or metadata.get("model_name")
            if name:
                return name
        if serialized:
            kwargs = serialized.get("kwargs") or {}
            for key in ("model", "model_name", "deployment_name"):
                if kwargs.get(key):
                    return kwargs[key]
        return None

    def on_llm_start(self, serialized, prompts, *, run_id: UUID, tags=None, metadata=None, **kwargs):  # noqa: D401
        model = self._model_from(serialized, metadata)
        if not model:
            return
        self.tracker.last_llm_model = model
        self.tracker.active_fallback = (
            model if model != self.primary_model and model in self.fallback_models else None
        )
        self.tracker.write()

    on_chat_model_start = on_llm_start

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs):
        self.tracker.llm_errors.append({
            "model": self.tracker.last_llm_model or "?",
            "error_type": type(error).__name__,
            "message": str(error)[:240],
            "at": int(time.time()),
        })
        if len(self.tracker.llm_errors) > 20:
            self.tracker.llm_errors = self.tracker.llm_errors[-20:]
        self.tracker.write()


def _build_config(payload: dict[str, Any]) -> dict[str, Any]:
    from tradingagents.default_config import DEFAULT_CONFIG

    cfg = DEFAULT_CONFIG.copy()
    cfg["llm_provider"] = os.environ.get(
        "TRADINGAGENTS_LLM_PROVIDER", cfg.get("llm_provider", "openai")
    )
    cfg["deep_think_llm"] = (
        payload.get("deep_think_llm")
        or os.environ.get("TRADINGAGENTS_DEEP_THINK_LLM")
        or cfg.get("deep_think_llm")
    )
    cfg["quick_think_llm"] = (
        payload.get("quick_think_llm")
        or os.environ.get("TRADINGAGENTS_QUICK_THINK_LLM")
        or cfg.get("quick_think_llm")
    )
    if payload.get("max_debate_rounds") is not None:
        cfg["max_debate_rounds"] = int(payload["max_debate_rounds"])
    elif os.environ.get("TRADINGAGENTS_MAX_DEBATE_ROUNDS"):
        cfg["max_debate_rounds"] = int(os.environ["TRADINGAGENTS_MAX_DEBATE_ROUNDS"])
    return cfg


def _merge_delta(state: dict[str, Any], delta: dict[str, Any]) -> None:
    for key, val in delta.items():
        if key == "messages" and isinstance(val, list):
            state.setdefault("messages", [])
            state["messages"].extend(val)
        else:
            state[key] = val


def _fallback_models() -> list[str]:
    return [
        m.strip()
        for m in os.environ.get("TRADINGAGENTS_FALLBACK_MODELS", "").split(",")
        if m.strip()
    ]


def _run_analysis_sync(payload: dict[str, Any], client, job_id: str) -> dict[str, Any]:
    """Blocking analysis run. Called from run_analysis via asyncio.to_thread."""
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    cfg = _build_config(payload)
    analysts = payload.get("analysts") or ["market", "social", "news", "fundamentals"]
    tracker = _ProgressTracker(client, job_id)
    primary = cfg["quick_think_llm"]
    callback = _ProgressCallback(tracker, primary, _fallback_models())

    ta = TradingAgentsGraph(
        selected_analysts=analysts,
        debug=False,
        config=cfg,
        callbacks=[callback],
    )

    init_state = ta.propagator.create_initial_state(payload["ticker"], payload["date"])
    args = ta.propagator.get_graph_args(callbacks=[callback])
    args["stream_mode"] = "updates"

    tracker.state = dict(init_state)
    tracker.current_node = "starting"
    tracker.write()

    for chunk in ta.graph.stream(init_state, **args):
        for node_name, delta in chunk.items():
            tracker.step += 1
            tracker.history.append(node_name)
            tracker.current_node = node_name
            if delta:
                _merge_delta(tracker.state, delta)
            tracker.write()

    state = tracker.state
    decision = ta.process_signal(state.get("final_trade_decision", ""))
    ta.curr_state = state
    try:
        ta._log_state(payload["date"], state)
    except Exception as exc:
        print(f"[worker] _log_state failed: {exc}", flush=True)

    return {
        "decision": decision,
        "final_trade_decision": state.get("final_trade_decision"),
        "ticker": payload["ticker"],
        "date": payload["date"],
        "analysts": analysts,
        "reports": {
            label: state.get(section)
            for section, label in _REPORT_LABEL.items()
            if state.get(section)
        },
        "steps_executed": tracker.step,
        "nodes_visited": tracker.history,
        "llm_errors_seen": len(tracker.llm_errors),
    }


def _run_reflection_sync(payload: dict[str, Any]) -> dict[str, Any]:
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    cfg = _build_config(payload)
    ta = TradingAgentsGraph(debug=False, config=cfg)
    ta.reflect_and_remember(float(payload["position_return"]))
    return {"reflected": True, "source_job_id": payload.get("source_job_id")}


async def run_job(job_id: str) -> None:
    """Async wrapper that runs a single job start-to-finish.

    Idempotent: if the job is already in state=done/error/cancelled, returns
    immediately. Handles transition to running, error capture, and final
    result write.
    """
    redis_url = os.environ["REDIS_URL"]
    client = get_client(redis_url)
    job = get_job(client, job_id)
    if not job:
        print(f"[worker] job {job_id} not found", flush=True)
        return
    if job.get("state") != "queued":
        print(
            f"[worker] job {job_id} already in state={job.get('state')}, skipping",
            flush=True,
        )
        return

    update_job(client, job_id, state="running", started_at=str(int(time.time())))
    try:
        payload = json.loads(job.get("payload", "{}"))
        kind = payload.get("kind", "analysis")
        if kind == "reflect":
            result = await asyncio.to_thread(_run_reflection_sync, payload)
        else:
            result = await asyncio.to_thread(_run_analysis_sync, payload, client, job_id)
        update_job(
            client, job_id,
            state="done",
            finished_at=str(int(time.time())),
            result=json.dumps(result, default=str),
        )
        print(f"[worker] job {job_id} done ({kind})", flush=True)
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[worker] job {job_id} failed: {exc}\n{tb}", flush=True)
        update_job(
            client, job_id,
            state="error",
            finished_at=str(int(time.time())),
            error=f"{type(exc).__name__}: {exc}",
        )
