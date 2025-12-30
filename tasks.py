import asyncio
from pathlib import Path
from yt_dlp import YoutubeDL
from celery_app import celery
import aiosqlite

DATA_ROOT_PATH = Path("/srv/hgst/ytdl/")
DB_PATH = Path(".database/database.db")


@celery.task(bind=True, max_retries=3)
def download_playlist(self, playlist_id: str, owner: str, url: str):
    """Download a playlist to the user/playlist folder"""

    user_folder = DATA_ROOT_PATH / owner
    playlist_folder = user_folder / playlist_id
    playlist_folder.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        "outtmpl": str(playlist_folder / "%(title)s.%(ext)s"),
        "ignoreerrors": True,
    }

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_count = len(info["entries"]) if info.get("entries") else 0

        # Update DB with active = True
        async def update_db():
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE playlist SET active = 1 WHERE playlist_id = ? AND owner = ?",
                    (playlist_id, owner),
                )
                await db.commit()

        asyncio.run(update_db())

        return {"status": "success", "video_count": video_count}

    except Exception as e:
        raise self.retry(exc=e, countdown=10)
