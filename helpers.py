
# Helper functions for initialization
import logging
import os
import sqlite3
from pathlib import Path


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

def init_db(path: Path):
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL DEFAULT 'user',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_url TEXT NOT NULL,
            title TEXT,
            owner_id INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_playlists_owner ON playlists(owner_id);
        CREATE TABLE IF NOT EXISTS playlist_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id INTEGER NOT NULL,
            video_id TEXT NOT NULL,
            title TEXT,
            added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_items_playlist ON playlist_items(playlist_id);
        """)
        conn.commit()
    finally:
        conn.close()