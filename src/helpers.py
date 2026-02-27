
# Helper functions for initialization
import logging
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from fastapi import HTTPException
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

METADATA_PIPELINE_VERSION = "2026-02-27.2"

def get_ytdl_opts(root_dir: Path, playlist_folder: bool = True, album_name: str | None = None):
	"""
	Returns a ytdlp opt dictionary for a specified root folder. Root_dir must be a Path object.
	If playlist_folder is True, files for a playlist will be placed into a subfolder named after the playlist.
	Embeds the video id and playlist id into the file metadata (comment tag).
	Writes canonical music metadata for Navidrome compatibility.
	"""
	root_dir = Path(root_dir)
	# Set umask for group writable files (664 files, 775 dirs)
	os.umask(0o002)
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

	album_tag = (album_name or '').strip()
	album_meta_source = album_tag if album_tag else '%(playlist_title,playlist,uploader,channel,creator)s'

	ytdl_opts = {
		'format': 'bestaudio[protocol!=m3u8_native][protocol!=m3u8]/bestaudio/best',
		'outtmpl': outtmpl,

		# Most important for current YouTube/SABR issues
		'extractor_args': {
			'youtube': {
				'player_client': ['default', '-android_sdkless'],
			}
		},

		# Playlist reliability
		'ignoreerrors': True,
		'retries': 5,
		'fragment_retries': 20,
		'continuedl': True,
		'concurrent_fragment_downloads': 4,
		'sleep_interval': 3,
		'max_sleep_interval': 6,
		'cookiefile': 'cookies.txt',
		'writeinfojson': True,
		'writethumbnail': True,
		'parse_metadata': [
			'%(artists,artist,uploader,channel,creator)s:%(meta_artist)s',
			f'{album_meta_source}:%(meta_album)s',
			'%(track,title,fulltitle)s:%(meta_title)s',
			'%(playlist_index,track_number,track)s:%(meta_track)s',
			'%(release_year,release_date,upload_date,year)s:%(meta_date)s',
			'%(genre)s:%(meta_genre)s',
		],

		'postprocessors': [{
			'key': 'FFmpegExtractAudio',
			'preferredcodec': 'mp3',
			'preferredquality': '192',
		}, {
			'key': 'FFmpegMetadata',
		}, {
			'key': 'FFmpegThumbnailsConvertor',
			'format': 'jpg',
		}, {
			'key': 'EmbedThumbnail',
		}],

		# Prefer mapping args to the specific PP
		'postprocessor_args': {
			'FFmpegExtractAudio': [
				'-id3v2_version', '3',
				'-metadata', 'comment=youtube_id=%(id)s; playlist_id=%(playlist_id)s'
			],
			'FFmpegMetadata': [
				'-metadata', 'artist=%(meta_artist)s',
				'-metadata', 'album_artist=%(meta_album)s',
				'-metadata', 'title=%(meta_title)s',
				'-metadata', 'album=%(meta_album)s',
				'-metadata', 'track=%(meta_track)s',
				'-metadata', 'date=%(meta_date)s',
				'-metadata', 'genre=%(meta_genre)s',
				'-metadata', 'description=',
				'-metadata', 'synopsis=',
				'-metadata', 'purl='
			],
			'FFmpegThumbnailsConvertor': [
				'-vf', 'scale=1000:1000:force_original_aspect_ratio=decrease',
				'-q:v', '3',
				'-pix_fmt', 'yuvj420p'
			]
		},
	}

	logging.getLogger("dev").debug(
		"YT-DLP metadata pipeline=%s album_name=%r parse_metadata=%s postprocessors=%s ppa_keys=%s",
		METADATA_PIPELINE_VERSION,
		album_name,
		ytdl_opts.get('parse_metadata'),
		[postprocessor.get('key') for postprocessor in ytdl_opts.get('postprocessors', [])],
		sorted((ytdl_opts.get('postprocessor_args') or {}).keys()),
	)

	return ytdl_opts

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