from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager
from pathlib import Path
import aiosqlite
import yaml
import os
import logging
import logging.config
import shutil
from yt_dlp import YoutubeDL
from celery import Celery
import dotenv
import json
import sys
from helpers import validate_true_playlist_url, check_playlist_accessible
from celery_app import scan

homedir = Path(__file__).parent.parent.resolve()
with open(homedir / "config" / "app_config.json", "r") as f:
	config = json.load(f)

DATA_ROOT_PATH = homedir / Path(config[config["current"]]["root_dir"])
DB_PATH = homedir / Path(config[config["current"]]["database_path"])
LOG_PATTERN = str(config[config["current"]]["logging_pattern"])

os.makedirs(DATA_ROOT_PATH, exist_ok=True)
os.makedirs(DB_PATH.parent, exist_ok=True)

def init_logger() -> logging.Logger:
	try:
		with open(homedir / "config" / "logger_config.yaml", "r") as f:
			config = yaml.safe_load(f)
		logging.config.dictConfig(config)
		logger = logging.getLogger(LOG_PATTERN)
		logger.debug("Logger configured")
		return logger
	except Exception as e:
		logging.basicConfig(level=logging.INFO)
		logger = logging.getLogger(__name__)
		logger.error(f"Logger initialization failed: {e}")
		return logger

async def get_db():
	async with aiosqlite.connect(DB_PATH) as db:
		await db.execute("PRAGMA foreign_keys = ON")
		db.row_factory = aiosqlite.Row
		yield db

@asynccontextmanager
async def lifespan(app: FastAPI):
	app.state.logger = init_logger()
	logger = app.state.logger
	dotenv.load_dotenv()
	if os.getenv("PASSKEY"):
		logger.info(".env loaded")
	# Check storage permissions
	try:
		test_dir = DATA_ROOT_PATH / "testing_write_permissions"
		test_dir.mkdir()
		shutil.rmtree(test_dir)
		logger.info("Storage path is writable")
	except Exception as e:
		logger.error(f"Storage path not writable: {e}")

	# Initialize DB
	async with aiosqlite.connect(DB_PATH) as db:
		await db.execute("PRAGMA foreign_keys = ON")

		await db.execute("""
		CREATE TABLE IF NOT EXISTS user (
			name TEXT PRIMARY KEY,
			display_name TEXT NOT NULL,
			admin INTEGER NOT NULL DEFAULT 0,
			active INTEGER NOT NULL DEFAULT 1
		)
		""")

		await db.execute("""
		CREATE TABLE IF NOT EXISTS playlist (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			playlist_id TEXT NOT NULL,
			name TEXT,
			owner TEXT NOT NULL,
			active INTEGER NOT NULL DEFAULT 1,
			FOREIGN KEY(owner) REFERENCES user(name) ON DELETE RESTRICT
		)
		""")

		await db.execute("""
		CREATE UNIQUE INDEX IF NOT EXISTS idx_playlist_owner_pid
		ON playlist(owner, playlist_id)
		""")

		await db.commit()
		logger.info("Database ready")
		
		cur = await db.execute("SELECT COUNT(*) FROM playlist")
		user_count = (await cur.fetchone())
		logger.info(f"Playlist table row count: {user_count[0]}")

		cur = await db.execute("SELECT COUNT(*) FROM user")
		user_count = (await cur.fetchone())
		logger.info(f"User table row count: {user_count[0]}")

	# Celery placeholder
	try:
		app.state.celery = Celery("ytdl")
		logger.info("Celery initialized")
	except Exception as e:
		logger.error(f"Celery init failed: {e}")
		app.state.celery = None

	yield
	logger.info("Application shutdown")

app = FastAPI(
	title="YTDL Management Server",
	version="0.1",
	description="YTDL management service",
	lifespan=lifespan,
)

@app.get("/")
async def docs():
	return RedirectResponse(url="/docs", status_code=307)

@app.post("/api/user/add")
async def add_user(
	name: str,
	display_name: str,
	passkey: str,
	admin: bool = False,
	db: aiosqlite.Connection = Depends(get_db),
):
	'''
	Creates a new user in the database.

		name: unique identifier for the user

		display_name: name that is used in the UI

		passkey: passkey for su management

		admin: whether the user is an admin or not
	
	Returns:

		str: json response
	'''
	logger = app.state.logger
 
	try:
		if passkey != os.getenv("PASSKEY"):
			raise HTTPException(status_code=401, detail="Invalid credentials")
		# Try insert, reactivate if inactive
		try:
			await db.execute(
				"INSERT INTO user (name, display_name, admin) VALUES (?, ?, ?)",
				(name, display_name, int(admin)),
			)
			await db.commit()
		except aiosqlite.IntegrityError:
			cur = await db.execute(
				"SELECT active FROM user WHERE name = ?", (name,)
			)
			row = await cur.fetchone()
			if row and row["active"] == 0:
				await db.execute(
					"UPDATE user SET active = 1, display_name = ?, admin = ? WHERE name = ?",
					(display_name, int(admin), name),
				)
				await db.commit()
			else:
				raise HTTPException(status_code=409, detail="User already exists")
		logger.debug(f"Added user {name} as {'admin' if admin else 'non-admin'}")
		return {
			"status": "success",
			"user": {
				"name": name,
				"display_name": display_name,
				"admin": admin,
				"active": True
			},
		}
	except HTTPException:
		raise
	except Exception:
		logger.error("Error adding user")
		raise HTTPException(status_code=500, detail="Failed to add user")

@app.delete("/api/user/deactivate")
async def deactivate_user(	
	name: str,
	passkey: str,
	db: aiosqlite.Connection = Depends(get_db)):
	'''
	Marks the user as deactivated in the database.
	'''
	logger = app.state.logger
	try:
		if passkey != os.getenv("PASSKEY"):
			raise HTTPException(status_code=401, detail="Invalid credentials")
		cur = await db.execute(
			"UPDATE user SET active = 0 WHERE name = ? AND active = 1", (name,)
		)
		await db.commit()
		if cur.rowcount == 0:
			raise HTTPException(status_code=404, detail="User not found or already inactive")
		return {"status": "success", "user": name, "active": False}
	except Exception:
		logger.exception("Error deactivating user")
		raise HTTPException(status_code=500, detail="Failed to deactivate user")

@app.put("/api/playlist/add")
async def add_playlist(
	url: str,
	owner: str,
	db: aiosqlite.Connection = Depends(get_db),
):
	logger = app.state.logger
	try:
		try:
			url = validate_true_playlist_url(url)
		except ValueError as exc:
			raise HTTPException(status_code=400, detail=str(exc))

		# Ensure owner exists and active
		cur = await db.execute(
			"SELECT 1 FROM user WHERE name = ? AND active = 1", (owner,)
		)
		if not await cur.fetchone():
			raise HTTPException(status_code=404, detail="Owner not found or inactive")

		# Check playlist accessibility with yt-dlp
		try:
			meta = check_playlist_accessible(url)
			logger.debug(f"meta for {url}: {str(meta)}")
		except RuntimeError as e:
			raise HTTPException(status_code=400, detail=f"Playlist not accessible: {e}")

		playlist_id = meta["playlist_id"]
		final_name = (meta.get("title") or playlist_id).strip()
		playlist_row_id = None

		# Try insert or reactivate
		try:
			insert_cur = await db.execute(
				"INSERT INTO playlist (playlist_id, name, owner) VALUES (?, ?, ?)",
				(playlist_id, final_name, owner),
			)
			await db.commit()
			playlist_row_id = insert_cur.lastrowid
		except aiosqlite.IntegrityError:
			# Reactivate if inactive
			cur = await db.execute(
				"SELECT id FROM playlist WHERE playlist_id = ? AND owner = ? AND active = 0",
				(playlist_id, owner),
			)
			row = await cur.fetchone()
			if row:
				await db.execute(
					"UPDATE playlist SET active = 1, name = ? WHERE id = ?",
					(final_name, row["id"]),
				)
				await db.commit()
				playlist_row_id = row["id"]
			else:
				raise HTTPException(status_code=409, detail="Playlist already exists")

		return {
			"status": "success",
			"playlist": {
				"id": playlist_row_id,
				"playlist_id": playlist_id,
				"name": final_name,
				"video_count": meta.get("count"),
				"active": True
			},
		}
	except HTTPException:
		raise
	except Exception:
		logger.exception("Error adding playlist")
		raise HTTPException(status_code=500, detail="Failed to add playlist")

@app.delete("/api/playlist/deactivate/{playlist_id}")
async def deactivate_playlist(
	playlist_id: str,
	owner: str,
	db: aiosqlite.Connection = Depends(get_db),
):
	'''
	Deactivate a playlist by ID.
	'''
	logger = app.state.logger
	try:
		cur = await db.execute(
			"UPDATE playlist SET active = 0 WHERE playlist_id = ? AND owner = ? AND active = 1",
			(playlist_id, owner),
		)
		await db.commit()
		if cur.rowcount == 0:
			raise HTTPException(
				status_code=404,
				detail="Playlist not found or already inactive",
			)
		return {"status": "success", "playlist_id": playlist_id, "active": False}
	except Exception:
		logger.exception("Error deactivating playlist")
		raise HTTPException(status_code=500, detail="Failed to deactivate playlist")

@app.get("/api/playlist/get_all")
async def get_all_playlists(
	owner: str,
	include_all: bool = False,
	db: aiosqlite.Connection = Depends(get_db),
):
	"""
	Return playlists visible to the requesting owner.

	Admins may request all playlists with include_all; non-admins only see
	active playlists they own. The response includes item count and metadata.
	"""
	logger = app.state.logger
	try:
		cur = await db.execute(
			"SELECT admin FROM user WHERE name = ? AND active = 1", (owner,)
		)
		row = await cur.fetchone()
		if not row:
			raise HTTPException(status_code=404, detail="Owner not found or inactive")
		is_admin = bool(row["admin"])

		if is_admin and include_all:
			cur = await db.execute("""
				SELECT p.id, p.playlist_id, p.name, p.owner, u.display_name AS owner_display_name, p.active
				FROM playlist p
				JOIN user u ON p.owner = u.name
			""")
		else:
			cur = await db.execute("""
				SELECT p.id, p.playlist_id, p.name, p.owner, u.display_name AS owner_display_name
				FROM playlist p
				JOIN user u ON p.owner = u.name
				WHERE p.owner = ? AND p.active = 1
			""", (owner,))

		playlists = [dict(r) for r in await cur.fetchall()]
		return {"items": playlists, "total": len(playlists)}
	except HTTPException:
		raise
	except Exception:
		logger.exception("Error getting playlists")
		raise HTTPException(status_code=500, detail="Failed to get playlists")

@app.get("/api/playlist/check_access")
async def check_access(url: str):
	"""
	Check if yt-dlp can access a playlist and return basic stats.
	"""
	logger = app.state.logger
	try:
		try:
			url = validate_true_playlist_url(url)
		except ValueError as exc:
			raise HTTPException(status_code=400, detail=str(exc))

		meta = check_playlist_accessible(url)
		return {
			"status": "ok",
			"playlist": meta,
		}
	except HTTPException:
		raise
	except Exception:
		logger.exception("Error checking playlist access")
		raise HTTPException(status_code=500, detail="Failed to check playlist access")

async def update_dependencies():
	"""
	Tries to update ytdlp and ffmpeg, useful when downloads encounter error.
	"""
	logger = app.state.logger

	async def run_cmd(*args: str) -> dict:
		proc = await asyncio.create_subprocess_exec(
			*args,
			stdout=asyncio.subprocess.PIPE,
			stderr=asyncio.subprocess.PIPE,
		)
		stdout, stderr = await proc.communicate()
		return {
			"cmd": " ".join(args),
			"exit_code": proc.returncode,
			"stdout": stdout.decode(errors="replace").strip(),
			"stderr": stderr.decode(errors="replace").strip(),
		}

	results: list[dict] = []

	logger.info("Updating dependencies: yt-dlp and ffmpeg")

	# 1) Update yt-dlp in current Python environment.
	try:
		pip_result = await run_cmd(sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp")
		results.append(pip_result)
		if pip_result["exit_code"] == 0:
			logger.info("yt-dlp upgrade succeeded")
		else:
			logger.warning("yt-dlp upgrade failed with exit code %s", pip_result["exit_code"])
	except Exception:
		logger.exception("Unexpected failure running yt-dlp upgrade")
		results.append({
			"cmd": f"{sys.executable} -m pip install --upgrade yt-dlp",
			"exit_code": -1,
			"stdout": "",
			"stderr": "Unexpected exception while upgrading yt-dlp",
		})

	# 2) Update ffmpeg only when apt-get is available and process has root privileges.
	if shutil.which("apt-get"):
		if os.geteuid() == 0:
			try:
				apt_update = await run_cmd("apt-get", "update")
				results.append(apt_update)
				apt_install = await run_cmd("apt-get", "install", "-y", "ffmpeg")
				results.append(apt_install)
				if apt_install["exit_code"] == 0:
					logger.info("ffmpeg upgrade/install succeeded via apt-get")
				else:
					logger.warning("ffmpeg install failed with exit code %s", apt_install["exit_code"])
			except Exception:
				logger.exception("Unexpected failure running apt-get for ffmpeg")
				results.append({
					"cmd": "apt-get install -y ffmpeg",
					"exit_code": -1,
					"stdout": "",
					"stderr": "Unexpected exception while installing ffmpeg",
				})
		else:
			msg = "Skipping ffmpeg apt update: root privileges required"
			logger.warning(msg)
			results.append({
				"cmd": "apt-get install -y ffmpeg",
				"exit_code": -2,
				"stdout": "",
				"stderr": msg,
			})
	else:
		msg = "Skipping ffmpeg apt update: apt-get not available"
		logger.warning(msg)
		results.append({
			"cmd": "apt-get install -y ffmpeg",
			"exit_code": -2,
			"stdout": "",
			"stderr": msg,
		})

	overall_ok = all(step.get("exit_code") == 0 for step in results if step["exit_code"] not in (-2,))
	logger.info("Dependency update complete: success=%s steps=%d", overall_ok, len(results))

	return {
		"status": "success" if overall_ok else "partial",
		"results": results,
	}

@app.post("/api/tasks/scan")
async def trigger_scan():
	"""
	Trigger a scan across all active playlists.
	"""
	logger = app.state.logger
	try:
		task = scan.delay()
		logger.info("Queued scan task %s", task.id)
		return {
			"status": "queued",
			"task_id": task.id,
		}
	except Exception:
		logger.exception("Error triggering scan")
		raise HTTPException(status_code=500, detail="Failed to trigger scan")