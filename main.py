from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from yt_dlp import YoutubeDL

single_ydl_opts = {
    'format': 'bestaudio/best',  # auto-pick best source (usually opus or m4a)
    'postprocessors': [
        {
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',   # options: 128/160/192/256/320
        },
        {
            'key': 'EmbedThumbnail',     # embed thumbnail -> MP3 cover art
        },
        {
            'key': 'FFmpegMetadata',     # write basic tags (title, artist, etc.)
        }
    ],
    'writethumbnail': True,              # download thumbnail automatically
    'outtmpl': '%(title)s.%(ext)s',      # filename format
}


with YoutubeDL(single_ydl_opts) as ydl:
    ydl.download(["https://www.youtube.com/watch?v=dQw4w9WgXcQ"])




'''
Endpoints: 
- add_playlist: Simple interface, to add or remove playlists
- admin: More complex interface to view and CRUD on playlists, configure yt-dlp features, manage cron job schedules
- force_update: trigger an update with parameters
- health_check: Healthcheck (200, 501 + error)

Cron jobs:
- Scan from youtube playlists for changes, update database
- Update local repository
- Read and update remote repository

Integration:
- yt-dlp
- rsync
- ntfy for notifications.
'''