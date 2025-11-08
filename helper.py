import logging
import os
from pathlib import Path

def get_logger():
	logger = logging.getLogger()
	# Clear any existing handlers first to prevent duplication
	if logger.handlers:
		logger.handlers.clear()
	logger.setLevel(logging.DEBUG)
	# File handler
	file_handler = logging.FileHandler('api_log.txt', mode='a')
	file_handler.setLevel(logging.DEBUG)
	# Console handler
	console_handler = logging.StreamHandler()
	console_handler.setLevel(logging.DEBUG)
	# Formatter for both handlers
	formatter = logging.Formatter('%(asctime)s  %(levelname)s  %(message)s')
	file_handler.setFormatter(formatter)
	console_handler.setFormatter(formatter)
	# Add handlers
	logger.addHandler(file_handler)
	logger.addHandler(console_handler)
	return logger

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