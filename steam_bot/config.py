"""Tunables and paths. Nothing else in the package reads the environment."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

HOME = Path.home()


def _require(name: str) -> str:
    """Return an environment variable or fail loudly."""
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"required environment variable {name!r} is not set")
    return val.strip()


# -- where the captures are -------------------------------------------------
#
# The bot runs in k3s and the captures stay on the console, so the default is
# SSH. "local" is what the CLIs use when they run on the console itself.
CAPTURE_SOURCE = os.environ.get("CAPTURE_SOURCE", "ssh").strip().lower()

# The console's own home, not this process's. The paths below are remote.
REMOTE_HOME = PurePosixPath(os.environ.get("STEAMMACHINE_HOME", "/home/deck"))

# Always an IP — from inside a pod the resolver is the cluster's, which knows
# nothing about the console.
SSH_HOST = _require("STEAMMACHINE_SSH_HOST")
SSH_USER = os.environ.get("STEAMMACHINE_SSH_USER", "deck")
SSH_KEY = Path(os.environ.get("STEAMMACHINE_SSH_KEY", "/ssh/id_ed25519"))
SSH_KNOWN_HOSTS = Path(
    os.environ.get("STEAMMACHINE_SSH_KNOWN_HOSTS", "/ssh/known_hosts")
)
SSH_CONNECT_TIMEOUT = int(os.environ.get("STEAMMACHINE_SSH_TIMEOUT", "10"))

# -- waking it up -----------------------------------------------------------
#
# Broadcast, never unicast: a suspended NIC answers magic packets but not ARP,
# so the node cannot resolve its MAC and a unicast is dropped before it reaches
# the wire. The broadcast only escapes onto the LAN from the node's network
# namespace, which is why the Deployment runs with hostNetwork.
WOL_MAC = _require("STEAMMACHINE_MAC")
WOL_BROADCAST = os.environ.get("WOL_BROADCAST", "255.255.255.255")
WOL_PORTS = tuple(
    int(p) for p in os.environ.get("WOL_PORTS", "9,7").split(",") if p.strip()
)
WAKE_TIMEOUT = int(os.environ.get("WAKE_TIMEOUT", "60"))


# -- the Steam Deck ---------------------------------------------------------
#
# Power control only: it holds captures of its own, but nothing reads them yet.
# It has no MAC here because it has no Ethernet — only Wi-Fi, which it drops on
# suspend, so there is nothing a magic packet could reach. On the Deck, suspend
# is therefore as irreversible as power off.
DECK_SSH_HOST = _require("STEAMDECK_SSH_HOST")
DECK_SSH_USER = os.environ.get("STEAMDECK_SSH_USER", "deck")
DECK_SSH_KEY = Path(os.environ.get("STEAMDECK_SSH_KEY", "/ssh-deck/id_ed25519"))
DECK_SSH_KNOWN_HOSTS = Path(
    os.environ.get("STEAMDECK_SSH_KNOWN_HOSTS", "/ssh-deck/known_hosts")
)

# Deliberately a list, colon-separated like PATH: a second console's captures
# can be folded in by adding a root, and discovery merges them.
_DEFAULT_ROOT = REMOTE_HOME / ".local/share/StardewValley/Screenshots"
SCREENSHOT_ROOTS = [
    PurePosixPath(p)
    for p in os.environ.get("STARDEW_SCREENSHOTS", str(_DEFAULT_ROOT)).split(":")
    if p
]

STEAM_USERDATA = PurePosixPath(
    os.environ.get("STEAM_USERDATA", str(REMOTE_HOME / ".local/share/Steam/userdata"))
)
STARDEW_APPID = "413150"

# How long a listing may be reused. Long enough that walking a menu costs one
# SSH round trip rather than one per button press, short enough that a capture
# taken mid-session shows up while the user is still looking.
SOURCE_CACHE_TTL = float(os.environ.get("SOURCE_CACHE_TTL", "20"))


# -- Telegram ---------------------------------------------------------------
#
# The bot token. Set TELEGRAM_BOT_TOKEN in the environment, or point
# TELEGRAM_CRED_DIR at a directory containing a file named "token".
CRED_DIR = Path(os.environ.get("TELEGRAM_CRED_DIR", HOME / ".config/telegram-steam-bot"))
TOKEN_FILE = CRED_DIR / "token"

# The Bot API refuses uploads over 50 MB; leave headroom for the multipart framing.
UPLOAD_BUDGET = 45 * 1024 * 1024
# sendPhoto additionally caps at 10 MB and re-encodes to JPEG.
PHOTO_MAX_BYTES = 10 * 1024 * 1024


# -- encoding ---------------------------------------------------------------
#
# These widths are fallbacks: build_mp4 tries the captures' native resolution
# first. Downscaling pixel art with lanczos invents high-frequency detail that
# costs more bits than the crisp original. Never assume smaller is cheaper here.
GIF_WIDTHS = [960, 640, 480]
GIF_FPS = 4
GIF_MAX_FRAMES = 200

VIDEO_WIDTHS = [1920, 1280, 960]
VIDEO_CRF = 20

LOOP_MAX_FRAMES = 200
LOOP_FPS = 4
VIDEO_FPS = 8

# Scratch only. The pod has no persistent storage by design: frames are pulled
# here, encoded, sent and deleted, so this is an emptyDir that dies with the pod.
WORK_DIR = Path(os.environ.get("STEAM_BOT_WORK", "/tmp/steam-bot"))

# A whole in-game year is ~112 captures at ~4 MB each, and every one has to be
# local before ffmpeg can touch it. This caps what a single request may pull, so
# one huge span cannot fill the scratch volume and evict the pod with it.
MAX_FETCH_BYTES = int(os.environ.get("MAX_FETCH_BYTES", str(3 * 1024 * 1024 * 1024)))

# Per chat. Generous for browsing, tight for encodes, which are the expensive ones.
RATE_ACTIONS = 30
RATE_WINDOW = 60
RATE_ENCODES = 5
RATE_ENCODE_WINDOW = 600

POLL_TIMEOUT = 50


def token() -> str:
    """The bot token, from the environment or the shared credentials directory."""
    env = os.environ.get("TELEGRAM_BOT_TOKEN")
    if env:
        return env.strip()
    return TOKEN_FILE.read_text().strip()


def build_source():
    """The capture source this process should use.

    Built once at startup and shared — the cache is only worth anything if every
    caller goes through the same instance.
    """
    from . import source as source_mod

    if CAPTURE_SOURCE == "local":
        inner: source_mod.Source = source_mod.LocalSource()
    else:
        inner = source_mod.SSHSource(
            host=SSH_HOST,
            user=SSH_USER,
            key=SSH_KEY,
            known_hosts=SSH_KNOWN_HOSTS,
            connect_timeout=SSH_CONNECT_TIMEOUT,
        )
    return source_mod.CachedSource(inner, SOURCE_CACHE_TTL)
