#!/bin/sh
# Proxy entrypoint for docker compose: render config.yaml from the mounted
# repository (template + .env + fork additions), then start LiteLLM.
# Nothing has to be rendered on the host before `docker compose up`.
set -eu

REPO=${REPO_DIR:-/repo}
CONFIG=${RENDERED_CONFIG:-/tmp/config.yaml}

python3 "$REPO/fork/render.py" \
  --env "$REPO/.env" \
  --template "$REPO/config.template.yaml" \
  --output "$CONFIG"

exec /app/docker/prod_entrypoint.sh --config "$CONFIG" "$@"
