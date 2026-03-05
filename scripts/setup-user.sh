#!/bin/bash
set -euo pipefail
# Create users and group if they don't exist
if ! id -u ytdl-uvicorn >/dev/null 2>&1; then
	useradd -u 3636 -r -s /bin/false ytdl-uvicorn
fi
if ! id -u ytdl-celery >/dev/null 2>&1; then
	useradd -u 3637 -r -s /bin/false ytdl-celery
fi
if ! getent group ytdl >/dev/null 2>&1; then
	groupadd ytdl
fi
usermod -aG ytdl ytdl-uvicorn
usermod -aG ytdl ytdl-celery
echo "ytdl-uvicorn and ytdl-celery users exist and have been added to the ytdl group."