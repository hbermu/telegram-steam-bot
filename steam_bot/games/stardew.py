"""Stardew Valley capture discovery.

The single source of truth for the on-disk layout. Nothing else in the package
globs the screenshots directory.

    <root>/<FarmName>-Farm-Screenshots-<GameID>/<Location>/<YY>-<SS>-<DD>.png

Three things the layout gets wrong if you guess it: it lives under
``.local/share`` and not ``.config``; the directory name carries the save's
GameID, so farms sharing a name never collide; and the PNGs sit one
``<Location>`` level deeper than the farm directory.

PNGs loose at the root of a screenshots directory are manual one-off captures
(``<FarmName>_<M-D-YYYY>_<id>.png``); they have no location and no daily
sequence, so they are grouped into one pseudo-series and cannot be animated.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .. import config, source

# root → farm dir → location dir → capture. One scan at this depth answers every
# question below, which is what keeps discovery to a single round trip when the
# captures sit on a console at the far end of an SSH connection.
SCAN_DEPTH = 3

KEY = "stardew"
NAME = "Stardew Valley"
ICON = "🌾"

FARM_DIR_RE = re.compile(r"^(?P<farm>.+)-Farm-Screenshots-(?P<game_id>\d+)$")
CAPTURE_RE = re.compile(r"^(?P<year>\d+)-(?P<season>\d+)-(?P<day>\d+)\.png$", re.I)
LOOSE_RE = re.compile(r"^(?P<farm>.+?)_\d+-\d+-\d+_\d+\.png$", re.I)

# The mod writes seasons 1-based. SaveGameInfo stores spring as 0 — do not mix
# the two numberings.
SEASONS = {1: "spring", 2: "summer", 3: "fall", 4: "winter"}

LOOSE_KEY = "loose"


@dataclass(frozen=True)
class Capture:
    # Remote: this names a file on the console, not on the machine running the
    # bot. It only becomes readable once source.fetch has pulled it down.
    path: PurePosixPath
    year: int
    season: int
    day: int
    mtime: float
    size: int = 0

    @property
    def sort_key(self) -> tuple:
        return (self.year, self.season, self.day, self.mtime)

    @property
    def label(self) -> str:
        if not self.year:
            return self.path.stem
        season = SEASONS.get(self.season, str(self.season))
        return f"Y{self.year} {season} {self.day:02d}"


@dataclass
class Series:
    """One farm as the user thinks of it: a save's captures in one location."""

    game_id: str
    location: str
    farm: str
    dirs: list[PurePosixPath] = field(default_factory=list)
    account: str | None = None

    @property
    def key(self) -> str:
        return f"{self.game_id}|{self.location}"

    @property
    def animatable(self) -> bool:
        return self.game_id != LOOSE_KEY

    def title(self) -> str:
        if self.account:
            return f"{self.farm} · {self.account}"
        return self.farm


def _tree(
    roots: list[PurePosixPath] | None = None,
) -> tuple[list[PurePosixPath], dict[PurePosixPath, list[source.Entry]]]:
    """One scan of the capture roots, bucketed by parent directory.

    Everything below reads this instead of touching the source itself, so a
    whole menu render costs one scan — and, behind the source cache, usually
    none at all.
    """
    wanted = list(roots or config.SCREENSHOT_ROOTS)
    children: dict[PurePosixPath, list[source.Entry]] = defaultdict(list)
    for entry in source.active().scan(wanted, SCAN_DEPTH):
        children[entry.path.parent].append(entry)
    for bucket in children.values():
        bucket.sort(key=lambda e: e.name)
    # A root that does not exist, or holds nothing, simply contributes no
    # children — find reports the missing one on stderr and carries on.
    return [r for r in wanted if r in children], children


def steam_accounts() -> dict[str, str]:
    """Map GameID -> Steam persona name.

    Steam Cloud syncs saves but not captures, and ``.config/StardewValley/Saves``
    only holds the account logged in right now, so the per-account copy under
    ``userdata`` is the one that can attribute every GameID. Best effort: an
    unmatched GameID simply comes back unlabelled.
    """
    out: dict[str, str] = {}
    src = source.active()

    account_dirs = [
        e.path
        for e in src.scan([config.STEAM_USERDATA], 1)
        if e.is_dir and e.path != config.STEAM_USERDATA
    ]
    if not account_dirs:
        return out

    # Every account's Saves directory in one scan rather than one call each.
    saves_of = {
        acct: acct / config.STARDEW_APPID / "ac/WinAppDataRoaming/StardewValley/Saves"
        for acct in account_dirs
    }
    found: dict[PurePosixPath, list[source.Entry]] = defaultdict(list)
    for entry in src.scan(list(saves_of.values()), 1):
        found[entry.path.parent].append(entry)

    for acct, saves in saves_of.items():
        entries = [e for e in found.get(saves, []) if e.is_dir]
        if not entries:
            continue
        persona = _persona(acct)
        for save in entries:
            _, _, game_id = save.name.rpartition("_")
            if game_id.isdigit():
                out.setdefault(game_id, persona or acct.name)
    return out


def _persona(user_dir: PurePosixPath) -> str | None:
    text = source.active().read_text(user_dir / "config/localconfig.vdf")
    if text is None:
        return None
    match = re.search(r'"PersonaName"\s+"([^"]*)"', text)
    return match.group(1) if match else None


def list_series(roots: list[PurePosixPath] | None = None) -> list[Series]:
    """Every series across every root, merged and labelled."""
    accounts = steam_accounts()
    found: dict[str, Series] = {}
    present, children = _tree(roots)

    for root in present:
        for farm_dir in children.get(root, []):
            if not farm_dir.is_dir:
                continue
            match = FARM_DIR_RE.match(farm_dir.name)
            if not match:
                continue
            farm = match.group("farm")
            game_id = match.group("game_id")
            for location_dir in children.get(farm_dir.path, []):
                if not location_dir.is_dir:
                    continue
                inside = children.get(location_dir.path, [])
                if not any(CAPTURE_RE.match(f.name) for f in inside):
                    continue
                key = f"{game_id}|{location_dir.name}"
                series = found.get(key)
                if series is None:
                    series = Series(
                        game_id=game_id,
                        location=location_dir.name,
                        farm=farm,
                        account=accounts.get(game_id),
                    )
                    found[key] = series
                series.dirs.append(location_dir.path)

        loose = [
            f for f in children.get(root, [])
            if not f.is_dir and LOOSE_RE.match(f.name)
        ]
        if loose:
            series = found.setdefault(
                f"{LOOSE_KEY}|-",
                Series(game_id=LOOSE_KEY, location="-", farm="Loose captures"),
            )
            series.dirs.append(root)

    return sorted(found.values(), key=lambda s: (s.game_id == LOOSE_KEY, s.farm.lower()))


def find_series(key: str, roots: list[PurePosixPath] | None = None) -> Series | None:
    for series in list_series(roots):
        if series.key == key:
            return series
    return None


def captures(series: Series) -> list[Capture]:
    """A series' captures, chronological.

    When the same in-game day exists in more than one root — which is what co-op
    produces, one image per device — the earliest root wins, so the order of
    ``SCREENSHOT_ROOTS`` decides.
    """
    _, children = _tree()

    if series.game_id == LOOSE_KEY:
        out = []
        for root in series.dirs:
            for entry in children.get(root, []):
                if not entry.is_dir and LOOSE_RE.match(entry.name):
                    out.append(Capture(entry.path, 0, 0, 0, entry.mtime, entry.size))
        return sorted(out, key=lambda c: c.mtime)

    by_day: dict[tuple[int, int, int], Capture] = {}
    for directory in series.dirs:
        for entry in children.get(directory, []):
            match = CAPTURE_RE.match(entry.name)
            if not match:
                continue
            day = (
                int(match.group("year")),
                int(match.group("season")),
                int(match.group("day")),
            )
            if day not in by_day:
                by_day[day] = Capture(entry.path, *day, entry.mtime, entry.size)
    return [by_day[k] for k in sorted(by_day)]


def summary(series: Series) -> str:
    """One line describing a series, for menus and captions."""
    shots = captures(series)
    if not shots:
        return "no captures"
    count = f"{len(shots)} capture" + ("s" if len(shots) != 1 else "")
    if series.game_id == LOOSE_KEY:
        return f"{count}, manual"
    if len(shots) == 1:
        return f"{count} · {shots[0].label}"
    return f"{count} · {shots[0].label} → {shots[-1].label}"


# -- selecting a slice of a series ----------------------------------------
#
# A button has 64 bytes of callback_data to say which captures it means, and the
# bot keeps no conversation state, so the selection travels as a short spec:
#
#     -               every capture
#     y2              the whole of year 2
#     y2s3            fall of year 2
#     1.1.7           one single day
#     1.1.7~2.3.14    an inclusive range between two days
#
# In-game time is this game's own vocabulary, so the whole grammar lives here.
# The bot never builds a spec by hand — it goes through ``drill``, ``day_code``
# and ``span`` — which is what keeps a second game free to define time however
# it likes.

SPEC_ALL = "-"

_DRILL_RE = re.compile(r"^y(\d+)(?:s(\d+))?$")


def season_name(season: int) -> str:
    return SEASONS.get(season, str(season))


def day_code(capture: Capture) -> str:
    """One day, in the compact form specs are written in."""
    return f"{capture.year}.{capture.season}.{capture.day}"


def span(start: str, end: str) -> str:
    """A spec covering everything between two day codes, inclusive."""
    return f"{start}~{end}"


def drill(year: int | None = None, season: int | None = None) -> str:
    """A spec for a whole year, a whole season, or everything."""
    if year is None:
        return SPEC_ALL
    return f"y{year}" + (f"s{season}" if season is not None else "")


def parse_drill(spec: str) -> tuple[int | None, int | None]:
    """Read back what ``drill`` wrote. Anything else reads as "nothing chosen"."""
    match = _DRILL_RE.match(spec or "")
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2)) if match.group(2) else None


def _code(text: str) -> tuple[int, int, int] | None:
    parts = text.split(".")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _code_label(code: tuple[int, int, int]) -> str:
    year, season, day = code
    return f"Y{year} {season_name(season)} {day:02d}"


def select(shots: list[Capture], spec: str) -> list[Capture]:
    """The captures a spec names, chronological.

    An unreadable spec selects nothing rather than raising: buttons outlive the
    message they were sent in, so one tapped after its captures were deleted
    should be a polite no-op, not a traceback in the log.
    """
    if not spec or spec == SPEC_ALL:
        return list(shots)

    year, season = parse_drill(spec)
    if year is not None:
        return [
            c for c in shots
            if c.year == year and (season is None or c.season == season)
        ]

    start_text, _, end_text = spec.partition("~")
    start = _code(start_text)
    if start is None:
        return []
    if not end_text:
        return [c for c in shots if (c.year, c.season, c.day) == start]
    end = _code(end_text)
    if end is None:
        return []
    if start > end:
        start, end = end, start
    return [c for c in shots if start <= (c.year, c.season, c.day) <= end]


def spec_label(spec: str) -> str:
    """How a spec reads in a menu or a caption."""
    if not spec or spec == SPEC_ALL:
        return "everything"
    year, season = parse_drill(spec)
    if year is not None:
        if season is None:
            return f"Year {year}"
        return f"Y{year} {season_name(season)}"
    start_text, _, end_text = spec.partition("~")
    start = _code(start_text)
    if start is None:
        return spec
    end = _code(end_text) if end_text else None
    if end is None:
        return _code_label(start)
    if start > end:
        start, end = end, start
    return f"{_code_label(start)} → {_code_label(end)}"


def years(shots: list[Capture]) -> list[int]:
    return sorted({c.year for c in shots if c.year})


def seasons(shots: list[Capture], year: int) -> list[int]:
    return sorted({c.season for c in shots if c.year == year})


def days(shots: list[Capture], year: int, season: int) -> list[int]:
    return sorted({c.day for c in shots if c.year == year and c.season == season})
