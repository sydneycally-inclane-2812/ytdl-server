#!/bin/bash
set -euo pipefail

# This script executes the server, used in the systemd service file. Not intended to be run directly.
# Includes: setting up environment variables, activating the virtual environment, and running the FastAPI server + Celery worker as ytdl-uvicorn and ytdl-celery users respectively.

# Check if running as root
if [[ "${EUID}" -ne 0 ]]; then
	echo "This script must be run as root" >&2
	exit 1
fi

# Get project root directory
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")
PROJECT_ROOT=$(dirname "$SCRIPT_DIR")
cd "$PROJECT_ROOT/src"
echo "Working directory: $(pwd)"

# Load environment variables if .env exists
if [[ -f "$PROJECT_ROOT/.env" ]]; then
	set -a
	source "$PROJECT_ROOT/.env"
	set +a
	echo "Loaded environment variables from .env"
fi

# Set up PATH
export PATH="/root/.local/bin:$PROJECT_ROOT/.venv/bin:/usr/local/bin:$PATH"

# Verify required commands
if ! command -v uv >/dev/null 2>&1; then
	echo "Error: uv not found in PATH" >&2
	exit 1
fi

# Parse config
CONFIG_FILE="$PROJECT_ROOT/config/app_config.json"
if ! command -v jq >/dev/null 2>&1; then
	echo "Error: jq not found" >&2
	exit 1
fi

CURRENT_ENV=$(jq -r '.current // "prod"' "$CONFIG_FILE")
PORT=$(jq -r --arg env "$CURRENT_ENV" '.[$env].port // 8080' "$CONFIG_FILE")
CELERY_RUNTIME_DIR="$PROJECT_ROOT/logs/celery"
CELERY_BEAT_SCHEDULE="$CELERY_RUNTIME_DIR/celerybeat-schedule"

echo "Environment: $CURRENT_ENV"
echo "Port: $PORT"

# Ensure celery runtime directory exists
mkdir -p "$CELERY_RUNTIME_DIR"
# Ensure celery runtime directory is owned by root:ytdl and has permissions 770
chown root:ytdl "$CELERY_RUNTIME_DIR"
chmod 770 "$CELERY_RUNTIME_DIR"

# Trap signals for cleanup
PIDS=()
cleanup() {
	echo "Shutting down services..."
	for pid in "${PIDS[@]}"; do
		kill "$pid" 2>/dev/null || true
	done
	wait
	echo "All services stopped."
	exit 0
}
trap cleanup INT TERM EXIT

# Start Celery worker as ytdl-celery
echo "Starting Celery worker as ytdl-celery..."
runuser -u ytdl-celery -- uv run --project "$PROJECT_ROOT" celery -A celery_app worker --loglevel=INFO --pool=solo &

# Start Celery beat as ytdl-celery
echo "Starting Celery beat as ytdl-celery..."
runuser -u ytdl-celery -- uv run --project "$PROJECT_ROOT" celery -A celery_app beat --loglevel=INFO --schedule "$CELERY_BEAT_SCHEDULE" &

# Start FastAPI server as ytdl-uvicorn
echo "Starting FastAPI server as ytdl-uvicorn on port $PORT..."
runuser -u ytdl-uvicorn -- uv run --project "$PROJECT_ROOT" uvicorn main:app --host 0.0.0.0 --port "$PORT" --workers 1 &

# Wait for all processes
wait
