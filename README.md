# ytdl-server

A yt-dlp wrapper with ffmpeg for automated downloading and organizing of YouTube playlists as MP3 files. Manages multiple users and playlists with automated synchronization.

## Features

- Automated YouTube playlist downloading and conversion to MP3
- Multi-user support with organized file structure
- RESTful API for configuration and control
- Background task processing with Celery workers
- Automatic playlist scanning and synchronization
- Database tracking of downloaded content

## Architecture

### Uvicorn Server
FastAPI application handling:
- REST API endpoints for user interactions
- Worker configuration and job management
- Database operations and state management
- Systemd service integration for production deployment

### Celery Workers
Background task processors executing:
- **Scan**: Compare local database with remote playlist state, detect changes, and queue sync jobs
- **Sync**: Download new content from YouTube playlists and update local database

### File Organization
```
{user}/{playlist}/{songname}.mp3
```

Example:
``` bash
ytdl                                                     # Data root
└── dat                                                  # User name
    ├── PLHCOl0MkeyVlfo1xp75jEdRS*********               # Playlist ID from Youtube
    │   ├── 17さいのうた。 ⧸ 『ユイカ』【MV】.mp3           # Human readable file names
    │   ├── archive.json                                 # Maps filenames back to their respective urls
	... ...
```

## Prerequisites

- Python >= 3.11
- ffmpeg
- Redis (for Celery task queue)
- yt-dlp

## Installation

1. Clone the repository:
```bash
git clone https://github.com/sydneycally-inclane-2812/ytdl-server.git
cd ytdl-server
```

2. Install uv:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

3. Install system dependencies:
```bash
# Debian/Ubuntu
sudo apt install ffmpeg redis

# Arch Linux
sudo pacman -S ffmpeg redis
```

4. Install Python dependencies:
```bash
uv sync
```

5. Configure environment variables:
```bash
cp env.template .env
# Edit .env with your settings
```

6. Configure application:
```bash
# Edit config/app_config.json with your settings
```

## Configuration

- `config/app_config.json`: Main application configuration, database path, Redis URL, etc.
- `config/logger_config.yaml`: Logging configuration
- `.env`: Environment variables (Only passkey for administration for now)
- `config/systemd/`: Systemd service template for production

See [config/README.md](config/README.md) for detailed configuration options.

## Usage

### Development

Start the development server:
```bash
./scripts/dev_startup
```

### Production

1. Set up systemd service:
```bash
sudo cp config/systemd/ytdl-server.service.template /etc/systemd/system/ytdl-server.service
# Edit service file with your paths
sudo systemctl enable ytdl-server
sudo systemctl start ytdl-server
```

2. Or use the production startup script:
```bash
./scripts/prod_startup
# Once up, exit to let it run in the background.
```

### API Endpoints

The server exposes REST endpoints for:
- Managing users and playlists
- Triggering scans and syncs
- Querying download status
- Configuring worker behavior

Access API documentation at `http://localhost:<8081 or your preconfigured port in config/app_config.json>/docs` when running.

## Development

### Project Structure
```
src/          # Main application code
  main.py     # FastAPI application
  celery_app.py  # Celery worker configuration
  helpers.py  # Utility functions
config/       # Configuration files
scripts/      # Startup and utility scripts
test/         # Test files
checks/       # Health check scripts
```

### Running Tests
```bash
python -m pytest test/
```

## Dependencies

- **FastAPI + Uvicorn**: REST API server
- **Celery + Redis**: Distributed task queue
- **yt-dlp**: YouTube content downloading
- **ffmpeg**: Audio conversion
- **aiosqlite**: Async database operations
- **pyyaml**: Configuration parsing

## License

AGPL-3.0
