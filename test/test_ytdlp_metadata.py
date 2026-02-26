#!/usr/bin/env python3
import argparse
import json
from typing import Any

from yt_dlp import YoutubeDL


METADATA_KEYS = [
    "id",
    "title",
    "uploader",
    "channel",
    "artist",
    "creator",
    "track",
    "album",
    "webpage_url",
    "extractor",
    "extractor_key",
]


def fetch_info(url: str, player_clients: list[str] | None = None) -> dict[str, Any]:
    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "skip_download": True,
    }
    if player_clients:
        ydl_opts["extractor_args"] = {"youtube": {"player_client": player_clients}}

    with YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


def summarize(info: dict[str, Any]) -> dict[str, Any]:
    summary = {key: info.get(key) for key in METADATA_KEYS}
    summary["has_identity_metadata"] = bool(
        info.get("uploader")
        or info.get("channel")
        or info.get("artist")
        or info.get("creator")
    )
    summary["has_music_metadata"] = bool(info.get("track") or info.get("artist") or info.get("album"))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check whether yt-dlp can extract uploader/artist metadata for a URL."
    )
    parser.add_argument("url", help="YouTube video URL to inspect")
    parser.add_argument(
        "--player-client",
        dest="player_clients",
        action="append",
        help="Optional youtube player client, can be repeated (example: --player-client default)",
    )
    parser.add_argument(
        "--raw-json",
        action="store_true",
        help="Print full raw extractor JSON instead of the summary",
    )
    args = parser.parse_args()

    try:
        info = fetch_info(args.url, args.player_clients)
    except Exception as exc:
        print(f"ERROR: failed to extract metadata: {exc}")
        raise SystemExit(1)

    if not info:
        print("ERROR: yt-dlp returned no info")
        raise SystemExit(2)

    if args.raw_json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return

    summary = summarize(info)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if summary["has_identity_metadata"]:
        print("\nRESULT: yt-dlp extracted uploader/artist metadata.")
    else:
        print("\nRESULT: yt-dlp did NOT extract uploader/artist metadata.")


if __name__ == "__main__":
    main()
