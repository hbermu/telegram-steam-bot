# telegram-steam-bot

A Telegram bot for browsing and exporting Steam game captures (screenshots, timelapses) from remote consoles over SSH.

Currently supports **Stardew Valley** farm captures with an in-game calendar-aware browsing system. Designed to be extensible to other games via a simple module interface.

## Features

- Browse captures by game, farm, year, season, or day via inline Telegram menus
- Generate H.264 timelapses with per-frame date labels
- Wake, suspend, and power off consoles remotely (Wake-on-LAN)
- Multi-console support (Steam Machine + Steam Deck)
- No persistent storage — frames are fetched on demand, encoded, sent, and deleted
- Standard library only — no pip dependencies

## Requirements

- Python 3.13+
- `ffmpeg`, `openssh-client`, `rsync` (installed in the Docker image)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- SSH access to the console(s) holding the captures

## Configuration

All configuration is via environment variables. Required variables will cause a startup failure if not set.

### Required

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token |
| `STEAMMACHINE_SSH_HOST` | IP address of the primary console |
| `STEAMMACHINE_MAC` | MAC address for Wake-on-LAN |
| `STEAMDECK_SSH_HOST` | IP address of the Steam Deck |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `CAPTURE_SOURCE` | `ssh` | `ssh` or `local` (for running on the console itself) |
| `STEAMMACHINE_HOME` | `/home/deck` | Remote user's home directory |
| `STEAMMACHINE_SSH_USER` | `deck` | SSH username |
| `STEAMMACHINE_SSH_KEY` | `/ssh/id_ed25519` | Path to SSH private key |
| `STEAMMACHINE_SSH_KNOWN_HOSTS` | `/ssh/known_hosts` | Path to known_hosts file |
| `STEAMMACHINE_SSH_TIMEOUT` | `10` | SSH connect timeout (seconds) |
| `STEAMDECK_SSH_USER` | `deck` | Steam Deck SSH username |
| `STEAMDECK_SSH_KEY` | `/ssh-deck/id_ed25519` | Steam Deck SSH key path |
| `STEAMDECK_SSH_KNOWN_HOSTS` | `/ssh-deck/known_hosts` | Steam Deck known_hosts |
| `WOL_BROADCAST` | `255.255.255.255` | Broadcast address for magic packets |
| `WOL_PORTS` | `9,7` | Ports for WoL packets (comma-separated) |
| `WAKE_TIMEOUT` | `60` | Seconds to wait for console wake |
| `STARDEW_SCREENSHOTS` | `<HOME>/.local/share/StardewValley/Screenshots` | Colon-separated capture roots |
| `STEAM_BOT_WORK` | `/tmp/steam-bot` | Scratch directory for encoding |
| `MAX_FETCH_BYTES` | `3221225472` (3 GB) | Max bytes to fetch for a single request |
| `SOURCE_CACHE_TTL` | `20` | Seconds to cache directory listings |

## Running

### Docker (recommended)

```bash
docker build -t telegram-steam-bot .
docker run --rm \
  -e TELEGRAM_BOT_TOKEN=your-token \
  -e STEAMMACHINE_SSH_HOST=10.0.0.100 \
  -e STEAMMACHINE_MAC=aa:bb:cc:dd:ee:ff \
  -e STEAMDECK_SSH_HOST=10.0.0.101 \
  -v /path/to/ssh-key:/ssh/id_ed25519:ro \
  -v /path/to/known_hosts:/ssh/known_hosts:ro \
  telegram-steam-bot
```

### Locally

```bash
export TELEGRAM_BOT_TOKEN=your-token
export STEAMMACHINE_SSH_HOST=10.0.0.100
export STEAMMACHINE_MAC=aa:bb:cc:dd:ee:ff
export STEAMDECK_SSH_HOST=10.0.0.101
python3 -m steam_bot
```

### Self-test

```bash
python3 selftest.py
```

Exercises discovery, the date grammar, menu crawl, and encoding without sending anything to users.

## Adding a game

Create a module in `steam_bot/games/` exposing:

- `KEY`, `NAME`, `ICON` — identity
- `list_series()` — all available series (farms, saves, etc.)
- `find_series(key)` — look up one series by key
- `captures(series)` — ordered list of captures in a series
- `summary(series)` — human-readable summary

Register it in `steam_bot/games/__init__.py`. See `stardew.py` for the full interface.

## Architecture

| Module | Responsibility |
|--------|---------------|
| `source.py` | Capture transport (local filesystem or SSH) |
| `games/stardew.py` | Disk layout and in-game calendar |
| `games/__init__.py` | Game registry |
| `telegram.py` | Bot API transport (long poll, uploads, rate limiting) |
| `media.py` | ffmpeg: H.264 and GIF encoding with per-frame labels |
| `bot.py` | Menus, callback routing, encode queue |
| `console.py` | Console power management (WoL, suspend, shutdown) |
| `config.py` | All tunables; the only module that reads the environment |

## Docker image

Pre-built images are published to GitHub Container Registry on every push to `main`:

```bash
docker pull ghcr.io/hbermudez/telegram-steam-bot:main
```

## License

MIT
