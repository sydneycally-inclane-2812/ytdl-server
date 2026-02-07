from celery import Celery

from helpers import get_ydl_opts

celery = Celery(
    "ytdl_worker",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/1",
)

celery.conf.task_routes = {
    "tasks.download_playlist": {"queue": "downloads"}
}
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
def sync(self, owner: str, playlist: str):
	'''
	Syncs a playlist with the online database.
	'''
	return

def validate(owner: str, playlist: str):
	'''
	Validates a playlist for integrity and corruption
	'''
	return

def sanitize():
	'''
	Clean up playlists that are deactivated or from deactivated accounts
	Scan database for invalid playlists.
	Run deletion on them.
	This must be triggered manually through the API.
	'''
	return

@celery.task(bind=True, max_retries=3)
def scan(self):
	'''
	Scans the database for valid users and valid playlists. 
	Goes through each playlist, call validate(), scan content, scan online for content
	Find diff -> queue a sync job
	'''
