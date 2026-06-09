import asyncio
import json
import logging
import random
import shutil
import uuid
from datetime import timedelta
from pathlib import Path

import aiosqlite
from celery import Celery
import redis
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
redis_client = redis.Redis.from_url(f"{redis_base_url}/0", decode_responses=True)

scan_wait_minutes = int(config[config["current"]].get("scan_wait", 12))
scan_wait_extra_maximum_minutes = int(config[config["current"]].get("scan_wait_extra_maximum", 0))
SYNC_BATCH_ACTIVE_KEY = "ytdl:sync_batch:active"
SYNC_BATCH_REMAINING_KEY_PREFIX = "ytdl:sync_batch:remaining:"
SYNC_BATCH_TTL_SECONDS = 24 * 60 * 60
celery.conf.task_routes = {
    "tasks.download_playlist": {"queue": "downloads"}
}
celery.conf.beat_schedule = {
	"schedule-scan-with-jitter": {
		"task": "celery_app.schedule_scan",
		"schedule": timedelta(minutes=scan_wait_minutes),
		"options": {"expires": scan_wait_minutes * 60},
	},
}
 
# Data paths
DATA_ROOT_PATH = homedir / Path(config[config["current"]]["root_dir"])
DB_PATH = homedir / Path(config[config["current"]]["database_path"])
TEMP_BASE_PATH = homedir / Path(config[config["current"]]["temp_dir"])
logger_name = str(config[config["current"]].get("logger_name", "dev"))
logger = logging.getLogger(logger_name)
ARCHIVE_FILE_NAME = "archive.json"

# Scan-Sync process, compressed into a single job because well having them separated doesn't help in this case.
# For celery schedule, create an enqueue_all task that fetches all playlists from database then queues the syncing of all playlists.
# Scan:
# - Fetch remote playlist information
# - Check if local copy exist
# 	- If yes, continue
# 	- If not, create folder and archive.json. Set permissions to 2770
# - Scan local copy to make sure all files are accounted for in archive.json
# 	- If file missing: throw warning, remove from archive.json
# 	- If extra file found: throw warning, remove from main copy
# - Process remote playlist information to a list of target = (title, url). Maintain order.
# - Compare with local copy in archive.json
# - Get diff to find remove and download ids
# - If none, exit
# Sync: 
# - Scan main copy
# - Remove all titles with id in remove list
# - Create a directory in temp with format <user>_<playlist_id>
# - Download all download ids to it with ytdlp
# 	- If download fails, skip and remove from target list
# - Update metadata of ids, all except track index
# - Copy back to main copy. Don't need to finish all playlists, just do it once one playlist is ready for copy.
# - Update archive.json to be the latest version
# - Update main copy playlist item's track index with the correct order as they appear in target.json
# - Remove folder in temp

# Update: Set all year to 2025 so music players don't complain they're not from the same year and put in different folders.

def load_archive_map(playlist_folder: Path) -> dict[str, str]:
	"""
	Load archive mappings from a playlist folder.

	Input:
		playlist_folder: Folder containing archive.json.
	Purpose:
		Read archive.json and return filename -> video_id mappings.
	Output:
		A sanitized mapping of filename to video ID.
	"""
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
	"""
	Persist archive mappings atomically.

	Input:
		playlist_folder: Folder containing archive.json.
		archive_map: filename -> video_id mapping to persist.
	Purpose:
		Write archive.json via a temp file to avoid partial writes.
	Output:
		None.
	"""
	archive_path = playlist_folder / ARCHIVE_FILE_NAME
	temp_path = playlist_folder / ".archive.json.tmp"
	temp_path.write_text(json.dumps(archive_map, ensure_ascii=False, indent=2) + "\n")
	temp_path.replace(archive_path)
	logger.debug("Saved archive map entries=%d to %s", len(archive_map), archive_path)


def _classify_remote_fetch_failure(error: Exception) -> str:
	"""Classify yt-dlp extraction failures into retry/safety categories."""
	message = str(error).lower()
	network_hints = (
		"timed out",
		"timeout",
		"unreachable",
		"connection",
		"network",
		"reset by peer",
		"503",
		"502",
	)
	if any(hint in message for hint in network_hints):
		return "disconnected"
	return "unavailable"


def _sync_batch_remaining_key(batch_id: str) -> str:
	return f"{SYNC_BATCH_REMAINING_KEY_PREFIX}{batch_id}"


def _current_sync_batch_id() -> str | None:
	try:
		batch_id = redis_client.get(SYNC_BATCH_ACTIVE_KEY)
		return str(batch_id) if batch_id else None
	except redis.RedisError:
		logger.exception("Failed to read sync batch lock")
		return None


def _start_sync_batch(batch_id: str) -> bool:
	"""Mark a sync batch as active before queueing per-playlist sync tasks."""
	try:
		return bool(redis_client.set(SYNC_BATCH_ACTIVE_KEY, batch_id, nx=True, ex=SYNC_BATCH_TTL_SECONDS))
	except redis.RedisError:
		logger.exception("Failed to start sync batch batch_id=%s", batch_id)
		return False


def _finish_sync_batch(batch_id: str, task_id: str | None) -> None:
	"""Remove a completed sync task from the current batch and clear the lock when done."""
	if not task_id:
		return
	remaining_key = _sync_batch_remaining_key(batch_id)
	try:
		if redis_client.srem(remaining_key, task_id):
			if redis_client.scard(remaining_key) <= 0:
				redis_client.delete(remaining_key)
				current_batch_id = redis_client.get(SYNC_BATCH_ACTIVE_KEY)
				if current_batch_id == batch_id:
					redis_client.delete(SYNC_BATCH_ACTIVE_KEY)
	except redis.RedisError:
			logger.exception("Failed to finish sync batch batch_id=%s task_id=%s", batch_id, task_id)


def extract_remote_playlist_targets(playlist_url: str) -> dict:
	"""
	Fetch ordered remote playlist targets with trust status.

	Input:
		playlist_url: Source playlist URL.
	Purpose:
		Fetch playlist entries, keep remote order, and report trust status.
	Output:
		Dictionary: {status, targets, reason}.
	"""
	opts = {
		"quiet": True,
		"verbose": False,
		"skip_download": True,
		"extract_flat": "in_playlist",
		"ignoreerrors": False,
		"forceipv4": True,

		# Setting playlist end at 1000 overrides the default of 100. 
		# If you have a playlist that grows beyond 10000 you have other problems.
		"playliststart": 1,
		"playlistend": 10000,
	}
	try:
		# Process remote playlist information to a list of target = (title, url). Maintain order.
		with YoutubeDL(opts) as ydl:
			info = ydl.extract_info(playlist_url, download=False)
	except Exception as e:
		status = _classify_remote_fetch_failure(e)
		logger.warning(
			"Remote playlist fetch failed for %s status=%s reason=%s",
			playlist_url,
			status,
			e,
		)
		return {
			"status": status,
			"targets": [],
			"reason": str(e),
		}

	if info is None:
		logger.warning("Remote playlist fetch returned no info for %s", playlist_url)
		return {
			"status": "unavailable",
			"targets": [],
			"reason": "extract_info returned no info",
		}

	entries = info.get("entries")
	if entries is None:
		logger.warning("Remote playlist info missing entries for %s", playlist_url)
		return {
			"status": "unavailable",
			"targets": [],
			"reason": "playlist entries missing",
		}

	targets: list[dict[str, str]] = []
	for entry in entries:
		if not entry:
			continue
		video_id = entry.get("id")
		if not isinstance(video_id, str) or not video_id:
			continue
		title = str(entry.get("title") or video_id)
		targets.append({
			"id": video_id,
			"title": title,
			"url": f"https://www.youtube.com/watch?v={video_id}",
		})

	logger.debug("Remote playlist targets fetched=%d from %s", len(targets), playlist_url)
	return {
		"status": "ok",
		"targets": targets,
		"reason": None,
	}


def _ensure_playlist_copy(playlist_folder: Path) -> None:
	"""Ensure playlist folder and archive.json exist with expected permissions."""
	playlist_folder.mkdir(parents=True, exist_ok=True)
	try:
		playlist_folder.chmod(0o2770)
	except Exception:
		logger.warning("Failed setting permissions on playlist folder %s", playlist_folder, exc_info=True)

	archive_path = playlist_folder / ARCHIVE_FILE_NAME
	if not archive_path.exists():
		save_archive_map(playlist_folder, {})
		try:
			archive_path.chmod(0o660)
		except Exception:
			logger.warning("Failed setting permissions on archive map %s", archive_path, exc_info=True)


def _reconcile_local_archive(playlist_folder: Path, archive_map: dict[str, str]) -> tuple[dict[str, str], int, int]:
	"""Reconcile archive entries and local media files for a playlist folder."""
	updated_archive = dict(archive_map)
	removed_archive_entries = 0
	removed_extra_files = 0

	for filename in list(updated_archive.keys()):
		file_path = playlist_folder / filename
		if file_path.exists():
			continue
		logger.warning("Archive entry points to missing file, removing from archive.json: %s", file_path)
		del updated_archive[filename]
		removed_archive_entries += 1

	mapped_files = set(updated_archive.keys())
	for media_file in sorted(path for path in playlist_folder.glob("*.mp3") if path.is_file()):
		if media_file.name in mapped_files:
			continue
		logger.warning("Unmapped local file found, deleting from main copy: %s", media_file)
		try:
			media_file.unlink()
			removed_extra_files += 1
		except Exception:
			logger.warning("Failed deleting extra local file %s", media_file, exc_info=True)

	return updated_archive, removed_archive_entries, removed_extra_files

def remove_ids_from_archive_and_disk(
	playlist_folder: Path,
	archive_map: dict[str, str],
	removed_ids: set[str],
) -> tuple[dict[str, str], int, int]:
	"""
	Remove tracks from disk and archive map by video IDs.

	Input:
		playlist_folder: Playlist directory.
		archive_map: Current filename -> video_id mappings.
		removed_ids: Video IDs to remove.
	Purpose:
		Delete mapped media files and delete corresponding archive entries.
	Output:
		(updated_archive, removed_entries_count, removed_files_count)
	"""
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


def download_missing_videos(
	temp_dir: Path,
	missing_targets: list[dict[str, str]],
	album_name: str | None = None,
	suppress_logs: bool = True
) -> set[str]:
	"""
	Download missing tracks to a temporary playlist directory.

	Input:
		temp_dir: Staging directory for downloads.
		missing_targets: Ordered list of target entries ({id,title,url}) to download.
		album_name: Optional album override passed to yt-dlp options.
	Purpose:
		Download only missing remote tracks and track successful IDs.
	Output:
		Set of successfully downloaded video IDs.
	"""
	if not missing_targets:
		return set()

	logger.debug("Downloading missing videos count=%d in %s", len(missing_targets), temp_dir)
	logger.info("Using metadata pipeline version=%s for %s", METADATA_PIPELINE_VERSION, temp_dir)
	options = get_ytdl_opts(temp_dir, playlist_folder=False, album_name=album_name, suppress_logs=suppress_logs)
	successful_ids: set[str] = set()
	with YoutubeDL(options) as ydl:
		for target in missing_targets:
			video_id = target["id"]
			video_url = target["url"]
			try:
				ydl.extract_info(video_url, download=True)
				successful_ids.add(video_id)
				logger.debug("Downloaded missing video id=%s", video_id)
			except Exception:
				logger.warning("Failed downloading video id=%s", video_id, exc_info=True)

	return successful_ids

def _pick_media_file(candidates: list[Path]) -> Path | None:
	"""Pick the best media file candidate for an info file."""
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
	"""
	Merge downloaded info.json files into archive mappings.

	Input:
		playlist_folder: Playlist directory.
		archive_map: Current filename -> video_id mappings.
		expected_ids: Downloaded IDs expected to have info files.
	Purpose:
		Map downloaded files to IDs using info sidecar files.
	Output:
		Updated archive map with added mappings for matched media files.
	"""
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
		prefix = f"{stem}."
		candidates = [
			path for path in playlist_folder.iterdir()
			if path.is_file()
			and path != info_path
			and path.name.startswith(prefix)
			and path.suffix.lower() not in {".json", ".part", ".ytdl"}
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
	"""
	Delete info sidecar files in a playlist directory.

	Input:
		playlist_folder: Playlist directory.
	Purpose:
		Remove *.info.json files after sync finalization.
	Output:
		Number of removed sidecar files.
	"""
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
	remote_target_ids: list[str],
	local_ids_before: set[str],
	playlist_temp_dir: Path,
	successful_download_ids: set[str],
	reconciliation_archive_removed: int,
	reconciliation_files_removed: int,
) -> dict:
	"""Finalize one playlist sync by applying staged files and writing final archive state."""
	playlist_folder = DATA_ROOT_PATH / owner / playlist
	_ensure_playlist_copy(playlist_folder)

	#Scan main copy.
	archive_map = load_archive_map(playlist_folder)
	archive_map, scan_archive_removed, scan_extra_files_removed = _reconcile_local_archive(playlist_folder, archive_map)

	#Remove all titles with id in remove list.
	archive_map, removed_entries, removed_files = remove_ids_from_archive_and_disk(
		playlist_folder,
		archive_map,
		removed_ids,
	)

	#Copy back to main copy.
	if playlist_temp_dir.exists() and any(playlist_temp_dir.iterdir()):
		move_files_to_root(playlist_temp_dir, playlist_folder)
	elif playlist_temp_dir.exists():
		shutil.rmtree(playlist_temp_dir)

	archive_map = merge_archive_with_info_files(playlist_folder, archive_map, successful_download_ids)

	#Update archive.json to be the latest version.
	ordered_archive: dict[str, str] = {}
	for target_id in remote_target_ids:
		for filename, video_id in archive_map.items():
			if video_id != target_id:
				continue
			file_path = playlist_folder / filename
			if not file_path.exists():
				continue
			ordered_archive[filename] = video_id
			break
	save_archive_map(playlist_folder, ordered_archive)

	#Update main copy playlist item's metadata with the correct track number as they appear in target.json.
	cleanup_stats = cleanup_folder_metadata(playlist_folder, recursive=False, logger=logger)

	#Remove folder in temp.
	pruned_info_files = prune_info_json_files(playlist_folder)

	logger.info(
		"Sync complete for %s/%s: remote_targets=%d local_ids_before=%d downloaded=%d cleanup_updated=%d cleanup_failed=%d removed_entries=%d removed_files=%d scan_archive_removed=%d scan_extra_files_removed=%d pre_scan_archive_removed=%d pre_scan_files_removed=%d info_pruned=%d archive_entries=%d",
		owner,
		playlist,
		len(remote_target_ids),
		len(local_ids_before),
		len(successful_download_ids),
		cleanup_stats["updated"],
		cleanup_stats["failed"],
		removed_entries,
		removed_files,
		scan_archive_removed,
		scan_extra_files_removed,
		reconciliation_archive_removed,
		reconciliation_files_removed,
		pruned_info_files,
		len(ordered_archive),
	)

	asyncio.run(_update_playlist_db(owner, playlist))

	return {
		"status": "success",
		"video_count": len(successful_download_ids),
		"removed_ids": len(removed_ids),
		"removed_entries": removed_entries,
		"removed_files": removed_files,
		"missing_ids": len(successful_download_ids),
		"cleanup_updated": cleanup_stats["updated"],
		"cleanup_failed": cleanup_stats["failed"],
		"archive_entries": len(ordered_archive),
	}


async def _update_playlist_db(owner: str, playlist: str):
	"""Mark a playlist active after successful sync finalization."""
	async with aiosqlite.connect(DB_PATH) as db:
		await db.execute(
			"UPDATE playlist SET active = 1 WHERE playlist_id = ? AND owner = ?",
			(playlist, owner),
		)
		await db.commit()


async def _get_playlist_name(owner: str, playlist: str) -> str | None:
	"""Fetch playlist name from DB for metadata tagging."""
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
	"""
	Schedule the enqueue-all task with configured jitter.

	Input:
		None.
	Purpose:
		Apply scan cadence jitter before queueing playlist sync tasks.
	Output:
		Dictionary with scheduling metadata.
	"""
	extra_minutes = max(0, scan_wait_extra_maximum_minutes)
	countdown_seconds = random.randint(0, extra_minutes * 60) if extra_minutes else 0
	active_batch_id = _current_sync_batch_id()
	if active_batch_id:
		logger.info(
			"Skipping enqueue_all because sync batch is still active batch_id=%s countdown_seconds=%d base_minutes=%d extra_max_minutes=%d",
			active_batch_id,
			countdown_seconds,
			scan_wait_minutes,
			extra_minutes,
		)
		return {
			"scheduled": False,
			"reason": "sync_in_progress",
			"batch_id": active_batch_id,
			"countdown_seconds": countdown_seconds,
			"base_minutes": scan_wait_minutes,
			"extra_max_minutes": extra_minutes,
		}
	logger.info(
		"Scheduling enqueue_all with jitter countdown_seconds=%d base_minutes=%d extra_max_minutes=%d",
		countdown_seconds,
		scan_wait_minutes,
		extra_minutes,
	)
	enqueue_all.apply_async(countdown=countdown_seconds)
	return {
		"scheduled": True,
		"countdown_seconds": countdown_seconds,
		"base_minutes": scan_wait_minutes,
		"extra_max_minutes": extra_minutes,
	}


@celery.task(bind=True, max_retries=3)
def sync(self, owner: str, playlist: str, url: str | None = None, batch_id: str | None = None):
	"""
	Sync a single playlist using the scan-sync workflow.

	Input:
		owner: Playlist owner.
		playlist: Playlist ID.
		url: Optional playlist URL override.
	Purpose:
		Fetch remote playlist state, diff against local archive, and apply download/delete sync.
	Output:
		Dictionary summarizing sync actions and cleanup counters.
	"""
	#Fetching remote playlist information.
	playlist_url = url or f"https://www.youtube.com/playlist?list={playlist}"
	playlist_temp_dir = TEMP_BASE_PATH / f"{owner}_{playlist}"
	should_finish_batch = batch_id is not None

	try:
		remote_fetch = extract_remote_playlist_targets(playlist_url)
		remote_status = str(remote_fetch.get("status") or "unavailable")
		if remote_status != "ok":
			return {
				"status": "skipped",
				"mode": "remote_untrusted",
				"owner": owner,
				"playlist": playlist,
				"remote_status": remote_status,
				"skipped_reason": remote_fetch.get("reason") or "Remote state unavailable",
			}

		remote_targets = list(remote_fetch.get("targets") or [])
		
		# Handle disconnections, if remote_targets return nothing
		remote_target_ids = [target["id"] for target in remote_targets]
		remote_id_set = set(remote_target_ids)

		#Check if local copy exist.
		playlist_folder = DATA_ROOT_PATH / owner / playlist
		_ensure_playlist_copy(playlist_folder)

		#Scan local copy to make sure all files are accounted for in archive.json.
		archive_map = load_archive_map(playlist_folder)
		archive_map, reconciliation_archive_removed, reconciliation_files_removed = _reconcile_local_archive(
			playlist_folder,
			archive_map,
		)
		if reconciliation_archive_removed or reconciliation_files_removed:
			save_archive_map(playlist_folder, archive_map)

		#Compare with local copy in archive.json.
		local_ids_before = set(archive_map.values())

		#Get diff to find remove and download ids.
		removed_id_set = local_ids_before - remote_id_set
		missing_ids = remote_id_set - local_ids_before

		#If none, exit.
		if not removed_id_set and not missing_ids:
			return {
				"status": "success",
				"mode": "noop",
				"owner": owner,
				"playlist": playlist,
				"removed_ids": 0,
				"missing_ids": 0,
			}

		#Create a directory in temp with format <user>_<playlist_id>.
		playlist_temp_dir.mkdir(parents=True, exist_ok=True)

		#Download all download ids to it with ytdlp.
		playlist_name = asyncio.run(_get_playlist_name(owner, playlist))
		missing_targets = [target for target in remote_targets if target["id"] in missing_ids]
		suppress_logs = bool(config.get(config.get("current", ""), {}).get("suppress_logs", True))
		print(f"Trying to download {len(missing_targets)} targets")
		successful_download_ids = download_missing_videos(
			playlist_temp_dir,
			missing_targets,
			album_name=playlist_name,
			suppress_logs=suppress_logs,
		)

		#If download fails, skip and remove from target list.
		failed_download_ids = missing_ids - successful_download_ids
		if failed_download_ids:
			logger.warning(
				"Skipping failed downloads from target list for %s/%s ids=%s",
				owner,
				playlist,
				sorted(failed_download_ids),
			)
		remote_target_ids = [video_id for video_id in remote_target_ids if video_id not in failed_download_ids]
		print(f"Finished downloading, trying to clean up metadata for {len(remote_target_ids)} targets")
		return _finalize_playlist_sync(
			owner=owner,
			playlist=playlist,
			removed_ids=removed_id_set,
			remote_target_ids=remote_target_ids,
			local_ids_before=local_ids_before,
			playlist_temp_dir=playlist_temp_dir,
			successful_download_ids=successful_download_ids,
			reconciliation_archive_removed=reconciliation_archive_removed,
			reconciliation_files_removed=reconciliation_files_removed,
		)
	except Exception as e:
		if self.request.retries < self.max_retries:
			should_finish_batch = False
			raise self.retry(exc=e, countdown=int(random.randint(2, 2) + 3 ** self.request.retries))
		raise
	finally:
		if should_finish_batch:
			_finish_sync_batch(batch_id=batch_id or "", task_id=self.request.id)
		if playlist_temp_dir.exists():
			shutil.rmtree(playlist_temp_dir)


def validate(owner: str, playlist: str) -> dict:
	"""
	Validate local playlist integrity and report issues.

	Input:
		owner: Playlist owner.
		playlist: Playlist ID.
	Purpose:
		Check local files for simple integrity issues.
	Output:
		Dictionary with issue list.
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

	Input:
		None.
	Purpose:
		Remove local folders for playlists/users that are inactive in the DB.
	Output:
		Dictionary with removed playlist count.
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
def enqueue_all(self):
	"""
	Queue sync tasks for all active playlists.

	Input:
		None.
	Purpose:
		Fetch active playlists from DB and enqueue one sync task per playlist.
	Output:
		Dictionary containing queued task count.
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
		batch_id = uuid.uuid4().hex
		if not _start_sync_batch(batch_id):
			logger.info("Skipping enqueue_all because another sync batch is already active batch_id=%s", batch_id)
			return {
				"status": "skipped",
				"reason": "sync_in_progress",
				"queued": 0,
				"playlists": len(rows),
				"mode": "enqueue_all",
			}
		queued = 0
		for row in rows:
			owner = row["owner"]
			playlist_id = row["playlist_id"]
			playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"
			task_id = uuid.uuid4().hex
			remaining_key = _sync_batch_remaining_key(batch_id)
			try:
				redis_client.sadd(remaining_key, task_id)
				sync.apply_async(
					kwargs={
						"owner": owner,
						"playlist": playlist_id,
						"url": playlist_url,
						"batch_id": batch_id,
					},
					task_id=task_id,
				)
			except Exception:
				logger.exception(
					"Failed to enqueue sync task batch_id=%s task_id=%s owner=%s playlist=%s",
					batch_id,
					task_id,
					owner,
					playlist_id,
				)
				try:
					redis_client.srem(remaining_key, task_id)
				except redis.RedisError:
					logger.exception(
						"Failed to remove failed sync task from batch batch_id=%s task_id=%s",
						batch_id,
						task_id,
					)
				continue
			queued += 1

		if queued == 0:
			redis_client.delete(SYNC_BATCH_ACTIVE_KEY)
			redis_client.delete(_sync_batch_remaining_key(batch_id))
			return {
				"status": "success",
				"queued": 0,
				"playlists": len(rows),
				"mode": "enqueue_all",
				"batch_id": batch_id,
			}

		return {
			"status": "success",
			"queued": queued,
			"playlists": len(rows),
			"mode": "enqueue_all",
			"batch_id": batch_id,
		}
	except Exception as e:
		raise self.retry(exc=e, countdown=60)


