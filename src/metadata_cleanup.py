from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path


REQUIRED_FIELDS = ("title", "artist", "album", "album_artist", "track")
NOISY_FIELDS = ("description", "synopsis", "purl")
REPLAY_FIELDS = ("replaygain_track_gain", "replaygain_track_peak")


@lru_cache(maxsize=1)
def _load_db_path() -> Path | None:
	homedir = Path(__file__).parent.parent.resolve()
	config_path = homedir / "config" / "app_config.json"
	if not config_path.exists():
		return None

	try:
		config = json.loads(config_path.read_text(encoding="utf-8"))
	except Exception:
		return None

	current = config.get("current")
	if not isinstance(current, str) or current not in config:
		return None

	database_path = config[current].get("database_path")
	if not isinstance(database_path, str) or not database_path.strip():
		return None

	return homedir / Path(database_path)


def analyze_replaygain(audio_file: Path) -> dict[str, str] | None:
	cmd = [
		"ffmpeg",
		"-v",
		"info",
		"-i",
		str(audio_file),
		"-af",
		"replaygain",
		"-f",
		"null",
		"-",
	]
	result = subprocess.run(cmd, capture_output=True, text=True, check=False)
	if result.returncode != 0:
		raise RuntimeError(result.stderr.strip() or f"ffmpeg replaygain analyze failed with exit code {result.returncode}")

	stderr_text = result.stderr or ""
	gain_match = re.search(r"track_gain\s*=\s*([+-]?[0-9]+(?:\.[0-9]+)?)\s*dB", stderr_text)
	peak_match = re.search(r"track_peak\s*=\s*([0-9]+(?:\.[0-9]+)?)", stderr_text)

	if not gain_match or not peak_match:
		return None

	gain_db = float(gain_match.group(1))
	peak = float(peak_match.group(1))
	return {
		"replaygain_track_gain": f"{gain_db:+.2f} dB",
		"replaygain_track_peak": f"{peak:.6f}",
	}


def _resolve_playlist_name_from_db(playlist_folder: Path) -> str | None:
	if not playlist_folder.parent:
		return None

	owner = playlist_folder.parent.name
	playlist_id = playlist_folder.name
	db_path = _load_db_path()
	if db_path is None or not db_path.exists():
		return None

	try:
		with sqlite3.connect(db_path) as db:
			row = db.execute(
				"SELECT name FROM playlist WHERE playlist_id = ? AND owner = ? LIMIT 1",
				(playlist_id, owner),
			).fetchone()
			if row and row[0]:
				return str(row[0]).strip() or None
	except Exception:
		return None

	return None


def ffprobe_tags(audio_file: Path) -> dict[str, str]:
	cmd = [
		"ffprobe",
		"-v",
		"error",
		"-show_entries",
		"format_tags",
		"-of",
		"json",
		str(audio_file),
	]
	result = subprocess.run(cmd, capture_output=True, text=True, check=False)
	if result.returncode != 0:
		raise RuntimeError(result.stderr.strip() or f"ffprobe failed with exit code {result.returncode}")

	payload = json.loads(result.stdout or "{}")
	tags = payload.get("format", {}).get("tags", {})
	if not isinstance(tags, dict):
		return {}
	return {str(key).lower(): str(value) for key, value in tags.items()}


def _normalize(value: str | None) -> str:
	if value is None:
		return ""
	return value.strip()


def _derive_artist(candidate: str) -> str:
	text = candidate.strip()
	if not text:
		return ""
	return re.sub(r"\s+", " ", text)


def _normalize_track(value: str) -> str:
	text = _normalize(value)
	if not text:
		return ""
	main = text.split("/", 1)[0].strip()
	if not main:
		return ""
	if main.isdigit():
		return str(int(main))
	return main


def _extract_video_id(tags: dict[str, str]) -> str | None:
	comment = _normalize(tags.get("comment"))
	if comment:
		match = re.search(r"youtube_id=([A-Za-z0-9_-]{11})", comment)
		if match:
			return match.group(1)
		match = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", comment)
		if match:
			return match.group(1)

	for key in ("purl", "description", "synopsis"):
		value = _normalize(tags.get(key))
		if not value:
			continue
		match = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", value)
		if match:
			return match.group(1)

	return None


def _load_archive_track_index(playlist_folder: Path) -> dict[str, str]:
	archive_index: dict[str, str] = {}

	archive_json = playlist_folder / "archive.json"
	if not archive_json.exists():
		return archive_index

	try:
		payload = json.loads(archive_json.read_text(encoding="utf-8", errors="replace"))
	except Exception:
		return archive_index

	if isinstance(payload, dict):
		for idx, (_, video_id) in enumerate(payload.items(), start=1):
			if isinstance(video_id, str) and video_id and video_id not in archive_index:
				archive_index[video_id] = str(idx)

	return archive_index


def build_target_tags(
	audio_file: Path,
	tags: dict[str, str],
	preferred_track: str | None = None,
	playlist_name: str | None = None,
) -> dict[str, str]:
	existing_title = _normalize(tags.get("title"))
	existing_artist = _normalize(tags.get("artist"))
	existing_album_artist = _normalize(tags.get("album_artist"))
	existing_album = _normalize(tags.get("album"))
	preferred_track_normalized = _normalize_track(preferred_track or "")
	if not preferred_track_normalized:
		raise ValueError("Missing archive.json track index for file")

	title = existing_title or audio_file.stem
	artist = existing_artist or "Unknown Artist"
	album = _normalize(playlist_name) or existing_album or audio_file.parent.name
	album_artist = album
	track = preferred_track_normalized

	target = {
		"title": title,
		"artist": artist,
		"album": album,
		"album_artist": album_artist,
		"track": track,
	}

	for optional_key in ("genre", "date", "disc"):
		value = _normalize(tags.get(optional_key))
		if value:
			target[optional_key] = value

	return target


def _write_tags(audio_file: Path, target_tags: dict[str, str]) -> tuple[bool, dict[str, str], dict[str, str]]:
	current_tags = ffprobe_tags(audio_file)

	changed = False
	for key, value in target_tags.items():
		if _normalize(current_tags.get(key)) != _normalize(value):
			changed = True
			break

	if not changed and not any(_normalize(current_tags.get(field)) for field in NOISY_FIELDS):
		return False, current_tags, current_tags

	with tempfile.NamedTemporaryFile(suffix=".mp3", dir=audio_file.parent, delete=False) as temp_file:
		temp_path = Path(temp_file.name)

	cmd = [
		"ffmpeg",
		"-v",
		"error",
		"-y",
		"-i",
		str(audio_file),
		"-map",
		"0",
		"-c",
		"copy",
		"-id3v2_version",
		"3",
	]

	for key, value in target_tags.items():
		cmd.extend(["-metadata", f"{key}={value}"])

	for noisy_key in NOISY_FIELDS:
		cmd.extend(["-metadata", f"{noisy_key}="])

	cmd.append(str(temp_path))

	result = subprocess.run(cmd, capture_output=True, text=True, check=False)
	if result.returncode != 0:
		try:
			temp_path.unlink(missing_ok=True)
		except Exception:
			pass
		raise RuntimeError(result.stderr.strip() or f"ffmpeg failed with exit code {result.returncode}")

	temp_path.replace(audio_file)
	updated_tags = ffprobe_tags(audio_file)
	return True, current_tags, updated_tags


def _ensure_group_readable(audio_file: Path) -> bool:
	current_mode = audio_file.stat().st_mode & 0o777
	desired_mode = current_mode | 0o644
	if desired_mode == current_mode:
		return False
	audio_file.chmod(desired_mode)
	return True


def cleanup_folder_metadata(
	playlist_folder: Path,
	*,
	recursive: bool = False,
	logger: logging.Logger | None = None,
) -> dict[str, int]:
	active_logger = logger or logging.getLogger("dev")
	playlist_folder = Path(playlist_folder)
	if not playlist_folder.exists():
		return {"updated": 0, "skipped": 0, "failed": 0, "total": 0}

	pattern = "**/*.mp3" if recursive else "*.mp3"
	files = sorted(path for path in playlist_folder.glob(pattern) if path.is_file())

	run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
	active_logger.info(
		"Metadata cleanup start id=%s folder=%s recursive=%s total_files=%d",
		run_id,
		playlist_folder,
		recursive,
		len(files),
	)

	updated = 0
	skipped = 0
	failed = 0
	playlist_name = _resolve_playlist_name_from_db(playlist_folder)
	active_logger.debug(
		"Metadata cleanup playlist folder=%s resolved_name=%r",
		playlist_folder,
		playlist_name,
	)
	archive_track_index = _load_archive_track_index(playlist_folder)
	active_logger.debug(
		"Metadata cleanup archive track index entries=%d folder=%s",
		len(archive_track_index),
		playlist_folder,
	)

	for audio_file in files:
		try:
			tags = ffprobe_tags(audio_file)
			video_id = _extract_video_id(tags)
			preferred_track = archive_track_index.get(video_id) if video_id else None
			target_tags = build_target_tags(
				audio_file,
				tags,
				preferred_track=preferred_track,
				playlist_name=playlist_name,
			)
			did_change, before_tags, after_tags = _write_tags(audio_file, target_tags)

			replay_tags = analyze_replaygain(audio_file)
			if replay_tags:
				did_replay_change, _, replay_after_tags = _write_tags(audio_file, replay_tags)
				if did_replay_change:
					did_change = True
					after_tags = replay_after_tags
					active_logger.debug(
						"ReplayGain tags written id=%s file=%s tags=%s",
						run_id,
						audio_file,
						json.dumps(replay_tags, ensure_ascii=False),
					)
			else:
				active_logger.debug("ReplayGain analysis produced no tags for %s", audio_file)

			perms_fixed = _ensure_group_readable(audio_file)
			if did_change:
				updated += 1
				active_logger.info("Metadata cleanup updated %s", audio_file)
				active_logger.debug(
					"Metadata cleanup file id=%s file=%s before=%s after=%s",
					run_id,
					audio_file,
					json.dumps({key: before_tags.get(key) for key in (*REQUIRED_FIELDS, *NOISY_FIELDS, *REPLAY_FIELDS)}, ensure_ascii=False),
					json.dumps({key: after_tags.get(key) for key in (*REQUIRED_FIELDS, *NOISY_FIELDS, *REPLAY_FIELDS)}, ensure_ascii=False),
				)
			else:
				skipped += 1
				active_logger.debug("Metadata cleanup skipped id=%s file=%s", run_id, audio_file)
			if perms_fixed:
				active_logger.info("Metadata cleanup fixed permissions %s", audio_file)
		except Exception as exc:
			failed += 1
			active_logger.warning("Metadata cleanup failed for %s: %s", audio_file, exc)

	active_logger.info(
		"Metadata cleanup summary id=%s folder=%s updated=%d skipped=%d failed=%d total=%d",
		run_id,
		playlist_folder,
		updated,
		skipped,
		failed,
		len(files),
	)

	return {
		"updated": updated,
		"skipped": skipped,
		"failed": failed,
		"total": len(files),
	}
