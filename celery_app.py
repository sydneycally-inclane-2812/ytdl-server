import asyncio
import json
import logging
import shutil
from pathlib import Path

import aiosqlite
from celery import Celery
from yt_dlp import YoutubeDL

from helpers import get_ydl_opts

celery = Celery(
    "ytdl_worker",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/1",
)

celery.conf.task_routes = {
    "tasks.download_playlist": {"queue": "downloads"}
}
# Data paths
DATA_ROOT_PATH = Path("/srv/hgst/ytdl/")
DB_PATH = Path(".database/database.db")

logger = logging.getLogger("dev")
# Data structure: {user}/{playlist_id}/{uploader - title.mp3, archive.txt}

# Periodic task `scan`: List remote IDs (flat playlist) and local files per playlist, diff them, and queue a `sync` task when new/removed items are found. Keep a per-playlist archive.txt for yt-dlp.

# Periodic/Triggered task `validate`: Spot-check local integrity (missing playlist dirs, orphaned files, zero-byte mp3s); if suspicious, queue `scan` for that playlist.

# Triggered task `sync`: Apply deletions from the diff, then run yt-dlp with the playlist archive to fetch new items only. Ignore duplicates. 

# @celery.task(bind=True, max_retries=3)
# def download_playlist(self, playlist_id: str, owner: str, url: str):
#     """Download a playlist to the user/playlist folder"""

#     user_folder = DATA_ROOT_PATH / owner
#     playlist_folder = user_folder / playlist_id
#     playlist_folder.mkdir(parents=True, exist_ok=True)

#     ydl_opts = get_ydl_opts(playlist_folder, playlist_folder=False)
#     ydl_opts["ignoreerrors"] = True

#     try:
#         with YoutubeDL(ydl_opts) as ydl:
#             info = ydl.extract_info(url, download=True)
#             video_count = len(info["entries"]) if info.get("entries") else 0

#         # Update DB with active = True
#         async def update_db():
#             async with aiosqlite.connect(DB_PATH) as db:
#                 await db.execute(
#                     "UPDATE playlist SET active = 1 WHERE playlist_id = ? AND owner = ?",
#                     (playlist_id, owner),
#                 )
#                 await db.commit()

#         asyncio.run(update_db())

#         return {"status": "success", "video_count": video_count}

#     except Exception as e:
#         raise self.retry(exc=e, countdown=10)

# @celery.task(bind=True, max_retries=3)
# def sync(self, owner: str, playlist_id: str, url: str, removed_ids: list[str] | None = None):
#     """
#     Apply deletions from the diff, then run yt-dlp with the playlist archive
#     to fetch new items only. Ignore duplicates.
#     """

#     user_folder = DATA_ROOT_PATH / owner
#     playlist_folder = user_folder / playlist_id
#     playlist_folder.mkdir(parents=True, exist_ok=True)

#     archive_file = playlist_folder / "archive.txt"
#     removed_ids = removed_ids or []

#     try:
#         removed_files = 0
#         removed_archive_entries = 0

#         if removed_ids and archive_file.exists():
#             existing_lines = archive_file.read_text().splitlines()
#             updated_lines = []
#             for line in existing_lines:
#                 if not line.strip():
#                     continue
#                 parts = line.split()
#                 if len(parts) >= 2 and parts[1] in removed_ids:
#                     removed_archive_entries += 1
#                     continue
#                 updated_lines.append(line)
#             archive_file.write_text("\n".join(updated_lines) + ("\n" if updated_lines else ""))

#         if removed_ids:
#             for info_path in playlist_folder.glob("*.info.json"):
#                 try:
#                     info = json.loads(info_path.read_text())
#                 except Exception:
#                     continue
#                 if info.get("id") not in removed_ids:
#                     continue
#                 stem = info_path.stem.replace(".info", "")
#                 for media_path in playlist_folder.glob(f"{stem}.*"):
#                     if media_path == info_path:
#                         continue
#                     try:
#                         media_path.unlink(missing_ok=True)
#                         removed_files += 1
#                     except Exception:
#                         logger.warning("Failed to delete %s", media_path)
#                 try:
#                     info_path.unlink(missing_ok=True)
#                 except Exception:
#                     logger.warning("Failed to delete %s", info_path)

#         ydl_opts = {
#             "format": "bestaudio/best",
#             "postprocessors": [
#                 {
#                     "key": "FFmpegExtractAudio",
#                     "preferredcodec": "mp3",
#                     "preferredquality": "192",
#                 }
#             ],
#             "outtmpl": str(playlist_folder / "%(uploader)s - %(title)s.%(ext)s"),
#             "download_archive": str(archive_file),
#             "ignoreerrors": True,
#             "writeinfojson": True,
#         }

#         with YoutubeDL(ydl_opts) as ydl:
#             info = ydl.extract_info(url, download=True)
#             video_count = len(info.get("entries", [])) if info else 0

#         async def update_db():
#             async with aiosqlite.connect(DB_PATH) as db:
#                 await db.execute(
#                     "UPDATE playlist SET active = 1 WHERE playlist_id = ? AND owner = ?",
#                     (playlist_id, owner),
#                 )
#                 await db.commit()

#         asyncio.run(update_db())

#         return {
#             "status": "success",
#             "video_count": video_count,
#             "removed_ids": len(removed_ids),
#             "removed_archive_entries": removed_archive_entries,
#             "removed_files": removed_files,
#         }
#     except Exception as e:
#         raise self.retry(exc=e, countdown=60)

@celery.task(bind=True, max_retries=3)
def sync(self, owner: str, playlist: str, url: str | None = None, removed_ids: list[str] | None = None):
	"""
	Sync a playlist: apply deletions, then download new items via yt-dlp archive.
	"""
	playlist_folder = DATA_ROOT_PATH / owner / playlist
	playlist_folder.mkdir(parents=True, exist_ok=True)
	archive_file = playlist_folder / "archive.txt"
	removed_ids = removed_ids or []
	playlist_url = url or f"https://www.youtube.com/playlist?list={playlist}"

	try:
		removed_archive_entries = 0
		removed_files = 0

		if removed_ids and archive_file.exists():
			existing_lines = archive_file.read_text().splitlines()
			updated_lines = []
			for line in existing_lines:
				if not line.strip():
					continue
				parts = line.split()
				if len(parts) >= 2 and parts[1] in removed_ids:
					removed_archive_entries += 1
					continue
				updated_lines.append(line)
			archive_file.write_text("\n".join(updated_lines) + ("\n" if updated_lines else ""))

		if removed_ids:
			for info_path in playlist_folder.glob("*.info.json"):
				try:
					info = json.loads(info_path.read_text())
				except Exception:
					continue
				if info.get("id") not in removed_ids:
					continue
				stem = info_path.stem.replace(".info", "")
				for media_path in playlist_folder.glob(f"{stem}.*"):
					if media_path == info_path:
						continue
					try:
						media_path.unlink(missing_ok=True)
						removed_files += 1
					except Exception:
						logger.warning("Failed to delete %s", media_path)
				try:
					info_path.unlink(missing_ok=True)
				except Exception:
					logger.warning("Failed to delete %s", info_path)

		ydl_opts = get_ydl_opts(playlist_folder, playlist_folder=False)
		ydl_opts.update({
			"format": "bestaudio[protocol!=m3u8_native][protocol!=m3u8]/bestaudio/best",

			"extractor_args": {
				"youtube": {"player_client": ["default", "-android_sdkless"]}
			},

			"concurrent_fragment_downloads": 1,
			"retries": 10,
			"fragment_retries": 20,
			"sleep_interval": 2,
			"max_sleep_interval": 6,

			# If you export cookies once, add this:
			"cookiefile": "cookies.txt",
		})


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
			"removed_archive_entries": removed_archive_entries,
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
	Scan active playlists, diff remote IDs vs archive, and queue sync tasks.
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
			archive_file = playlist_folder / "archive.txt"

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

			archived_ids = set()
			if archive_file.exists():
				for line in archive_file.read_text().splitlines():
					parts = line.split()
					if len(parts) >= 2:
						archived_ids.add(parts[1])

			removed_ids = list(archived_ids - remote_ids)
			new_ids = remote_ids - archived_ids

			if new_ids or removed_ids:
				sync.delay(owner, playlist_id, playlist_url, removed_ids)
				queued += 1

		return {"status": "success", "queued": queued, "playlists": len(rows)}
	except Exception as e:
		raise self.retry(exc=e, countdown=60)

@celery.task
def update_system():
	'''
	Updates apt and ytdlp to get the latest patch.
	'''
