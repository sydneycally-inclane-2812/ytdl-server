import asyncio
import json
import logging
import random
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path

import aiosqlite
from celery import Celery
from celery.schedules import crontab
from yt_dlp import YoutubeDL

from helpers import METADATA_PIPELINE_VERSION, get_ytdl_opts, move_files_to_root
from metadata_cleanup import cleanup_folder_metadata

homedir = Path(__file__).parent.parent.resolve()
with open(homedir / "config" / "app_config.json", "r") as f:
	config = json.load(f)

redis_base_url = config[config["current"]]["redis_url"].rstrip("/")

celery = Celery(
    "ytdl_worker",
	broker=f"{redis_base_url}/0",
	backend=f"{redis_base_url}/1",
)

scan_wait_minutes = int(config[config["current"]].get("scan_wait", 12))
scan_wait_extra_maximum_minutes = int(config[config["current"]].get("scan_wait_extra_maximum", 0))
celery.conf.task_routes = {
    "tasks.download_playlist": {"queue": "downloads"}
}
celery.conf.beat_schedule = {
	"schedule-scan-with-jitter": {
		"task": "celery_app.schedule_scan",
		"schedule": timedelta(minutes=scan_wait_minutes),
	},
}
 
# Data paths
DATA_ROOT_PATH = homedir / Path(config[config["current"]]["root_dir"])
DB_PATH = homedir / Path(config[config["current"]]["database_path"])
TEMP_BASE_PATH = homedir / Path(config[config["current"]]["temp_dir"])
logger_name = str(config[config["current"]].get("logger_name", "dev"))
logger = logging.getLogger(logger_name)
ARCHIVE_FILE_NAME = "archive.json"

def load_archive_map(playlist_folder: Path) -> dict[str, str]:
	archive_path = playlist_folder / ARCHIVE_FILE_NAME
	if not archive_path.exists():
		logger.debug("No archive map at %s", archive_path)
		return {}

	try:
		raw = json.loads(archive_path.read_text())
	except Exception:
		logger.exception("Failed reading archive map %s", archive_path)
		return {}

	if not isinstance(raw, dict):
		logger.warning("Invalid archive map at %s: expected object", archive_path)
		return {}

	archive_map: dict[str, str] = {}
	for filename, video_id in raw.items():
		if isinstance(filename, str) and isinstance(video_id, str) and filename and video_id:
			archive_map[filename] = video_id
		else:
			logger.debug("Skipping invalid archive entry filename=%r video_id=%r", filename, video_id)

	logger.debug("Loaded archive map entries=%d from %s", len(archive_map), archive_path)
	return archive_map


def save_archive_map(playlist_folder: Path, archive_map: dict[str, str]) -> None:
	archive_path = playlist_folder / ARCHIVE_FILE_NAME
	temp_path = playlist_folder / ".archive.json.tmp"
	temp_path.write_text(json.dumps(archive_map, ensure_ascii=False, indent=2) + "\n")
	temp_path.replace(archive_path)
	logger.debug("Saved archive map entries=%d to %s", len(archive_map), archive_path)


def extract_remote_playlist_ids(playlist_url: str) -> set[str]:
	opts = {
		"quiet": True,
		"skip_download": True,
		"extract_flat": True,
		"ignoreerrors": True,
	}
	with YoutubeDL(opts) as ydl:
		info = ydl.extract_info(playlist_url, download=False)
		entries = info.get("entries", []) if info else []
		remote_ids = {entry["id"] for entry in entries if entry and entry.get("id")}

	logger.debug("Remote playlist ids fetched=%d from %s", len(remote_ids), playlist_url)
	return remote_ids

def remove_ids_from_archive_and_disk(
	playlist_folder: Path,
	archive_map: dict[str, str],
	removed_ids: set[str],
) -> tuple[dict[str, str], int, int]:
	updated_archive = dict(archive_map)
	removed_entries = 0
	removed_files = 0

	if not removed_ids:
		return updated_archive, removed_entries, removed_files

	for filename, video_id in list(archive_map.items()):
		if video_id not in removed_ids:
			continue

		file_path = playlist_folder / filename
		if file_path.exists():
			try:
				file_path.unlink()
				removed_files += 1
				logger.debug("Deleted media file %s for id %s", file_path, video_id)
			except Exception:
				logger.warning("Failed deleting media file %s", file_path, exc_info=True)
		else:
			logger.debug("Mapped file already missing for id %s: %s", video_id, file_path)

		del updated_archive[filename]
		removed_entries += 1

	known_removed_ids = {video_id for video_id in archive_map.values() if video_id in removed_ids}
	if len(known_removed_ids) < len(removed_ids):
		logger.debug(
			"Requested removed ids not present in archive for %s: requested=%d matched=%d",
			playlist_folder,
			len(removed_ids),
			len(known_removed_ids),
		)

	return updated_archive, removed_entries, removed_files


def download_missing_videos(temp_dir: Path, missing_ids: set[str], album_name: str | None = None) -> int:
    if not missing_ids:
        return 0

    logger.debug("Downloading missing videos count=%d in %s", len(missing_ids), temp_dir)
    logger.info("Using metadata pipeline version=%s for %s", METADATA_PIPELINE_VERSION, temp_dir)
    options = get_ytdl_opts(temp_dir, playlist_folder=False, album_name=album_name)  # Pass temp_dir here
    success = 0
    with YoutubeDL(options) as ydl:
        for video_id in sorted(missing_ids):
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            try:
                ydl.extract_info(video_url, download=True)
                success += 1
                logger.debug("Downloaded missing video id=%s", video_id)
            except Exception:
                logger.warning("Failed downloading video id=%s", video_id, exc_info=True)

    return success

def _pick_media_file(candidates: list[Path]) -> Path | None:
	if not candidates:
		return None
	mp3_candidates = [candidate for candidate in candidates if candidate.suffix.lower() == ".mp3"]
	if mp3_candidates:
		return max(mp3_candidates, key=lambda candidate: candidate.stat().st_mtime)
	return max(candidates, key=lambda candidate: candidate.stat().st_mtime)

def merge_archive_with_info_files(
	playlist_folder: Path,
	archive_map: dict[str, str],
	expected_ids: set[str],
) -> dict[str, str]:
	updated_archive = dict(archive_map)
	matched_ids: set[str] = set()

	for info_path in playlist_folder.glob("*.info.json"):
		try:
			info = json.loads(info_path.read_text())
		except Exception:
			logger.warning("Failed parsing info file %s", info_path, exc_info=True)
			continue

		if info.get("_type") not in (None, "video"):
			logger.debug("Skipping non-video info file %s _type=%s", info_path, info.get("_type"))
			continue

		video_id = info.get("id")
		if not isinstance(video_id, str) or not video_id:
			logger.debug("Skipping info file with invalid id: %s", info_path)
			continue
		if expected_ids and video_id not in expected_ids:
			continue

		stem = info_path.stem.replace(".info", "")
		candidates = [
			path for path in playlist_folder.glob(f"{stem}.*")
			if path != info_path and path.suffix.lower() not in {".json", ".part", ".ytdl"}
		]
		selected_media = _pick_media_file(candidates)
		if not selected_media:
			logger.debug("No media file match for info file %s", info_path)
			continue

		updated_archive[selected_media.name] = video_id
		matched_ids.add(video_id)

	if expected_ids:
		unmatched = expected_ids - matched_ids
		if unmatched:
			logger.debug("No info/media mapping found for ids: %s", sorted(unmatched))

	return updated_archive

def prune_info_json_files(playlist_folder: Path) -> int:
	removed = 0
	for info_path in playlist_folder.glob("*.info.json"):
		try:
			info_path.unlink(missing_ok=True)
			removed += 1
		except Exception:
			logger.warning("Failed deleting info file %s", info_path, exc_info=True)

	logger.debug("Pruned info files count=%d in %s", removed, playlist_folder)
	return removed


def _finalize_playlist_sync(
	owner: str,
	playlist: str,
	removed_ids: set[str],
	missing_ids: set[str],
	remote_ids: set[str],
	local_ids_before: set[str],
	playlist_temp_dir: Path,
	downloaded_count: int,
) -> dict:
	playlist_folder = DATA_ROOT_PATH / owner / playlist
	playlist_folder.mkdir(parents=True, exist_ok=True)

	archive_map = load_archive_map(playlist_folder)
	archive_map, removed_entries, removed_files = remove_ids_from_archive_and_disk(
		playlist_folder,
		archive_map,
		removed_ids,
	)

	if playlist_temp_dir.exists() and any(playlist_temp_dir.iterdir()):
		move_files_to_root(playlist_temp_dir, playlist_folder)
	elif playlist_temp_dir.exists():
		shutil.rmtree(playlist_temp_dir)

	archive_map = merge_archive_with_info_files(playlist_folder, archive_map, missing_ids)
	save_archive_map(playlist_folder, archive_map)
	cleanup_stats = {"updated": 0, "skipped": 0, "failed": 0, "total": 0}
	if downloaded_count > 0:
		cleanup_stats = cleanup_folder_metadata(playlist_folder, recursive=False, logger=logger)
	pruned_info_files = prune_info_json_files(playlist_folder)

	logger.info(
		"Sync complete for %s/%s: remote_ids=%d local_ids_before=%d missing=%d downloaded=%d cleanup_updated=%d cleanup_failed=%d removed_entries=%d removed_files=%d info_pruned=%d archive_entries=%d",
		owner,
		playlist,
		len(remote_ids),
		len(local_ids_before),
		len(missing_ids),
		downloaded_count,
		cleanup_stats["updated"],
		cleanup_stats["failed"],
		removed_entries,
		removed_files,
		pruned_info_files,
		len(archive_map),
	)

	asyncio.run(_update_playlist_db(owner, playlist))

	return {
		"status": "success",
		"video_count": downloaded_count,
		"removed_ids": len(removed_ids),
		"removed_entries": removed_entries,
		"removed_files": removed_files,
		"missing_ids": len(missing_ids),
		"cleanup_updated": cleanup_stats["updated"],
		"cleanup_failed": cleanup_stats["failed"],
		"archive_entries": len(archive_map),
	}


async def _update_playlist_db(owner: str, playlist: str):
	async with aiosqlite.connect(DB_PATH) as db:
		await db.execute(
			"UPDATE playlist SET active = 1 WHERE playlist_id = ? AND owner = ?",
			(playlist, owner),
		)
		await db.commit()


async def _get_playlist_name(owner: str, playlist: str) -> str | None:
	async with aiosqlite.connect(DB_PATH) as db:
		db.row_factory = aiosqlite.Row
		cur = await db.execute(
			"SELECT name FROM playlist WHERE playlist_id = ? AND owner = ? LIMIT 1",
			(playlist, owner),
		)
		row = await cur.fetchone()
		if row and row["name"]:
			return str(row["name"])
		return None


@celery.task
def schedule_scan():
	extra_minutes = max(0, scan_wait_extra_maximum_minutes)
	countdown_seconds = random.randint(0, extra_minutes * 60) if extra_minutes else 0
	logger.info(
		"Scheduling scan with jitter countdown_seconds=%d base_minutes=%d extra_max_minutes=%d",
		countdown_seconds,
		scan_wait_minutes,
		extra_minutes,
	)
	scan.apply_async(countdown=countdown_seconds)
	return {
		"scheduled": True,
		"countdown_seconds": countdown_seconds,
		"base_minutes": scan_wait_minutes,
		"extra_max_minutes": extra_minutes,
	}


@celery.task(bind=True, max_retries=3)
def sync(self, owner: str, playlist: str, url: str | None = None, removed_ids: list[str] | None = None):
	"""
	Sync a playlist using archive.json as source of truth.
	Downloads files to a unique temporary directory for the playlist, then moves them to the playlist folder.
	"""
	removed_ids = removed_ids or []
	playlist_url = url or f"https://www.youtube.com/playlist?list={playlist}"
	playlist_temp_dir = TEMP_BASE_PATH / f"{owner}_{playlist}"
	playlist_temp_dir.mkdir(parents=True, exist_ok=True)

	try:
		removed_id_set = set(removed_ids)
		logger.debug("Starting sync for %s/%s with removed_ids=%d", owner, playlist, len(removed_id_set))

		remote_ids = extract_remote_playlist_ids(playlist_url)
		playlist_folder = DATA_ROOT_PATH / owner / playlist
		playlist_folder.mkdir(parents=True, exist_ok=True)
		archive_map = load_archive_map(playlist_folder)
		local_ids_before = set(archive_map.values())
		missing_ids = remote_ids - local_ids_before
		playlist_name = asyncio.run(_get_playlist_name(owner, playlist))
		downloaded_count = download_missing_videos(playlist_temp_dir, missing_ids, album_name=playlist_name)
		return _finalize_playlist_sync(
			owner=owner,
			playlist=playlist,
			removed_ids=removed_id_set,
			missing_ids=missing_ids,
			remote_ids=remote_ids,
			local_ids_before=local_ids_before,
			playlist_temp_dir=playlist_temp_dir,
			downloaded_count=downloaded_count,
		)
	except Exception as e:
		raise self.retry(exc=e, countdown=60)
	finally:
		if playlist_temp_dir.exists():
			shutil.rmtree(playlist_temp_dir)


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
	Scan active playlists, stage all downloads to temp, then finalize all sync updates.
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
		changes: list[dict] = []
		for row in rows:
			owner = row["owner"]
			playlist_id = row["playlist_id"]
			playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"

			validation = validate(owner, playlist_id)
			if validation["issues"]:
				logger.info("Validation issues for %s/%s: %s", owner, playlist_id, validation["issues"])

			playlist_folder = DATA_ROOT_PATH / owner / playlist_id
			playlist_folder.mkdir(parents=True, exist_ok=True)
			archive_map = load_archive_map(playlist_folder)
			local_ids = set(archive_map.values())
			remote_ids = extract_remote_playlist_ids(playlist_url)

			removed_ids = list(local_ids - remote_ids)
			new_ids = remote_ids - local_ids
			logger.debug(
				"Scan diff for %s/%s: local_ids=%d remote_ids=%d new_ids=%d removed_ids=%d",
				owner,
				playlist_id,
				len(local_ids),
				len(remote_ids),
				len(new_ids),
				len(removed_ids),
			)

			if new_ids or removed_ids:
				changes.append({
					"owner": owner,
					"playlist_id": playlist_id,
					"playlist_url": playlist_url,
					"removed_ids": set(removed_ids),
					"missing_ids": set(new_ids),
					"remote_ids": remote_ids,
					"local_ids_before": local_ids,
				})

		if not changes:
			return {"status": "success", "queued": 0, "playlists": len(rows), "mode": "batch"}

		TEMP_BASE_PATH.mkdir(parents=True, exist_ok=True)
		batch_root = Path(tempfile.mkdtemp(prefix="scan_batch_", dir=str(TEMP_BASE_PATH)))
		finalized = 0
		total_downloaded = 0
		try:
			for change in changes:
				playlist_temp_dir = batch_root / change["owner"] / change["playlist_id"]
				playlist_temp_dir.mkdir(parents=True, exist_ok=True)
				playlist_name = asyncio.run(_get_playlist_name(change["owner"], change["playlist_id"]))
				downloaded_count = download_missing_videos(
					playlist_temp_dir,
					change["missing_ids"],
					album_name=playlist_name,
				)
				change["playlist_temp_dir"] = playlist_temp_dir
				change["downloaded_count"] = downloaded_count
				total_downloaded += downloaded_count

			for change in changes:
				_finalize_playlist_sync(
					owner=change["owner"],
					playlist=change["playlist_id"],
					removed_ids=change["removed_ids"],
					missing_ids=change["missing_ids"],
					remote_ids=change["remote_ids"],
					local_ids_before=change["local_ids_before"],
					playlist_temp_dir=change["playlist_temp_dir"],
					downloaded_count=change["downloaded_count"],
				)
				finalized += 1
		finally:
			if batch_root.exists():
				shutil.rmtree(batch_root, ignore_errors=True)

		return {
			"status": "success",
			"queued": finalized,
			"playlists": len(rows),
			"mode": "batch",
			"downloaded": total_downloaded,
		}
	except Exception as e:
		raise self.retry(exc=e, countdown=60)


