
# Helper functions for initialization
import logging
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from fastapi import HTTPException

def get_ydl_opts(root_dir: Path, playlist_folder: bool = True):
	"""
	Returns a ytdlp opt dictionary for a specified root folder. Root_dir must be a Path object.
	If playlist_folder is True, files for a playlist will be placed into a subfolder named after the playlist.
	Embeds the video id and playlist id into the file metadata (comment tag).
	"""
	root_dir = Path(root_dir)
	# create directory if missing
	if not root_dir.exists():
		root_dir.mkdir(parents=True, exist_ok=True)

	# Test if we have write permissions
	if not os.access(str(root_dir), mode=os.W_OK):
		raise ValueError(f"Invalid path or no write permission: {root_dir}")

	if playlist_folder:
		# use playlist tokens so ytdlp will put playlist items into a folder named after the playlist
		outtmpl = str(root_dir / '%(playlist_title)s' / '%(playlist_index)s - %(title)s.%(ext)s')
	else:
		outtmpl = str(root_dir / '%(title)s.%(ext)s')

	ydl_opts = {
		'format': 'bestaudio/best',
		'postprocessors': [
			{
				'key': 'FFmpegExtractAudio',
				'preferredcodec': 'mp3',
				'preferredquality': '192',
			},
			{
				'key': 'EmbedThumbnail',
			},
			{
				'key': 'FFmpegMetadata',
			}
		],
		'writethumbnail': True,
		'outtmpl': outtmpl,
		# add ffmpeg args to write a comment tag containing video id and playlist id
		# ytdlp will expand %(id)s and %(playlist_id)s when running postprocessors
		'postprocessor_args': [
			'-metadata', 'comment=youtube_id=%(id)s; playlist_id=%(playlist_id)s'
		],
	}
	return ydl_opts

def validate_playlist_url(url: str) -> bool:
	try:
		parsed = urlparse(url)
		if parsed.scheme not in ("http", "https"):
			return False
		if "youtube.com" not in parsed.netloc:
			return False
		return "list" in parse_qs(parsed.query)
	except Exception:
		return False


def extract_playlist_id(url: str) -> str | None:
	try:
		parsed = urlparse(url)
		return parse_qs(parsed.query).get("list", [None])[0]
	except Exception:
		return None


from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

def check_playlist_accessible(url: str) -> dict:
	"""
	Confirms that:
	- URL refers to a playlist
	- yt-dlp can access it (not private/deleted)
	Returns normalized playlist metadata.
	"""

	# ---- Step 1: Extract playlist_id from URL if present
	parsed = urlparse(url)
	qs = parse_qs(parsed.query)
	url_playlist_id = qs.get("list", [None])[0]

	opts = {
		"quiet": True,
		"skip_download": True,
		"extract_flat": True,
		"noplaylist": False,
		"playlist_items": "1",  # force playlist resolution
	}

	try:
		with YoutubeDL(opts) as ydl:
			info = ydl.extract_info(url, download=False)
			

		if not info:
			raise RuntimeError("No information returned")

		playlist_id = (
			info.get("playlist_id")
			or info.get("id")
			or url_playlist_id
		)

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