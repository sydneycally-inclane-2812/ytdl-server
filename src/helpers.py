# Helper functions for initialization
import json
import logging
import os
import re
import shutil
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import HTTPException
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

METADATA_PIPELINE_VERSION = "2026-03-01.3"

homedir = Path(__file__).parent.parent.resolve()
with open(homedir / "config" / "app_config.json", "r") as f:
	_app_config = json.load(f)


def _current_runtime_config() -> dict:
	current = _app_config.get("current")
	if isinstance(current, str) and current in _app_config:
		candidate = _app_config[current]
		if isinstance(candidate, dict):
			return candidate
	return {}


def _numeric_or_none(value) -> float | None:
	if value is None:
		return None
	if isinstance(value, bool):
		return None
	if isinstance(value, (int, float)):
		return float(value)
	return None


def _build_audio_filter_chain(trim_silence_seconds) -> str | None:
	trim_seconds = _numeric_or_none(trim_silence_seconds)
	filters: list[str] = []

	if trim_seconds is not None and trim_seconds > 0:
		trim_value = f"{trim_seconds:g}"
		filters.append(
			"silenceremove=start_periods=1:start_duration="
			f"{trim_value}:start_threshold=-50dB:detection=peak"
		)
		filters.append(
			"areverse,silenceremove=start_periods=1:start_duration="
			f"{trim_value}:start_threshold=-50dB:detection=peak,areverse"
		)

	if not filters:
		return None
	return ",".join(filters)


def get_ytdl_opts(temp_dir: Path, playlist_folder: bool = True, album_name: str | None = None):
	"""
	Returns a ytdlp opt dictionary for a specified temp folder. Temp_dir must be a Path object.
	Downloads files to temp_dir.
	"""
	temp_dir = Path(temp_dir)

	os.umask(0o002)

	if not temp_dir.exists():
		temp_dir.mkdir(parents=True, exist_ok=True)

	if not os.access(str(temp_dir), mode=os.W_OK):
		raise ValueError(f"Invalid path or no write permission: {temp_dir}")

	if playlist_folder:
		outtmpl = str(temp_dir / "%(playlist_title)s" / "%(playlist_index)s - %(title)s.%(ext)s")
	else:
		outtmpl = str(temp_dir / "%(title)s.%(ext)s")

	runtime_config = _current_runtime_config()
	sleep_interval = runtime_config.get("sleep_interval", 3)
	max_sleep_interval = runtime_config.get("max_sleep_interval", 6)
	audio_filter_chain = _build_audio_filter_chain(runtime_config.get("postprocessing_trim_silence_seconds"))

	if not isinstance(sleep_interval, (int, float)):
		sleep_interval = 3
	if not isinstance(max_sleep_interval, (int, float)):
		max_sleep_interval = 6

	album_tag = (album_name or "").strip()
	album_meta_source = album_tag if album_tag else "%(playlist_title,playlist,uploader,channel,creator)s"
	cookie_file = str(homedir / "src" / "cookies.txt")

	ytdl_opts = {
		"format": "bestaudio[protocol!=m3u8_native][protocol!=m3u8]/bestaudio/best",
		"outtmpl": outtmpl,
		"extractor_args": {
			"youtube": {
				"player_client": ["default", "-android_sdkless"],
			}
		},
		"ignoreerrors": True,
		"retries": 5,
		"fragment_retries": 20,
		"continuedl": True,
		"concurrent_fragment_downloads": 4,
		"sleep_interval": sleep_interval,
		"max_sleep_interval": max_sleep_interval,
		"cookiefile": cookie_file,
		"writeinfojson": True,
		"writethumbnail": True,
		"parse_metadata": [
			"%(artists,artist,uploader,channel,creator)s:%(meta_artist)s",
			f"{album_meta_source}:%(meta_album)s",
			"%(track,title,fulltitle)s:%(meta_title)s",
			"%(playlist_index,track_number,track)s:%(meta_track)s",
			"%(release_year,release_date,upload_date,year)s:%(meta_date)s",
			"%(genre)s:%(meta_genre)s",
		],
		"postprocessors": [
			{
				"key": "FFmpegExtractAudio",
				"preferredcodec": "mp3",
				"preferredquality": "192",
			},
			{
				"key": "FFmpegMetadata",
			},
			{
				"key": "FFmpegThumbnailsConvertor",
				"format": "jpg",
			},
			{
				"key": "EmbedThumbnail",
			},
		],
		"postprocessor_args": {
			"FFmpegExtractAudio": [
				"-id3v2_version",
				"3",
				"-metadata",
				"comment=youtube_id=%(id)s; playlist_id=%(playlist_id)s",
			],
			"FFmpegMetadata": [
				"-metadata",
				"artist=%(meta_artist)s",
				"-metadata",
				"album_artist=%(meta_album)s",
				"-metadata",
				"title=%(meta_title)s",
				"-metadata",
				"album=%(meta_album)s",
				"-metadata",
				"track=%(meta_track)s",
				"-metadata",
				"date=%(meta_date)s",
				"-metadata",
				"genre=%(meta_genre)s",
				"-metadata",
				"description=",
				"-metadata",
				"synopsis=",
				"-metadata",
				"purl=",
			],
			"FFmpegThumbnailsConvertor": [
				"-vf",
				"scale=1000:1000:force_original_aspect_ratio=decrease",
				"-q:v",
				"3",
				"-pix_fmt",
				"yuvj420p",
			],
		},
	}

	if audio_filter_chain:
		ytdl_opts["postprocessor_args"]["FFmpegExtractAudio"].extend([
			"-af",
			audio_filter_chain,
		])

	logging.getLogger("dev").debug(
		"YT-DLP metadata pipeline=%s album_name=%r parse_metadata=%s postprocessors=%s ppa_keys=%s optional_filter=%r",
		METADATA_PIPELINE_VERSION,
		album_name,
		ytdl_opts.get("parse_metadata"),
		[postprocessor.get("key") for postprocessor in ytdl_opts.get("postprocessors", [])],
		sorted((ytdl_opts.get("postprocessor_args") or {}).keys()),
		audio_filter_chain,
	)

	return ytdl_opts


def move_files_to_root(temp_dir: Path, root_dir: Path):
	"""Move files from temp_dir to root_dir and clear temp_dir."""
	for item in temp_dir.iterdir():
		dest = root_dir / item.name
		if item.is_dir():
			shutil.move(str(item), str(dest))
			# Set permissions for MP3 files in moved directory
			for mp3_file in dest.rglob("*.mp3"):
				mp3_file.chmod(0o644)
		else:
			shutil.copy2(str(item), str(dest))
			# Set permissions if it's an MP3 file
			if dest.suffix.lower() == ".mp3":
				dest.chmod(0o644)
	shutil.rmtree(temp_dir)


def validate_true_playlist_url(url: str) -> str:
	"""
	Verify a playlist URL and return a normalized URL. This standardizes the input and output.

	Scheme is optional, but the playlist must follow:
	www.youtube.com/playlist?list=<PLAYLIST_ID>
	"""
	pattern = re.compile(
		r"^(?:https?://)?(?:www\.)?youtube\.com/playlist\?"
		r"(?:.*&)?list=([A-Za-z0-9_-]+)(?:&.*)?$",
		re.IGNORECASE,
	)
	match = pattern.match(url.strip())
	if not match:
		raise ValueError(f"Invalid YouTube playlist URL {url}")
	playlist_id = match.group(1)
	if len(playlist_id) != 34:
		raise ValueError(f"Invalid Youtube ID length {len(playlist_id)}")
	return f"https://www.youtube.com/playlist?list={playlist_id}"


def check_playlist_accessible(url: str) -> dict:
	"""
	Confirms that:
	- URL refers to a playlist
	- yt-dlp can access it (not private/deleted)
	Returns normalized playlist metadata.
	"""
	parsed = urlparse(url)
	qs = parse_qs(parsed.query)
	url_playlist_id = qs.get("list", [None])[0]

	opts = {
		"quiet": True,
		"skip_download": True,
		"extract_flat": True,
		"noplaylist": False,
		"playlist_items": "1",
	}

	try:
		with YoutubeDL(opts) as ydl:
			info = ydl.extract_info(url, download=False)

		if not info:
			raise RuntimeError("No information returned")

		playlist_id = info.get("playlist_id") or info.get("id") or url_playlist_id
		if not playlist_id:
			raise RuntimeError("URL is not a playlist")

		if info.get("availability") == "private":
			raise RuntimeError("Playlist is private")

		return {
			"playlist_id": playlist_id,
			"title": info.get("title"),
			"count": info.get("playlist_count"),
		}
	except DownloadError as e:
		raise RuntimeError(str(e))
