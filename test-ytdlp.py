from pathlib import Path
from yt_dlp import YoutubeDL


def main() -> None:
    url = "https://www.youtube.com/watch?v=3triLkS0nq4"
    out_dir = Path("/tmp/ytdlp-test")
    out_dir.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        "format": "bestaudio[protocol!=m3u8_native][protocol!=m3u8]/bestaudio/best",

        # ✅ use cookies exported via Cookie-Editor (Netscape format)
        "cookiefile": "./cookies.txt",

        # ✅ avoid android_sdkless which commonly causes 403s
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "tv"],
            }
        },

        # "postprocessors": [
        #     {
        #         "key": "FFmpegExtractAudio",
        #         "preferredcodec": "mp3",
        #         "preferredquality": "192",
        #     }
        # ],

        "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
        "ignoreerrors": False,
    }

    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    print(f"Saved audio to: {out_dir}")


if __name__ == "__main__":
    main()
