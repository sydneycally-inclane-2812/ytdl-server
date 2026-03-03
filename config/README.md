# Configuration Guide

## app_config.json

Main application configuration file with the following options:

### Directory Settings

- **`root_dir`**: Root directory where downloaded media files are organized by user/playlist structure  
  Example: `"/srv/hgst/ytdl"`

- **`temp_dir`**: Temporary directory for in-progress downloads before moving to final location  
  Example: `"/srv/hgst/ytdl_temp"`

- **`database_path`**: Path to SQLite database file for tracking downloads and playlist state. Can be relative to the project root.  
  Example: `".database/database.db"`

### Service Settings

- **`redis_url`**: Redis server URL for Celery task queue  
  Example: `"redis://localhost:6379"`

- **`port`**: Port number for the FastAPI/Uvicorn server  
  Example: `8080`

### Logging

- **`logging_pattern`**: Logging configuration profile (matches patterns in logger_config.yaml)  
  Example: `"dev"` or `"prod"`

### Worker Behavior

- **`sleep_interval`**: Base sleep time in seconds between worker polling cycles  
  Example: `2`

- **`max_sleep_interval`**: Maximum sleep interval in seconds for exponential backoff. No need to set too high because the postprocess encoding process adds some padding to this
  Example: `4`

- **`scan_wait`**: Wait time in seconds before starting a scan operation. This is to randomize scanning interval to avoid being flagged as bot scraping
  Example: `5`

- **`scan_wait_extra_maximum`**: Maximum additional random wait time in seconds added to scan_wait  
  Example: `1`

### Post-Processing

- **`postprocessing_trim_silence_seconds`**: Number of seconds of silence to trim from start/end of audio files (null to disable)  
  Example: `null` or `2`