"""Where captures are read from: this filesystem, or a console over SSH.

The bot runs in k3s and the captures live on the console, so every path a game
module talks about is a *remote* path. This module is the only place that knows
the difference.

Two things shape the design:

**One ``find`` per scan, never one call per directory.** A per-directory
abstraction would be the obvious shape, but each ``iterdir`` becomes an SSH
round trip and a single menu render walks a dozen directories. Instead a scan
returns the whole tree in one invocation and everything downstream works
against that snapshot in memory. Browsing a menu then costs no SSH at all.

**Paths are never interpolated into a remote shell.** Farm directories carry
apostrophes and non-ASCII (``Baldur's-Farm-…``, ``Villa ñuñi``), which is
exactly the input that breaks naive quoting. ``find`` is given the roots as
argv entries, its output is NUL-terminated, and file transfers go through
rsync's ``--files-from`` rather than a command line.

Standard library only: ``ssh`` and ``rsync`` are invoked as subprocesses. The
package deliberately has no third-party dependencies, and adding paramiko to
save a subprocess would trade that for nothing.
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)

# `ssh` reserves 255 for its own failures — host unreachable, auth refused,
# handshake aborted. Every other code comes from the remote command. That is the
# whole basis for telling "the console is off" apart from "find returned 1".
SSH_FAILURE = 255


class Unreachable(RuntimeError):
    """The console could not be contacted.

    Distinct from any other error on purpose: it is the expected state whenever
    the console is powered off, and the bot reports it as such rather than as a
    breakage.
    """


@dataclass(frozen=True)
class Entry:
    """One node of a scanned tree. Enough to answer every question a game module
    asks without going back to the source."""

    path: PurePosixPath
    is_dir: bool
    mtime: float
    size: int

    @property
    def name(self) -> str:
        return self.path.name


class Source:
    """Read-only access to a capture tree."""

    name = "source"

    def scan(self, roots: list[PurePosixPath], depth: int) -> list[Entry]:
        raise NotImplementedError

    def read_text(self, path: PurePosixPath) -> str | None:
        raise NotImplementedError

    def fetch(self, paths: list[PurePosixPath], dest: Path) -> list[Path]:
        """Materialise remote files under ``dest``, returning the local paths in
        the same order. ffmpeg cannot read over SSH, so every encode starts
        here."""
        raise NotImplementedError

    def run(self, argv: list[str], timeout: int = 30):
        """Run a command wherever the captures are."""
        raise NotImplementedError


class LocalSource(Source):
    """The captures are on this machine. Used by the CLIs and the tests."""

    name = "local"

    def scan(self, roots: list[PurePosixPath], depth: int) -> list[Entry]:
        out: list[Entry] = []
        for root in roots:
            base = Path(root)
            if not base.is_dir():
                continue
            out.extend(self._walk(base, base, depth))
        return out

    def _walk(self, base: Path, current: Path, depth: int) -> list[Entry]:
        out: list[Entry] = []
        if depth < 0:
            return out
        try:
            children = sorted(current.iterdir())
        except OSError:
            return out
        for child in children:
            try:
                stat = child.stat()
            except OSError:
                continue
            is_dir = child.is_dir()
            out.append(
                Entry(PurePosixPath(child), is_dir, stat.st_mtime, stat.st_size)
            )
            if is_dir:
                out.extend(self._walk(base, child, depth - 1))
        return out

    def read_text(self, path: PurePosixPath) -> str | None:
        try:
            return Path(path).read_text(errors="replace")
        except OSError:
            return None

    def fetch(self, paths: list[PurePosixPath], dest: Path) -> list[Path]:
        return [Path(p) for p in paths]

    def run(self, argv: list[str], timeout: int = 30):
        return subprocess.run(argv, capture_output=True, timeout=timeout)


class SSHSource(Source):
    """The captures are on a console reached over SSH.

    The key is a dedicated one and the host key is pinned: ``StrictHostKeyChecking``
    stays on, because a bot that silently accepts a new host key would happily
    hand its session to anything that took the console's address.
    """

    name = "ssh"

    def __init__(
        self,
        host: str,
        user: str,
        key: Path,
        known_hosts: Path,
        connect_timeout: int = 10,
    ):
        self.host = host
        self.user = user
        self.key = _private_key_copy(key)
        self.known_hosts = known_hosts
        self.connect_timeout = connect_timeout

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"

    def _ssh_options(self) -> list[str]:
        return [
            "-i", str(self.key),
            "-o", f"UserKnownHostsFile={self.known_hosts}",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={self.connect_timeout}",
            # The console is a desktop that sleeps; a dead TCP session should
            # surface as an error in seconds, not hang the poller for minutes.
            "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=3",
        ]

    def _run(self, argv: list[str], timeout: int) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run(
                argv, capture_output=True, timeout=timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise Unreachable(f"{self.host} did not answer in {timeout}s") from exc
        except FileNotFoundError as exc:  # ssh/rsync missing from the image
            raise RuntimeError(f"{argv[0]} is not installed in this image") from exc
        if proc.returncode == SSH_FAILURE:
            detail = proc.stderr.decode(errors="replace").strip().splitlines()
            raise Unreachable(detail[-1] if detail else f"cannot reach {self.host}")
        return proc

    def _remote(self, argv: list[str]) -> list[str]:
        """Wrap a remote argv for ssh.

        ssh does not take an argv — it joins whatever it is given with spaces and
        hands the result to a shell on the far side. So the quoting has to happen
        here, or ``\\t`` in a -printf format arrives as a literal ``t`` and an
        apostrophe in a farm name (``Baldur's-Farm-…``) tears the command in half.
        """
        return ["ssh", *self._ssh_options(), self.target, shlex.join(argv)]

    def scan(self, roots: list[PurePosixPath], depth: int) -> list[Entry]:
        # -printf is GNU find; SteamOS is Arch, so it is the stock find. The NUL
        # terminator is what makes apostrophes and spaces in farm names safe.
        proc = self._run(
            self._remote([
                "find", *[str(r) for r in roots],
                "-maxdepth", str(depth),
                "-printf", r"%y\t%T@\t%s\t%p\0",
            ]),
            timeout=60,
        )
        # find exits 1 when a root does not exist and still prints the rest, so a
        # non-zero code is not itself a failure — an empty tree is.
        if proc.returncode not in (0, 1):
            detail = proc.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"find failed on {self.host}: {detail}")
        return _parse_find(proc.stdout)

    def read_text(self, path: PurePosixPath) -> str | None:
        proc = self._run(self._remote(["cat", "--", str(path)]), timeout=30)
        if proc.returncode != 0:
            return None
        return proc.stdout.decode(errors="replace")

    def run(self, argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
        """Run an arbitrary command on the console.

        The key is deliberately not tied to a forced command, so this is the
        general-purpose door — power control today, whatever else later.
        """
        return self._run(self._remote(argv), timeout=timeout)

    def fetch(self, paths: list[PurePosixPath], dest: Path) -> list[Path]:
        """Pull the frames down in one rsync.

        ``--files-from`` reads the list on stdin, so no path ever reaches a
        shell, and one connection covers the whole set — a per-file scp of a
        112-day timelapse would be 112 handshakes.
        """
        if not paths:
            return []
        dest.mkdir(parents=True, exist_ok=True)
        # NUL-separated (--from0), so a newline in a name could not split an
        # entry in two. Paths go in relative to /, which is what makes the
        # landing place below deterministic.
        listing = b"\0".join(str(p).lstrip("/").encode() for p in paths) + b"\0"
        ssh_cmd = shlex.join(["ssh", *self._ssh_options()])
        argv = [
            "rsync", "-e", ssh_cmd,
            "--files-from=-", "--from0", "--times",
            f"{self.target}:/", str(dest),
        ]
        try:
            proc = subprocess.run(
                argv, input=listing, capture_output=True, timeout=1800
            )
        except subprocess.TimeoutExpired as exc:
            raise Unreachable(f"{self.host} stalled during transfer") from exc
        if proc.returncode == 255:
            raise Unreachable(f"lost the connection to {self.host} mid-transfer")
        if proc.returncode != 0:
            detail = proc.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"rsync failed: {detail}")
        return [dest / str(p).lstrip("/") for p in paths]


def _private_key_copy(key: Path) -> Path:
    """Return a path to the key that ssh will actually accept.

    A Kubernetes Secret volume lands as ``root:<fsGroup>``, and there is no mode
    that satisfies both sides: 0400 leaves it unreadable by an unprivileged
    process, and 0440 makes it group-readable, which OpenSSH refuses outright
    ("UNPROTECTED PRIVATE KEY FILE" — its check is ``mode & 077``). Copying it
    to a private 0600 file owned by this process is the way out, and it costs
    one 400-byte read at startup.

    A key that is already private is used where it is, so running the CLIs
    against ``~/.ssh`` does not litter.
    """
    try:
        mode = key.stat().st_mode
    except OSError:
        return key  # let ssh produce the real error later
    if not mode & 0o077:
        return key

    private = Path(tempfile.mkdtemp(prefix="steam-bot-ssh-")) / key.name
    private.write_bytes(key.read_bytes())
    private.chmod(0o600)
    log.info("staged %s as %s so ssh will accept its permissions", key, private)
    return private


def _parse_find(payload: bytes) -> list[Entry]:
    """Read ``-printf '%y\\t%T@\\t%s\\t%p\\0'`` back into entries.

    Anything unparseable is dropped rather than raised on: one odd filename
    should cost its own entry, not the whole listing.
    """
    out: list[Entry] = []
    for chunk in payload.split(b"\0"):
        if not chunk:
            continue
        parts = chunk.decode(errors="replace").split("\t", 3)
        if len(parts) != 4:
            continue
        kind, mtime, size, path = parts
        try:
            out.append(
                Entry(PurePosixPath(path), kind == "d", float(mtime), int(size))
            )
        except ValueError:
            continue
    return out


class CachedSource:
    """A source with a short memory.

    Navigating the menus re-derives the series list on every button press, and
    without this each press would be an SSH round trip. The TTL is deliberately
    short: a capture appears once per in-game day, so a few seconds of staleness
    is invisible, while a minute of it would make a fresh capture look missing.
    """

    def __init__(self, inner: Source, ttl: float):
        self.inner = inner
        self.ttl = ttl
        self._scans: dict[tuple, tuple[float, list[Entry]]] = {}
        self._texts: dict[PurePosixPath, tuple[float, str | None]] = {}

    @property
    def name(self) -> str:
        return self.inner.name

    def scan(self, roots: list[PurePosixPath], depth: int) -> list[Entry]:
        key = (tuple(str(r) for r in roots), depth)
        hit = self._scans.get(key)
        now = time.monotonic()
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        entries = self.inner.scan(roots, depth)
        self._scans[key] = (now, entries)
        return entries

    def read_text(self, path: PurePosixPath) -> str | None:
        hit = self._texts.get(path)
        now = time.monotonic()
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        text = self.inner.read_text(path)
        self._texts[path] = (now, text)
        return text

    def fetch(self, paths: list[PurePosixPath], dest: Path) -> list[Path]:
        return self.inner.fetch(paths, dest)

    def run(self, argv: list[str], timeout: int = 30):
        return self.inner.run(argv, timeout=timeout)

    def invalidate(self):
        self._scans.clear()
        self._texts.clear()


# The process-wide source. A singleton because the cache is worthless if each
# caller builds its own, and because the CLIs and the tests need to swap it for
# a local or a fake one before anything else runs.
_active: CachedSource | None = None


def active() -> CachedSource:
    global _active
    if _active is None:
        from . import config

        _active = config.build_source()
    return _active


def set_active(replacement: CachedSource | None):
    global _active
    _active = replacement


def scratch(dest: Path):
    """Delete a scratch tree, never raising.

    The pod has no persistent storage by design, so every fetched frame has to
    go once its encode is done. Called from a ``finally``, where an exception
    would mask the real one.
    """
    shutil.rmtree(dest, ignore_errors=True)
