# mcp-tradingagents

MCP server exposing [TradingAgents](https://github.com/TauricResearch/TradingAgents) as tools, with self-contained OAuth 2.1 PKCE (via [mcp-oauth-template](https://github.com/5queezer/mcp-oauth-template)) and a Redis-backed job queue. Deploys to Cloud Run in one command.

## Tools

**Sync data (fast):**
- `get_stock_data`, `get_indicators`, `get_fundamentals`
- `get_balance_sheet`, `get_cashflow`, `get_income_statement`
- `get_news`, `get_global_news(..., query=…)`, `get_insider_transactions`

**Async full-graph analysis (3–10 min, Redis-backed):**
- `start_analysis(ticker, date, …)` → `{job_id}`
- `get_analysis_status(job_id)` → structured progress (phase, active_model, llm_errors, …)
- `get_analysis_result(job_id)`
- `list_analyses`, `cancel_analysis`, `reflect_and_remember`

## Architecture

```
claude.ai ──OAuth──▶ /authorize, /token
          ──MCP ──▶ /mcp ──▶ sync tools             (route_to_vendor)
                         └▶ start_analysis ──▶ Redis (job=queued)
                                           ──▶ Cloud Tasks ──▶ /internal/run-job
                                              (or asyncio fire-and-forget for dev)
                         └▶ get_analysis_status     ──▶ reads Redis
```

Worker (`worker.run_job`) streams LangGraph in `stream_mode="updates"`, writes progress to Redis after each node, and captures `on_llm_start` / `on_llm_error` callbacks for rate-limit visibility. TradingAgents' `FallbackChatModel` (in the upstream fork) retries with fallback models on rate-limit / API errors.

## Environment

Required:

| var                  | purpose                                              |
|----------------------|------------------------------------------------------|
| `BASE_URL`           | public service URL (deploy.sh sets automatically)    |
| `ADMIN_PASSWORD`     | OAuth login                                          |
| `WORKER_SECRET`      | shared secret: Cloud Tasks → `/internal/run-job`     |
| `REDIS_URL`          | `rediss://default:TOKEN@HOST:PORT` (Upstash etc.)    |
| `OPENROUTER_API_KEY` | LLM provider                                         |

Tunable:

| var                               | default                    |
|-----------------------------------|----------------------------|
| `TRADINGAGENTS_LLM_PROVIDER`      | `openrouter`               |
| `TRADINGAGENTS_DEEP_THINK_LLM`    | `deepseek/deepseek-chat`   |
| `TRADINGAGENTS_QUICK_THINK_LLM`   | `deepseek/deepseek-chat`   |
| `TRADINGAGENTS_FALLBACK_MODELS`   | elephant-alpha, glm-air, … |
| `TRADINGAGENTS_MAX_DEBATE_ROUNDS` | `1`                        |
| `TRADINGAGENTS_LLM_MAX_RETRIES`   | `6`                        |

Cloud Tasks (optional — when set, `start_analysis` enqueues instead of running in-process):

- `CLOUD_TASKS_QUEUE`, `CLOUD_TASKS_LOCATION`, `CLOUD_TASKS_PROJECT`
- `CLOUD_TASKS_SERVICE_ACCOUNT` (for OIDC; otherwise `WORKER_SECRET` header is used)

## Deploy

```bash
source ~/.secrets.d/openrouter
export REDIS_URL='rediss://default:TOKEN@XXX.upstash.io:6379'
export ADMIN_PASSWORD='…'
export WORKER_SECRET="$(openssl rand -hex 32)"

./deploy.sh tradingagents-mcp europe-west3
```

Claude.ai connector URL:

```
https://tradingagents-mcp-<hash>.run.app/mcp
```

## Files

```
tradingagents_server.py   FastMCP instance + tools + /internal/run-job + create_app
worker.py                 LangGraph analysis runner + Redis progress callback
jobs.py                   Redis helpers
mcp_server/               mcp-oauth-template, + RedisTokenStore/ClientStore
Dockerfile                python:3.12-slim + git + uvicorn
deploy.sh                 gcloud run deploy wrapper
```

## License

MIT (inherited from mcp-oauth-template).
