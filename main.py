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

from helpers import validate_playlist_url, extract_playlist_id, check_playlist_accessible


cwd = Path(__file__).parent

DATA_ROOT_PATH = Path("/srv/hgst/ytdl/")
DB_PATH = cwd / ".database" / "database.db"

os.makedirs(DATA_ROOT_PATH, exist_ok=True)
os.makedirs(DB_PATH.parent, exist_ok=True)

def init_logger() -> logging.Logger:
	try:
		with open("logger_config.yaml", "r") as f:
			config = yaml.safe_load(f)
		logging.config.dictConfig(config)
		logger = logging.getLogger("dev")
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
		logger.info(f"Playlist table row count: {user_count}")

		cur = await db.execute("SELECT COUNT(*) FROM user")
		user_count = (await cur.fetchone())
		logger.info(f"User table row count: {user_count}")

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
	admin: bool = False,
	db: aiosqlite.Connection = Depends(get_db),
):
	logger = app.state.logger
	try:
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
		logger.exception("Error adding user")
		raise HTTPException(status_code=500, detail="Failed to add user")

@app.delete("/api/user/deactivate/{name}")
async def deactivate_user(name: str, db: aiosqlite.Connection = Depends(get_db)):
	logger = app.state.logger
	try:
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
	name: str | None = None,
	db: aiosqlite.Connection = Depends(get_db),
):
	logger = app.state.logger
	try:
		if not validate_playlist_url(url):
			raise HTTPException(status_code=400, detail="Invalid YouTube playlist URL")

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
		final_name = name or meta["title"]

		# Try insert or reactivate
		try:
			insert_cur = await db.execute(
				"INSERT INTO playlist (playlist_id, name, owner) VALUES (?, ?, ?)",
				(playlist_id, final_name, owner),
			)
			await db.commit()
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
				insert_cur = row
			else:
				raise HTTPException(status_code=409, detail="Playlist already exists")

		return {
			"status": "success",
			"playlist": {
				"id": insert_cur.lastrowid,
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

@app.get("/api/playlist/check_by_url")
async def check_playlist_by_url(
	url: str,
	owner: str,
	db: aiosqlite.Connection = Depends(get_db),
):
	logger = app.state.logger
	try:
		if not validate_playlist_url(url):
			raise HTTPException(status_code=400, detail="Invalid URL")

		playlist_id = extract_playlist_id(url)

		cur = await db.execute(
			"SELECT id, playlist_id, name FROM playlist WHERE playlist_id = ? AND owner = ? AND active = 1",
			(playlist_id, owner),
		)
		row = await cur.fetchone()

		if row:
			return {"exists": True, "playlist": dict(row)}
		return {"exists": False}
	except HTTPException:
		raise
	except Exception:
		logger.exception("Error checking playlist")
		raise HTTPException(status_code=500, detail="Failed to check playlist")
