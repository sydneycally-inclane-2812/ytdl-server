from celery import Celery

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