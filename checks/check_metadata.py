#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import subprocess
from pathlib import Path


LOGGER = logging.getLogger("check_metadata")

REQUIRED_FIELDS = ("title", "artist", "album", "album_artist", "track")
OPTIONAL_FIELDS = ("genre", "date", "disc")

UNKNOWN_LIKE_VALUES = {
	"unknown",
	"unknown artist",
	"unknown album",
	"various",
	"various artists",
	"n/a",
	"none",
	"null",
	"-",
}


def _normalize(value: str | None) -> str:
	if value is None:
		return ""
	return value.strip()


def _is_unknown_like(value: str | None) -> bool:
	normalized = _normalize(value).lower()
	return normalized in UNKNOWN_LIKE_VALUES


def ffprobe_tags(audio_file: Path) -> dict[str, str]:
	cmd = [
		"ffprobe",
		"-v",
		"error",
		"-show_entries",
		"format_tags",
		"-of",
		"json",
		str(audio_file),
	]
	result = subprocess.run(cmd, capture_output=True, text=True, check=False)
	if result.returncode != 0:
		raise RuntimeError(result.stderr.strip() or f"ffprobe failed with exit code {result.returncode}")

	payload = json.loads(result.stdout or "{}")
	tags = payload.get("format", {}).get("tags", {})
	if not isinstance(tags, dict):
		return {}
	return {str(k).lower(): str(v) for k, v in tags.items()}


def validate_tags(tags: dict[str, str]) -> list[str]:
	issues: list[str] = []
	for field in REQUIRED_FIELDS:
		value = tags.get(field)
		if not _normalize(value):
			issues.append(f"missing required tag: {field}")
			continue
		if _is_unknown_like(value):
			issues.append(f"unknown-like required tag: {field}={value!r}")

	for field in OPTIONAL_FIELDS:
		value = tags.get(field)
		if value is not None and _is_unknown_like(value):
			issues.append(f"unknown-like optional tag: {field}={value!r}")

	return issues


def iter_audio_files(target: Path, recursive: bool) -> list[Path]:
	if target.is_file():
		return [target]
	pattern = "**/*.mp3" if recursive else "*.mp3"
	return sorted(path for path in target.glob(pattern) if path.is_file())


def main() -> int:
	parser = argparse.ArgumentParser(
		description="Manual metadata validator using ffprobe for Navidrome-required tags."
	)
	parser.add_argument("path", help="Path to an MP3 file or folder of MP3 files")
	parser.add_argument(
		"--recursive",
		action="store_true",
		help="When path is a folder, scan recursively",
	)
	parser.add_argument(
		"--verbose",
		action="store_true",
		help="Enable debug logging",
	)
	args = parser.parse_args()

	logging.basicConfig(
		level=logging.DEBUG if args.verbose else logging.INFO,
		format="%(asctime)s %(levelname)s %(name)s: %(message)s",
	)

	target = Path(args.path).expanduser().resolve()
	if not target.exists():
		LOGGER.error("Path does not exist: %s", target)
		return 2

	files = iter_audio_files(target, recursive=args.recursive)
	if not files:
		LOGGER.error("No MP3 files found at %s", target)
		return 2

	LOGGER.info("Checking %d file(s)", len(files))

	failed = 0
	for audio_file in files:
		try:
			tags = ffprobe_tags(audio_file)
			issues = validate_tags(tags)
		except Exception as exc:
			failed += 1
			LOGGER.error("%s -> probe failed: %s", audio_file, exc)
			continue

		if issues:
			failed += 1
			LOGGER.warning("%s -> FAIL", audio_file)
			for issue in issues:
				LOGGER.warning("  - %s", issue)
		else:
			LOGGER.info("%s -> OK", audio_file)

	passed = len(files) - failed
	LOGGER.info("Summary: passed=%d failed=%d total=%d", passed, failed, len(files))
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())