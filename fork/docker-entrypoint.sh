#!/bin/sh
# Proxy entrypoint for docker compose: render config.yaml from the mounted
# repository (template + .env + fork additions), then start LiteLLM.
# Nothing has to be rendered on the host before `docker compose up`.
set -eu

REPO=${REPO_DIR:-/repo}
CONFIG=${RENDERED_CONFIG:-/tmp/config.yaml}

# Provider keys come from the mounted .env, not from the environment
# compose captured at container creation: the rendered config resolves
# them via os.environ/<VAR>, so exporting here lets a plain
# `docker compose restart litellm-proxy` pick up a changed key.
eval "$(python3 "$REPO/fork/env_exports.py" --env "$REPO/.env")"

python3 "$REPO/fork/render.py" \
    --env "$REPO/.env" \
    --template "$REPO/config.template.yaml" \
    --output "$CONFIG"

exec /app/docker/prod_entrypoint.sh --config "$CONFIG" "$@"
