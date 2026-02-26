import asyncio
import json
import logging
import shutil
from pathlib import Path
import aiosqlite
from celery import Celery
from yt_dlp import YoutubeDL
import json
from helpers import get_ytdl_opts

celery = Celery(
    "ytdl_worker",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/1",
)

celery.conf.task_routes = {
    "tasks.download_playlist": {"queue": "downloads"}
}
homedir = Path(__file__).parent.parent.resolve()
with open(homedir / "config" / "app_config.json", "r") as f:
	config = json.load(f)
 
# Data paths
DATA_ROOT_PATH = homedir / Path(config[config["current"]]["root_dir"])
DB_PATH = homedir / Path(config[config["current"]]["database_path"])
logger = logging.getLogger("dev")

@celery.task(bind=True, max_retries=3)
def sync(self, owner: str, playlist: str, url: str | None = None, removed_ids: list[str] | None = None):
	"""
	Sync a playlist: apply deletions, then download new items via yt-dlp.
	"""
	playlist_folder = DATA_ROOT_PATH / owner / playlist
	playlist_folder.mkdir(parents=True, exist_ok=True)
	removed_ids = removed_ids or []
	playlist_url = url or f"https://www.youtube.com/playlist?list={playlist}"

	try:
		removed_files = 0
		removed_id_set = set(removed_ids)

		if removed_id_set:
			for info_path in playlist_folder.glob("*.info.json"):
				try:
					info = json.loads(info_path.read_text())
				except json.JSONDecodeError:
					logger.warning("Invalid JSON in info file %s", info_path, exc_info=True)
					continue
				except Exception:
					logger.warning("Cannot read info file %s", info_path, exc_info=True)
					continue
				video_id = info.get("id")
				if not video_id:
					logger.debug("Skipping info file without id: %s", info_path)
					continue
				if video_id not in removed_id_set:
					continue
				stem = info_path.stem.replace(".info", "")
				for media_path in playlist_folder.glob(f"{stem}.*"):
					if media_path == info_path:
						continue
					try:
						media_path.unlink(missing_ok=True)
						removed_files += 1
					except Exception:
						logger.warning("Failed to delete media file %s", media_path, exc_info=True)
				try:
					info_path.unlink(missing_ok=True)
					removed_files += 1
				except Exception:
					logger.warning("Failed to delete info file %s", info_path, exc_info=True)

		if removed_id_set:
			logger.info(
				"Sync cleanup complete for %s/%s: removed_ids=%d, files_removed=%d",
				owner,
				playlist,
				len(removed_id_set),
				removed_files,
			)

		ydl_opts = get_ytdl_opts(playlist_folder, playlist_folder=False)


		with YoutubeDL(ydl_opts) as ydl:
			info = ydl.extract_info(playlist_url, download=True)
			video_count = len(info.get("entries", [])) if info else 0

		async def update_db():
			async with aiosqlite.connect(DB_PATH) as db:
				await db.execute(
					"UPDATE playlist SET active = 1 WHERE playlist_id = ? AND owner = ?",
					(playlist, owner),
				)
				await db.commit()

		asyncio.run(update_db())

		return {
			"status": "success",
			"video_count": video_count,
			"removed_ids": len(removed_ids),
			"removed_files": removed_files,
		}
	except Exception as e:
		raise self.retry(exc=e, countdown=60)

def validate(owner: str, playlist: str) -> dict:
	"""
	Validate local playlist integrity and report issues.
	"""
	playlist_folder = DATA_ROOT_PATH / owner / playlist
	issues: list[dict] = []

	if not playlist_folder.exists():
		issues.append({"issue": "missing_directory"})
		return {"owner": owner, "playlist": playlist, "issues": issues}

	zero_byte_files = [p.name for p in playlist_folder.glob("*.mp3") if p.stat().st_size == 0]
	if zero_byte_files:
		issues.append({"issue": "zero_byte_files", "count": len(zero_byte_files)})

	return {"owner": owner, "playlist": playlist, "issues": issues}

def sanitize() -> dict:
	"""
	Delete local data for inactive playlists or deactivated users.
	"""
	removed = 0

	async def fetch_inactive():
		async with aiosqlite.connect(DB_PATH) as db:
			db.row_factory = aiosqlite.Row
			cur = await db.execute(
				"""
				SELECT p.playlist_id, p.owner
				FROM playlist p
				JOIN user u ON p.owner = u.name
				WHERE p.active = 0 OR u.active = 0
				"""
			)
			return await cur.fetchall()

	rows = asyncio.run(fetch_inactive())
	for row in rows:
		playlist_folder = DATA_ROOT_PATH / row["owner"] / row["playlist_id"]
		if playlist_folder.exists():
			shutil.rmtree(playlist_folder, ignore_errors=True)
			removed += 1

	return {"status": "success", "removed_playlists": removed}

@celery.task(bind=True, max_retries=3)
def scan(self):
	"""
	Scan active playlists, diff remote IDs vs local info.json IDs, and queue sync tasks.
	"""
	try:
		async def fetch_playlists():
			async with aiosqlite.connect(DB_PATH) as db:
				db.row_factory = aiosqlite.Row
				cur = await db.execute(
					"SELECT owner, playlist_id FROM playlist WHERE active = 1"
				)
				return await cur.fetchall()

		rows = asyncio.run(fetch_playlists())
		queued = 0
		for row in rows:
			owner = row["owner"]
			playlist_id = row["playlist_id"]
			playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"

			validation = validate(owner, playlist_id)
			if validation["issues"]:
				logger.info("Validation issues for %s/%s: %s", owner, playlist_id, validation["issues"])

			playlist_folder = DATA_ROOT_PATH / owner / playlist_id
			playlist_folder.mkdir(parents=True, exist_ok=True)

			ydl_opts = {
				"quiet": True,
				"skip_download": True,
				"extract_flat": True,
				"ignoreerrors": True,
			}

			with YoutubeDL(ydl_opts) as ydl:
				info = ydl.extract_info(playlist_url, download=False)
				entries = info.get("entries", []) if info else []
				remote_ids = {entry["id"] for entry in entries if entry and entry.get("id")}

			local_ids = set()
			for info_path in playlist_folder.glob("*.info.json"):
				try:
					info_json = json.loads(info_path.read_text())
				except json.JSONDecodeError:
					logger.warning("Invalid JSON in info file %s", info_path, exc_info=True)
					continue
				except Exception:
					logger.warning("Cannot read info file %s", info_path, exc_info=True)
					continue

				video_id = info_json.get("id")
				if not video_id:
					logger.debug("Skipping info file without id: %s", info_path)
					continue
				local_ids.add(video_id)

			removed_ids = list(local_ids - remote_ids)
			new_ids = remote_ids - local_ids

			if new_ids or removed_ids:
				sync.delay(owner, playlist_id, playlist_url, removed_ids)
				queued += 1

		return {"status": "success", "queued": queued, "playlists": len(rows)}
	except Exception as e:
		raise self.retry(exc=e, countdown=60)


