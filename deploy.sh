#!/usr/bin/env bash
# deploy.sh -- Deploy TradingAgents MCP server to Google Cloud Run.
#
# Required env (source ~/.secrets.d/...):
#   OPENROUTER_API_KEY         — LLM provider auth
#   REDIS_URL                  — rediss://default:TOKEN@XXX.upstash.io:6379
#   ADMIN_PASSWORD             — OAuth login password (keep in a pwstore)
#   WORKER_SECRET              — shared secret for Cloud Tasks → /internal/run-job
#
# Optional env:
#   TRADINGAGENTS_LLM_PROVIDER          default: openrouter
#   TRADINGAGENTS_DEEP_THINK_LLM        default: deepseek/deepseek-chat
#   TRADINGAGENTS_QUICK_THINK_LLM       default: deepseek/deepseek-chat
#   TRADINGAGENTS_FALLBACK_MODELS       comma-separated
#   TRADINGAGENTS_MAX_DEBATE_ROUNDS     default: 1
#   TRADINGAGENTS_LLM_MAX_RETRIES       default: 6
#   CLOUD_TASKS_QUEUE, CLOUD_TASKS_LOCATION, CLOUD_TASKS_SERVICE_ACCOUNT
#
# Usage:
#   ./deploy.sh tradingagents-mcp europe-west3 [project-id]

set -euo pipefail

SERVICE_NAME="${1:?Usage: ./deploy.sh <service-name> <region> [project-id]}"
REGION="${2:?Usage: ./deploy.sh <service-name> <region> [project-id]}"
PROJECT="${3:-$(gcloud config get-value project)}"

: "${OPENROUTER_API_KEY:?set OPENROUTER_API_KEY}"
: "${REDIS_URL:?set REDIS_URL (rediss://...)}"
: "${ADMIN_PASSWORD:?set ADMIN_PASSWORD}"
: "${WORKER_SECRET:?set WORKER_SECRET}"

TA_LLM_PROVIDER="${TRADINGAGENTS_LLM_PROVIDER:-openrouter}"
TA_DEEP="${TRADINGAGENTS_DEEP_THINK_LLM:-deepseek/deepseek-chat}"
TA_QUICK="${TRADINGAGENTS_QUICK_THINK_LLM:-deepseek/deepseek-chat}"
TA_FALLBACK="${TRADINGAGENTS_FALLBACK_MODELS:-openrouter/elephant-alpha,z-ai/glm-4.5-air:free,meta-llama/llama-3.3-70b-instruct:free}"
TA_ROUNDS="${TRADINGAGENTS_MAX_DEBATE_ROUNDS:-1}"
TA_RETRIES="${TRADINGAGENTS_LLM_MAX_RETRIES:-6}"

echo "==> Deploying $SERVICE_NAME to $REGION (project: $PROJECT)"

# First pass: deploy without BASE_URL so we learn the service URL.
gcloud run deploy "$SERVICE_NAME" \
  --source . \
  --region "$REGION" \
  --project "$PROJECT" \
  --platform managed \
  --allow-unauthenticated \
  --memory 2Gi \
  --cpu 2 \
  --min-instances 0 \
  --max-instances 5 \
  --timeout 3600 \
  --no-cpu-throttling \
  --set-env-vars "OPENROUTER_API_KEY=${OPENROUTER_API_KEY}" \
  --set-env-vars "REDIS_URL=${REDIS_URL}" \
  --set-env-vars "ADMIN_PASSWORD=${ADMIN_PASSWORD}" \
  --set-env-vars "WORKER_SECRET=${WORKER_SECRET}" \
  --set-env-vars "TRADINGAGENTS_LLM_PROVIDER=${TA_LLM_PROVIDER}" \
  --set-env-vars "TRADINGAGENTS_DEEP_THINK_LLM=${TA_DEEP}" \
  --set-env-vars "TRADINGAGENTS_QUICK_THINK_LLM=${TA_QUICK}" \
  --set-env-vars "^##^TRADINGAGENTS_FALLBACK_MODELS=${TA_FALLBACK}" \
  --set-env-vars "TRADINGAGENTS_MAX_DEBATE_ROUNDS=${TA_ROUNDS}" \
  --set-env-vars "TRADINGAGENTS_LLM_MAX_RETRIES=${TA_RETRIES}"

SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" \
  --region "$REGION" --project "$PROJECT" \
  --format "value(status.url)")

# Patch BASE_URL (and Cloud Tasks wiring if provided) now that we know the URL.
PATCH_ARGS=(--set-env-vars "BASE_URL=${SERVICE_URL}")
if [[ -n "${CLOUD_TASKS_QUEUE:-}" && -n "${CLOUD_TASKS_LOCATION:-}" ]]; then
  PATCH_ARGS+=(--set-env-vars "CLOUD_TASKS_QUEUE=${CLOUD_TASKS_QUEUE}")
  PATCH_ARGS+=(--set-env-vars "CLOUD_TASKS_LOCATION=${CLOUD_TASKS_LOCATION}")
  PATCH_ARGS+=(--set-env-vars "CLOUD_TASKS_PROJECT=${PROJECT}")
  if [[ -n "${CLOUD_TASKS_SERVICE_ACCOUNT:-}" ]]; then
    PATCH_ARGS+=(--set-env-vars "CLOUD_TASKS_SERVICE_ACCOUNT=${CLOUD_TASKS_SERVICE_ACCOUNT}")
  fi
fi

gcloud run services update "$SERVICE_NAME" \
  --region "$REGION" --project "$PROJECT" \
  "${PATCH_ARGS[@]}"

echo ""
echo "==> Deployed: $SERVICE_URL"
echo "MCP URL for claude.ai:      ${SERVICE_URL}/mcp"
echo "OAuth discovery (sanity):   curl ${SERVICE_URL}/.well-known/oauth-authorization-server | jq"
echo "Health:                     curl ${SERVICE_URL}/health"
