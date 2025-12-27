from fastapi import FastAPI, HTTPException, Depends, Query, UploadFile, File, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from contextlib import asynccontextmanager
from helpers import get_ydl_opts, init_db
from pathlib import Path
from yt_dlp import YoutubeDL
from collections import defaultdict, deque
import sqlite3
import yaml
import os
import logging, logging.config

DATA_ROOT_PATH = Path('/srv/hgst/ytdl/')
DB_PATH = Path(".database/ytdl-manager.db")


os.makedirs(DATA_ROOT_PATH, exist_ok=True)
opt = get_ydl_opts(root_dir=DATA_ROOT_PATH)

logger = None
database = None
celery = None


async def lifespan(app: FastAPI):
	# Initialize the logger
	with open("logger_config.yaml", "r") as f:
		config = yaml.safe_load(f)
	logging.config.dictConfig(config)
	logger = logging.getLogger(__name__)
	logger.debug(f"Path {str(DATA_ROOT_PATH)} exists.")
	# Initialize the database

	# Initialize celery

	# Initialize
	pass

try:
	conn = sqlite3.connect(DB_PATH)
except Exception as e:
	logger.error("Cannot connect to DB: Encountered Error", e)


app = FastAPI(title="YTDL Management Server", version="0.1", description="YTDL management service")

@app.get("/")
async def docs():
	return RedirectResponse(url="/docs", status_code=307)


'''
Endpoints: 
- add_playlist: Simple interface, to add or remove playlists
- admin: More complex interface to view and CRUD on playlists, configure yt-dlp features, manage cron job schedules
- force_update: trigger an update with parameters
- health_check: Healthcheck (200, 501 + error)

Cron jobs:
- Scan from youtube playlists for changes, update database
- Update local repository
- Read and update remote repository

Integration:
- yt-dlp
- rsync
- ntfy for notifications.
'''

"""
# Interface & API Plan (v1)

## Base URLs
- Interface: `/`
- API: `/api/`

## Auth Model
- Session cookie OR Bearer token (choose one per deploy; both supported).
- Roles: "user", "admin".
- All `/api/*` endpoints require auth; role gates noted below.
- Optional: `Idempotency-Key` header for mutating endpoints that enqueue jobs.

## Interface

#### Login `/login` (UI)
- Users/admins sign in; returns session cookie (HTTPOnly, SameSite=Lax/Strict).

#### User `/user` (UI)
- Manage own playlists; trigger own library update; view job status.
- Calls:
		- `GET  /api/playlist/get`
		- `POST /api/playlist/add`
		- `DELETE /api/playlist/remove`
		- `POST /api/manage/library-update`       # scope=user (implicit)
		- `GET  /api/manage/job-status`

#### Admin `/admin` (UI)
- Manage all users & playlists; health; schedules; global library updates.
- Calls:
		- `GET  /api/manage/healthcheck`
		- `GET  /api/playlist/get`
		- `POST /api/playlist/add`
		- `DELETE /api/playlist/remove`
		- `POST /api/manage/library-update`       # scope=global (admin only)
		- `GET  /api/user/get`
		- `POST /api/user/add`
		- `DELETE /api/user/remove`
		- `GET  /api/manage/schedule`
		- `GET  /api/manage/queue`

## API

### Management
Endpoint group: `/api/manage`

- `GET /api/manage/healthcheck`
	Description: Liveness/readiness and dependency check (yt-dlp, ffmpeg, rsync).
	Params: none
	Roles: user, admin
	Responses:
		200 { service: "ok", versions: { app, yt_dlp, ffmpeg, rsync }, last_jobs: {...} }
		501 { error: { code: "DEP_MISSING", message, details } }

- `GET /api/manage/job-status`
	Description: Get status of a job.
	Params:
		- `id` (required): Job ID (UUID)
	Roles: user (own jobs), admin (any)
	Response: 200 { id, kind, status, created_at, started_at, finished_at, result?, error? }

- `GET /api/manage/queue`
	Description: Inspect job queue.
	Params:
		- `limit` (optional, default 50)
		- `fields` (optional, comma-separated; default all)
	Roles: admin
	Response: 200 { items: [...], total }

- `GET /api/manage/schedule`
	Description: List cron jobs (scanner/downloader/rsync/composite).
	Params:
		- `name` (optional): filter by job name
		- `fields` (optional)
	Roles: admin
	Response: 200 { jobs: [...] }

- `POST /api/manage/library-update`
	Description: Enqueue a library update (scan/download/rsync pipeline).
	Body:
		- `user`  (required if scope=user by admin; implicit as caller if role=user)
		- `scope` (required): "user" | "global"  (global requires admin)
		- `options` (optional): { full_rescan?: bool, fast?: bool, dry_run?: bool }
	Headers:
		- `Idempotency-Key` (optional)
	Roles: user (scope=user), admin (user/global)
	Responses:
		202 { job_id }
		409 { error: { code: "JOB_DUPLICATE", ... } }  # if debounced without allow_concurrent

### Playlist Management
Endpoint group: `/api/playlist`

- `GET /api/playlist/get`
	Description: Get playlists.
	Params:
		- `id` (optional): single playlist
		- `owner` (optional, admin only): filter by user ID
		- `page`, `per_page` (optional)
	Roles: user (own), admin (any)
	Response: 200 { items: [...], page, per_page, total }

- `POST /api/playlist/add`
	Description: Add a playlist for current user (or target user if admin).
	Body:
		- `playlist_url` (required)
		- `title` (optional)
		- `tags` (optional list)
		- `policy` (optional): { audio_only?, embed_thumbnail?, format?, write_metadata?, archive? }
		- `owner` (admin only, optional): user ID
	Roles: user (own), admin (any)
	Responses:
		201 { id, playlist_url, owner, enabled: true }
		409 { error: { code: "ALREADY_EXISTS", ... } }

- `DELETE /api/playlist/remove`
	Description: Remove or disable a playlist.
	Params:
		- `id` (required) OR `ids` (comma-separated)
		- `hard` (optional, default false): hard delete vs soft disable
	Roles: user (own), admin (any)
	Response: 204

### User Management
Endpoint group: `/api/user`

- `GET /api/user/get`
	Description: List or fetch users.
	Params:
		- `id` (optional)
		- `page`, `per_page` (optional)
	Roles: admin
	Response: 200 { items: [...], page, per_page, total }

- `POST /api/user/add`
	Description: Create a user.
	Body:
		- `email` (required)
		- `role`  (optional; default "user")
		- `password` or provisioning method (depending on auth)
	Roles: admin
	Response: 201 { id, email, role }

- `DELETE /api/user/remove`
	Description: Remove user(s).
	Params:
		- `id` (required) OR `ids` (comma-separated)
	Roles: admin
	Response: 204

### Notifications & Integrations (optional surfaces)
- Telegram bridge (server-side): emit job lifecycle messages.
	Config at admin level; no public endpoint required.
- ntfy support can be toggled in config (admin UI) if implemented.

## Conventions

- Naming: use either kebab-case or snake_case in paths; this plan uses kebab in actions and underscores in bodies; keep paths consistent (e.g., prefer `library-update` in URL).
- Errors (uniform):
	{ "error": { "code": "STRING_CODE", "message": "Human readable", "details": {...} } }
- Pagination for list endpoints: `page`, `per_page`; response includes `total`.
- Security:
	- Auth required on all API routes.
	- Role checks server-side.
	- Validate playlist URL is canonical YouTube playlist.
	- Constrain filesystem and sanitize rsync/ytdlp args (allowlist).
- Observability:
	- Each job has `logs_url` if you expose logs (redacted).
	- Healthcheck returns dependency versions.
"""
