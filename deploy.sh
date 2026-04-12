#!/usr/bin/env bash
# deploy.sh -- Deploy MCP OAuth server to Google Cloud Run
#
# Usage:
#   ./deploy.sh polymarket-mcp europe-west1
#   ./deploy.sh my-service us-central1 [project-id]

set -euo pipefail

SERVICE_NAME="${1:?Usage: ./deploy.sh <service-name> <region> [project-id]}"
REGION="${2:?Usage: ./deploy.sh <service-name> <region> [project-id]}"
PROJECT="${3:-$(gcloud config get-value project)}"

echo "==> Deploying $SERVICE_NAME to $REGION (project: $PROJECT)"

gcloud run deploy "$SERVICE_NAME" \
  --source . \
  --region "$REGION" \
  --project "$PROJECT" \
  --platform managed \
  --allow-unauthenticated \
  --set-env-vars "BASE_URL=https://${SERVICE_NAME}-$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --project "$PROJECT" --format 'value(status.url)' 2>/dev/null | sed 's/https:\/\///' || echo 'PENDING').run.app" \
  --memory 256Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 2 \
  --timeout 60

SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" \
  --region "$REGION" \
  --project "$PROJECT" \
  --format "value(status.url)")

# Patch BASE_URL now that we have the real URL
gcloud run services update "$SERVICE_NAME" \
  --region "$REGION" \
  --project "$PROJECT" \
  --set-env-vars "BASE_URL=${SERVICE_URL}"

echo ""
echo "==> Deployed: $SERVICE_URL"
echo ""
echo "Add to claude.ai connectors:"
echo "  MCP URL: ${SERVICE_URL}/mcp"
echo ""
echo "Test OAuth discovery:"
echo "  curl ${SERVICE_URL}/.well-known/oauth-authorization-server | jq"
echo ""
echo "Test health:"
echo "  curl ${SERVICE_URL}/health"
