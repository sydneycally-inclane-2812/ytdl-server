#!/bin/bash
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1 --reload &
UVICORN_PID=$!
uv run celery -A celery_app worker --loglevel=info --pool=solo &
CELERY_PID=$!
echo "Started uvicorn and celery, API available at http://0.0.0.0:8000"
trap "kill $UVICORN_PID $CELERY_PID 2>/dev/null" EXIT
wait -n
exit $?


