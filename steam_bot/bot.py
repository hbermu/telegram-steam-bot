"""Menus, callback routing, the encode queue and the rate limiter.

Navigation state lives entirely in ``callback_data``, so the bot keeps no
conversation state and a restart never strands a half-finished menu.

Every button carries ``<kind>|<game>|<arg>|<series key>``. The series key is the
only field allowed to contain a pipe (it is ``<GameID>|<Location>``), which is
why it goes last and the split stops before it. ``<arg>`` is whatever the kind
needs — usually a spec from the game module naming a slice of the series, ``-``
when there is nothing to say.
"""

from __future__ import annotations

import contextlib
import html
import logging
import queue
import re
import tempfile
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from . import config, console, games, media, source, telegram

log = logging.getLogger(__name__)

# The captures live on the console and nowhere else — there is no copy in the
# cluster on purpose. When it is off, say so plainly instead of reporting an
# error that reads like a fault.
OFFLINE = (
    "🔌 The Steam Machine is not reachable — most likely powered off.\n\n"
    "Captures are read from the console itself, so nothing can be listed "
    "until it is back on."
)

# The one list of commands: what the router dispatches, what /help prints, and
# what gets registered with Telegram at startup. Keeping them in one place is
# what stops the menu in the Telegram UI from advertising something the code
# does not handle — every other text just opens the game menu, so a stale entry
# would silently fall through instead of failing.
#
# Suspend and power off are deliberately NOT commands. They are irreversible on
# at least one console, and a command is one typo away from being sent; they
# stay behind the Consoles menu, which confirms first.
COMMANDS = [
    ("start", "Browse farms and captures", "menu_games"),
    ("games", "Pick a game and a farm", "menu_games"),
    ("latest", "The newest capture, whatever farm it came from", "cmd_latest"),
    ("consoles", "Wake, suspend or power off a console", "menu_consoles"),
    ("status", "Are the consoles up, and how much there is to browse", "cmd_status"),
    ("wake", "Wake the Steam Machine", "cmd_wake"),
    ("help", "What this bot can do", "cmd_help"),
]

HELP = (
    "Farm captures from the Steam Machine, and power control for both consoles."
    "\n\n"
    + "\n".join(f"/{name} — {desc}" for name, desc, _ in COMMANDS)
    + "\n\nFrom a farm you can pull the latest capture, any single day, or a "
    "timelapse of the whole save, one year, one season, or a range you choose "
    "day by day.\n\n"
    "Suspending and powering off live in /consoles rather than in a command, "
    "because they ask before doing something that may need the physical button."
)

# Days sit seven to a row so a season reads like a calendar; years and seasons
# are few enough to line up four across.
DAYS_PER_ROW = 7
DRILL_PER_ROW = 4

# The season path through the timelapse menu: "ss" is "pick a year first",
# "ssy2" is "year 2 chosen, now pick its season". Navigation only — the year
# itself goes straight back to the game module.
SEASON_PATH_RE = re.compile(r"^ss(?:y(\d+))?$")

# Buttons from before the arg field existed. They parse as garbage now, and a
# button that silently does nothing is worse than one that explains itself.
LEGACY_KINDS = {"s", "p", "o", "a", "g", "menu"}


@dataclass
class Job:
    chat_id: int
    message_id: int
    game_key: str
    series_key: str
    spec: str = "-"


def _rows(buttons, per_row):
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


class RateLimiter:
    """Per chat. Browsing is cheap; encodes are what can hurt the console."""

    def __init__(self):
        self._actions: dict[int, deque] = defaultdict(deque)
        self._encodes: dict[int, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _allow(self, bucket, chat_id, limit, window) -> bool:
        now = time.monotonic()
        with self._lock:
            stamps = bucket[chat_id]
            while stamps and now - stamps[0] > window:
                stamps.popleft()
            if len(stamps) >= limit:
                return False
            stamps.append(now)
            return True

    def action(self, chat_id) -> bool:
        return self._allow(
            self._actions, chat_id, config.RATE_ACTIONS, config.RATE_WINDOW
        )

    def encode(self, chat_id) -> bool:
        return self._allow(
            self._encodes, chat_id, config.RATE_ENCODES, config.RATE_ENCODE_WINDOW
        )


class Bot:
    def __init__(self, client: telegram.Client):
        self.tg = client
        self.limits = RateLimiter()
        # One encode at a time, globally. Polling continues on the main thread,
        # so the bot stays responsive while ffmpeg works.
        self.jobs: queue.Queue[Job] = queue.Queue()
        self.worker = threading.Thread(target=self._encode_loop, daemon=True)

    def start(self):
        self.worker.start()
        self.register_commands()

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _esc(text: str) -> str:
        return html.escape(str(text))

    def _series(self, game_key: str, series_key: str):
        game = games.get(game_key)
        if game is None:
            return None, None
        return game, game.find_series(series_key)

    def _show(self, chat_id, message_id, text, markup):
        """Edit the menu in place, or open a new one where that is impossible.

        Menus are also reached from the buttons under a photo, and
        editMessageText cannot touch a message whose body is a caption. Without
        this fallback such a button would log an error and look simply dead.
        """
        if message_id:
            try:
                self.tg.edit_message_text(chat_id, message_id, text, markup)
                return
            except telegram.TelegramError:
                pass
        self.tg.send_message(chat_id, text, markup)

    @staticmethod
    def _pick(game, shots: list, spec: str) -> list:
        """The captures a spec names, for games that can be browsed by date."""
        if not games.browsable(game) or not spec or spec == "-":
            return shots
        return game.select(shots, spec)

    # -- menus ------------------------------------------------------------
    def menu_games(self, chat_id, message_id=None):
        rows = [
            [(f"{g.ICON} {g.NAME}", f"sl|{key}")] for key, g in games.GAMES.items()
        ]
        rows.append([("🎛 Consoles", "cs|-|-|-")])
        self._show(chat_id, message_id, "<b>Pick a game</b>",
                   telegram.keyboard(rows))

    # -- the consoles -----------------------------------------------------

    def _offline(self, chat_id, message_id=None):
        """Report the capture console as unreachable, with a way out."""
        self._show(chat_id, message_id, OFFLINE, telegram.keyboard([
            [("🔌 Wake it", f"wk|{console.CAPTURE_CONSOLE.key}|-|-")],
            [("⬅ Back", "mg|-|-|-")],
        ]))

    def menu_consoles(self, chat_id, message_id=None):
        rows = [
            [(f"{c.icon} {c.name}", f"cn|{c.key}|-|-")]
            for c in console.CONSOLES.values()
        ]
        rows.append([("⬅ Back", "mg|-|-|-")])
        self._show(chat_id, message_id, "<b>Pick a console</b>",
                   telegram.keyboard(rows))

    def menu_console(self, chat_id, message_id, console_key):
        target = console.get(console_key)
        if target is None:
            self.menu_consoles(chat_id, message_id)
            return
        up = target.is_up()
        rows = []
        if up:
            # Suspend is offered plainly only where it can be undone. Where it
            # cannot, it goes through the same confirmation as a power off,
            # because the outcome is the same: someone has to walk over to it.
            rows.append([("😴 Suspend", f"sp|{target.key}|-|-")])
            rows.append([("⏻ Power off", f"pw|{target.key}|-|-")])
            state = "awake"
        else:
            if target.can_wake:
                rows.append([("🔌 Wake it", f"wk|{target.key}|-|-")])
            state = "not answering"
        rows.append([("⬅ Back", "cs|-|-|-")])

        if target.can_wake:
            note = (
                "Suspending is reversible — it comes back in about five "
                "seconds. Powering off is not: Wake-on-LAN does not work from "
                "a full shutdown here, so only the physical button restarts it."
            )
        else:
            note = (
                f"⚠️ It cannot be woken remotely: {target.no_wake_reason}. "
                "So <b>both</b> suspending and powering it off need the "
                "physical button afterwards."
            )
        self._show(
            chat_id, message_id,
            f"<b>{self._esc(target.name)}</b> — {state}.\n\n{note}",
            telegram.keyboard(rows),
        )

    def do_wake(self, chat_id, message_id, console_key):
        target = console.get(console_key)
        if target is None or not target.can_wake:
            self.menu_consoles(chat_id, message_id)
            return
        if target.is_up():
            self.menu_console(chat_id, message_id, console_key)
            return
        self._show(chat_id, message_id,
                   f"🔌 Magic packet sent to the {self._esc(target.name)}. "
                   "Waiting for it to answer…", None)

        def run():
            if target.wake():
                self.tg.send_message(
                    chat_id, f"✅ {target.name} is awake.",
                    telegram.keyboard([[("🎮 Games", "mg|-|-|-")]]))
            else:
                # Deliberately not guessing which of the two it was: from here a
                # console in S5 and one that failed to wake look identical, and
                # inventing a cause would be worse than saying so.
                self.tg.send_message(
                    chat_id,
                    f"❌ No answer after {config.WAKE_TIMEOUT}s. Waking only "
                    "works from suspend, so if it was fully powered off it "
                    "needs the physical button.")

        self._background(run)

    def act_power(self, chat_id, message_id, console_key, verb, confirmed):
        """Suspend or power off, confirming first when it cannot be undone."""
        target = console.get(console_key)
        if target is None:
            self.menu_consoles(chat_id, message_id)
            return
        reversible = verb == "suspend" and target.can_wake

        if not reversible and not confirmed:
            if verb == "suspend":
                warning = (
                    f"😴 <b>Suspend the {self._esc(target.name)}?</b>\n\n"
                    f"It cannot be woken remotely — {target.no_wake_reason} — "
                    "so this needs the physical button afterwards, exactly like "
                    "a power off."
                )
            else:
                warning = (
                    f"⏻ <b>Power off the {self._esc(target.name)} completely?</b>"
                    "\n\nThis cannot be undone from here. It will stay off "
                    "until someone presses the button on it."
                )
            yes = "😴 Yes, suspend it" if verb == "suspend" else "⏻ Yes, power it off"
            self._show(chat_id, message_id, warning, telegram.keyboard([
                [(yes, f"{'spy' if verb == 'suspend' else 'pwy'}|{target.key}|-|-")],
                [("⬅ Cancel", f"cn|{target.key}|-|-")],
            ]))
            return

        getattr(target, "suspend" if verb == "suspend" else "poweroff")()
        if reversible:
            tail = "The 🔌 Wake button brings it back."
            rows = [[("🔌 Wake it", f"wk|{target.key}|-|-")]]
        else:
            tail = "It will need the physical button next time."
            rows = [[("⬅ Back", "cs|-|-|-")]]
        icon = "😴" if verb == "suspend" else "⏻"
        word = "Suspending" if verb == "suspend" else "Powering off"
        self._show(chat_id, message_id,
                   f"{icon} {word} the {self._esc(target.name)}. {tail}",
                   telegram.keyboard(rows))

    def _background(self, fn):
        """Run something slow off the poll loop.

        Waking blocks for up to WAKE_TIMEOUT, and doing that inline would leave
        the bot mute for a minute.
        """
        threading.Thread(target=self._guarded(fn), daemon=True).start()

    @staticmethod
    def _guarded(fn):
        def wrapper():
            try:
                fn()
            except Exception:
                log.exception("background task failed")
        return wrapper

    def menu_series(self, chat_id, message_id, game_key):
        game = games.get(game_key)
        if game is None:
            return
        series = game.list_series()
        if not series:
            self._show(
                chat_id, message_id,
                "No captures yet.\n"
                "The mod writes the first one the morning you leave the house.",
                telegram.keyboard([[("⬅ Games", "mg")]]),
            )
            return
        rows = [
            [(f"{s.title()} · {game.summary(s)}", f"dt|{game_key}|-|{s.key}")]
            for s in series
        ]
        rows.append([("⬅ Games", "mg")])
        self._show(
            chat_id, message_id,
            f"<b>{self._esc(game.NAME)}</b> — pick a farm",
            telegram.keyboard(rows),
        )

    def menu_detail(self, chat_id, message_id, game_key, series_key):
        game, series = self._series(game_key, series_key)
        if series is None:
            self.menu_series(chat_id, message_id, game_key)
            return
        shots = game.captures(series)
        lines = [f"<b>{self._esc(series.farm)}</b>"]
        if series.account:
            lines.append(f"Account: {self._esc(series.account)}")
        lines.append(self._esc(game.summary(series)))

        key = series.key
        buttons = [[
            ("📷 Latest", f"ph|{game_key}|-|{key}"),
            ("🖼 Original", f"og|{game_key}|-|{key}"),
        ]]
        if games.browsable(game) and series.animatable and shots:
            buttons.append([("📅 Pick a day", f"dp|{game_key}|-|{key}")])
        if series.animatable and len(shots) > 1:
            buttons.append([("🎞 Timelapse", f"tl|{game_key}|-|{key}")])
        buttons.append([("⬅ Farms", f"sl|{game_key}")])
        self._show(chat_id, message_id, "\n".join(lines),
                   telegram.keyboard(buttons))

    def _drill_menu(self, chat_id, message_id, game_key, series_key, spec, *,
                    nav_kind, day_kind, day_arg, head_days,
                    nav_arg=None, shots_filter=None, back=None):
        """The year → season → day walk, shared by all three date pickers.

        Picking a single day, the start of a range and its end differ only in
        where a day button leads and which captures are still on the table, so
        the walk is written once. ``nav_arg`` lets a caller thread extra state
        (the range's first day) through the navigation buttons.

        Levels with a single choice collapse: in a series with one year, the
        first tap already shows seasons.
        """
        game, series = self._series(game_key, series_key)
        if series is None or not games.browsable(game):
            self.menu_series(chat_id, message_id, game_key)
            return
        shots = game.captures(series)
        if shots_filter:
            shots = shots_filter(shots)
        if not shots:
            self.menu_detail(chat_id, message_id, game_key, series_key)
            return

        nav_arg = nav_arg or (lambda drill: drill)
        year, season = game.parse_drill(spec)
        present = game.years(shots)
        if year is None and len(present) == 1:
            year = present[0]
        if year is not None and season is None:
            in_year = game.seasons(shots, year)
            if len(in_year) == 1:
                season = in_year[0]

        def nav(label, drill):
            return (label, f"{nav_kind}|{game_key}|{nav_arg(drill)}|{series_key}")

        if year is None:
            rows = _rows(
                [nav(f"Year {y}", game.drill(y)) for y in present], DRILL_PER_ROW
            )
            head = "pick a year"
        elif season is None:
            rows = _rows(
                [nav(game.season_name(s).title(), game.drill(year, s))
                 for s in game.seasons(shots, year)],
                DRILL_PER_ROW,
            )
            rows.append([nav("⬅ Years", game.drill())])
            head = f"Year {year} — pick a season"
        else:
            in_season = [
                c for c in shots if c.year == year and c.season == season
            ]
            rows = _rows(
                [(f"{c.day:02d}",
                  f"{day_kind}|{game_key}|{day_arg(c)}|{series_key}")
                 for c in in_season],
                DAYS_PER_ROW,
            )
            rows.append([nav("⬅ Seasons", game.drill(year))])
            head = f"Y{year} {game.season_name(season)} — {head_days}"

        rows.append(back or [("⬅ Farm", f"dt|{game_key}|-|{series_key}")])
        self._show(
            chat_id, message_id,
            f"<b>{self._esc(series.farm)}</b> — {head}",
            telegram.keyboard(rows),
        )

    def menu_days(self, chat_id, message_id, game_key, series_key, spec):
        game = games.get(game_key)
        if not games.browsable(game):
            self.menu_detail(chat_id, message_id, game_key, series_key)
            return
        self._drill_menu(
            chat_id, message_id, game_key, series_key, spec,
            nav_kind="dp", day_kind="ph",
            day_arg=game.day_code, head_days="pick a day",
        )

    def menu_timelapse(self, chat_id, message_id, game_key, series_key, spec):
        """What to animate: everything, a year, a season, or a chosen range."""
        game, series = self._series(game_key, series_key)
        if series is None or not series.animatable:
            self.menu_detail(chat_id, message_id, game_key, series_key)
            return
        if not games.browsable(game):
            # No calendar for this game: the whole series is the only option.
            self.enqueue_clip(chat_id, message_id, game_key, series_key, "-")
            return
        shots = game.captures(series)

        path = SEASON_PATH_RE.match(spec or "")
        if path and path.group(1):
            year = int(path.group(1))
            rows = _rows(
                [(game.season_name(s).title(),
                  f"go|{game_key}|{game.drill(year, s)}|{series_key}")
                 for s in game.seasons(shots, year)],
                DRILL_PER_ROW,
            )
            rows.append([("⬅ Years", f"tl|{game_key}|ss|{series_key}")])
            head = f"Year {year} — animate which season?"
        elif path:
            rows = _rows(
                [(f"Year {y}", f"tl|{game_key}|ssy{y}|{series_key}")
                 for y in game.years(shots)],
                DRILL_PER_ROW,
            )
            rows.append([("⬅ Back", f"tl|{game_key}|-|{series_key}")])
            head = "a season of which year?"
        else:
            present = game.years(shots)
            rows = [[(f"🎞 Everything ({len(shots)} days)",
                      f"go|{game_key}|-|{series_key}")]]
            if len(present) > 1:
                rows += _rows(
                    [(f"📅 Year {y}", f"go|{game_key}|{game.drill(y)}|{series_key}")
                     for y in present],
                    DRILL_PER_ROW,
                )
            rows.append([("🍂 One season", f"tl|{game_key}|ss|{series_key}")])
            rows.append([("✂️ Custom range", f"rs|{game_key}|-|{series_key}")])
            head = "timelapse of what?"

        rows.append([("⬅ Farm", f"dt|{game_key}|-|{series_key}")])
        self._show(
            chat_id, message_id,
            f"<b>{self._esc(series.farm)}</b> — {head}",
            telegram.keyboard(rows),
        )

    def menu_range_start(self, chat_id, message_id, game_key, series_key, spec):
        game = games.get(game_key)
        if not games.browsable(game):
            self.menu_detail(chat_id, message_id, game_key, series_key)
            return
        self._drill_menu(
            chat_id, message_id, game_key, series_key, spec,
            nav_kind="rs", day_kind="re",
            day_arg=game.day_code, head_days="first day of the range",
            back=[("⬅ Timelapse", f"tl|{game_key}|-|{series_key}")],
        )

    def menu_range_end(self, chat_id, message_id, game_key, series_key, arg):
        """Second half of a custom range; the first day rides along in the arg.

        The arg is ``<start>`` while the end is still being chosen, and
        ``<start>:<drill>`` once that choice has walked into the calendar. Only
        days at or after the start are offered, so an inverted range cannot be
        built by hand — ``select`` would tolerate one, but a menu that lets you
        make a mistake it then silently fixes is a worse menu.
        """
        game = games.get(game_key)
        if not games.browsable(game):
            self.menu_detail(chat_id, message_id, game_key, series_key)
            return
        start, _, spec = (arg or "").partition(":")

        def at_or_after(shots):
            head = game.select(shots, start)
            if not head:
                return []
            first = head[0]
            floor = (first.year, first.season, first.day)
            return [c for c in shots if (c.year, c.season, c.day) >= floor]

        self._drill_menu(
            chat_id, message_id, game_key, series_key, spec or "-",
            nav_kind="re", day_kind="go",
            day_arg=lambda c: game.span(start, game.day_code(c)),
            head_days=f"last day (from {game.spec_label(start)})",
            nav_arg=lambda drill: f"{start}:{drill}",
            shots_filter=at_or_after,
            back=[("⬅ Timelapse", f"tl|{game_key}|-|{series_key}")],
        )

    # -- actions ----------------------------------------------------------
    def send_capture(self, chat_id, game_key, series_key, spec):
        """One capture as a photo: the latest when spec is ``-``, else the day."""
        game, series = self._series(game_key, series_key)
        if series is None:
            self.tg.send_message(chat_id, "That farm is gone.")
            return
        shots = self._pick(game, game.captures(series), spec)
        if not shots:
            self.tg.send_message(chat_id, "No capture for that day.")
            return
        shot = shots[-1]
        caption = f"<b>{self._esc(series.farm)}</b> — {self._esc(shot.label)}"
        markup = telegram.keyboard([[
            ("🖼 Original PNG", f"og|{game_key}|{spec}|{series.key}"),
            ("⬅ Farm", f"dt|{game_key}|-|{series.key}"),
        ]])
        # The size comes from the scan, so deciding which path to take costs no
        # transfer — only the branch actually taken pulls the file down.
        with self._local([shot]) as (path,):
            if shot.size > config.PHOTO_MAX_BYTES:
                # Over the photo limit; the document path is the only one left.
                self.tg.send_document(chat_id, path, caption)
            else:
                self.tg.send_photo(chat_id, path, caption, markup)

    def send_original(self, chat_id, game_key, series_key, spec):
        game, series = self._series(game_key, series_key)
        if series is None:
            return
        shots = self._pick(game, game.captures(series), spec)
        if not shots:
            return
        shot = shots[-1]
        with self._local([shot]) as (path,):
            self.tg.send_document(
                chat_id, path,
                f"<b>{self._esc(series.farm)}</b> — {self._esc(shot.label)} "
                f"(original, {shot.size / 1e6:.1f} MB)",
            )

    @contextlib.contextmanager
    def _local(self, shots):
        """Pull captures down for the length of one request, then delete them.

        The pod has no persistent storage by design, so nothing fetched here is
        allowed to outlive the request that asked for it — hence the ``finally``.
        The byte cap is what stops one enormous span from filling the scratch
        volume and getting the pod evicted mid-encode.
        """
        wanted = sum(s.size for s in shots)
        if wanted > config.MAX_FETCH_BYTES:
            raise media.EncodeError(
                f"that span is {wanted / 1e9:.1f} GB of captures, over the "
                f"{config.MAX_FETCH_BYTES / 1e9:.1f} GB this bot will pull at once"
            )
        config.WORK_DIR.mkdir(parents=True, exist_ok=True)
        dest = Path(tempfile.mkdtemp(dir=config.WORK_DIR, prefix="fetch-"))
        try:
            yield source.active().fetch([s.path for s in shots], dest)
        finally:
            source.scratch(dest)

    def enqueue_clip(self, chat_id, message_id, game_key, series_key, spec):
        game, series = self._series(game_key, series_key)
        if series is None or not series.animatable:
            return
        picked = self._pick(game, game.captures(series), spec)
        if len(picked) < 2:
            self.tg.send_message(
                chat_id, "That range has fewer than two captures — nothing to animate."
            )
            return
        if not self.limits.encode(chat_id):
            self.tg.send_message(
                chat_id, "Too many GIFs in a row. Give it a minute."
            )
            return
        pending = self.jobs.qsize()
        note = f" ({pending} ahead in the queue)" if pending else ""
        span = (
            game.spec_label(spec) if games.browsable(game) else "everything"
        )
        self.tg.send_message(
            chat_id,
            f"⏳ Building the timelapse for <b>{self._esc(series.farm)}</b> — "
            f"{self._esc(span)}, {len(picked)} days{note}. This takes a while.",
        )
        self.jobs.put(Job(chat_id, message_id, game_key, series_key, spec))

    # -- worker -----------------------------------------------------------
    def _encode_loop(self):
        while True:
            job = self.jobs.get()
            try:
                self._encode(job)
            except source.Unreachable as exc:
                # The console can go to sleep between queueing a clip and
                # building it, so this is a normal outcome here, not a fault.
                log.info("console unreachable for %s: %s", job.series_key, exc)
                try:
                    self._offline(job.chat_id)
                except Exception:
                    log.exception("and could not report it either")
            except Exception:
                log.exception("encode failed for %s", job.series_key)
                try:
                    self.tg.send_message(job.chat_id, "The clip failed. Check the log.")
                except Exception:
                    log.exception("and could not report it either")
            finally:
                self.jobs.task_done()

    def _encode(self, job: Job):
        game, series = self._series(job.game_key, job.series_key)
        if series is None:
            return
        shots = self._pick(game, game.captures(series), job.spec)
        if len(shots) < 2:
            self.tg.send_message(job.chat_id, "At least two captures are needed.")
            return
        span = game.spec_label(job.spec) if games.browsable(game) else "everything"

        # Always H.264, whatever the length. A GIF gets 256 colours per frame,
        # which on a farm capture reads as banding and dither no matter how wide
        # it is encoded; the only thing length still decides is how it is
        # delivered — a short one loops, a long one gets a scrubber.
        as_video = len(shots) > config.LOOP_MAX_FRAMES
        fps = config.VIDEO_FPS if as_video else config.LOOP_FPS

        config.WORK_DIR.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^\w.~-]", "_", f"{series.game_id}-{series.location}-{job.spec}")
        out = config.WORK_DIR / f"{stem}.mp4"
        started = time.monotonic()
        try:
            # ffmpeg cannot read over SSH, so the frames come down first. They
            # are gone again by the time the clip is sent — only the encoded
            # output survives the block, and not for long either.
            with self._local(shots) as paths:
                clip = media.build_mp4(
                    paths, out, fps=fps, budget=config.UPLOAD_BUDGET
                )
        except media.EncodeError as exc:
            self.tg.send_message(job.chat_id, f"Could not build it: {self._esc(exc)}")
            return
        except source.Unreachable as exc:
            log.warning("console went away mid-encode: %s", exc)
            self._offline(job.chat_id)
            return

        elapsed = time.monotonic() - started
        caption = (
            f"<b>{self._esc(series.farm)}</b> — {self._esc(span)}\n"
            f"{clip.frames} frames, {clip.width}px, {clip.size / 1e6:.1f} MB"
        )
        caption += f", every day"
        log.info(
            "%s %s [%s]: %s frames, %spx, %.1f MB, %.0fs",
            "video" if as_video else "loop",
            series.key, job.spec, clip.frames, clip.width, clip.size / 1e6, elapsed,
        )
        send = self.tg.send_video if as_video else self.tg.send_animation
        try:
            send(
                job.chat_id, clip.path, caption, width=clip.width,
                height=clip.height, duration=round(clip.duration) or 1,
            )
        finally:
            clip.path.unlink(missing_ok=True)

    # -- dispatch ---------------------------------------------------------
    def handle_message(self, message: dict):
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()
        if not self.limits.action(chat_id):
            return
        # "/games@BotName" is what a group would send; strip the @suffix
        # everywhere rather than depending on where the message came from.
        word = text.split()[0] if text else ""
        name = word[1:].split("@")[0].lower() if word.startswith("/") else ""
        handler = next((h for n, _, h in COMMANDS if n == name), None)
        try:
            # Anything that is not a command opens the game menu, which is the
            # single most likely intent.
            getattr(self, handler or "menu_games")(chat_id)
        except source.Unreachable as exc:
            log.info("console unreachable: %s", exc)
            self._offline(chat_id)

    # -- commands ---------------------------------------------------------

    def cmd_help(self, chat_id):
        self.tg.send_message(chat_id, HELP)

    def cmd_wake(self, chat_id):
        self.do_wake(chat_id, None, console.CAPTURE_CONSOLE.key)

    def cmd_status(self, chat_id):
        """A one-screen answer to "is it on, and is there anything to look at".

        Must survive the console being down — that is most of the reason to ask.
        """
        lines = [
            f"{c.icon} <b>{self._esc(c.name)}</b> — "
            f"{'awake' if c.is_up() else 'not answering'}"
            + ("" if c.can_wake else " (needs the physical button)")
            for c in console.CONSOLES.values()
        ]
        try:
            farms = captures = 0
            for game in games.GAMES.values():
                for series in game.list_series():
                    farms += 1
                    captures += len(game.captures(series))
            lines.append(
                f"\n🌾 {farms} farm{'s' if farms != 1 else ''}, "
                f"{captures} capture{'s' if captures != 1 else ''}"
            )
        except source.Unreachable:
            lines.append("\nCaptures cannot be read while it is down.")
        self.tg.send_message(chat_id, "\n".join(lines), telegram.keyboard([
            [("🎮 Games", "mg|-|-|-"), ("🎛 Consoles", "cs|-|-|-")],
        ]))

    def cmd_latest(self, chat_id):
        """The newest capture anywhere, which is what "show me my farm" means."""
        best = None
        for game in games.GAMES.values():
            for series in game.list_series():
                shots = game.captures(series)
                if not shots:
                    continue
                if best is None or shots[-1].sort_key > best[2].sort_key:
                    best = (game, series, shots[-1])
        if best is None:
            self.tg.send_message(chat_id, "No captures yet.")
            return
        game, series, shot = best
        spec = game.day_code(shot) if games.browsable(game) else "-"
        self.send_capture(chat_id, game.KEY, series.key, spec)

    def register_commands(self):
        """Publish the command list to Telegram.

        Done at startup from COMMANDS so the menu the user sees can never drift
        from what the router actually handles.
        """
        try:
            self.tg.call("setMyCommands", commands=[
                {"command": name, "description": desc}
                for name, desc, _ in COMMANDS
            ])
            log.info("registered %d commands", len(COMMANDS))
        except Exception:
            # Cosmetic: the bot works fine with a stale menu, so this must never
            # be the reason it fails to start.
            log.warning("could not register the command list", exc_info=True)

    def handle_callback(self, callback: dict):
        data = callback.get("data") or ""
        message = callback.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")
        if chat_id is None:
            return
        if not self.limits.action(chat_id):
            self.tg.answer_callback_query(callback["id"], "Too many requests.")
            return
        self.tg.answer_callback_query(callback["id"])

        parts = data.split("|", 3) + ["", "", ""]
        kind, game_key, arg, series_key = parts[:4]
        try:
            if kind in LEGACY_KINDS:
                self.tg.send_message(
                    chat_id,
                    "That menu is from an older version of the bot. "
                    "Send /start for the current one.",
                )
            elif kind == "mg":
                self.menu_games(chat_id, message_id)
            elif kind == "sl":
                self.menu_series(chat_id, message_id, game_key)
            elif kind == "dt":
                self.menu_detail(chat_id, message_id, game_key, series_key)
            elif kind == "dp":
                self.menu_days(chat_id, message_id, game_key, series_key, arg)
            elif kind == "tl":
                self.menu_timelapse(chat_id, message_id, game_key, series_key, arg)
            elif kind == "rs":
                self.menu_range_start(chat_id, message_id, game_key, series_key, arg)
            elif kind == "re":
                self.menu_range_end(chat_id, message_id, game_key, series_key, arg)
            elif kind == "ph":
                self.send_capture(chat_id, game_key, series_key, arg)
            elif kind == "og":
                self.send_original(chat_id, game_key, series_key, arg)
            elif kind == "go":
                self.enqueue_clip(chat_id, message_id, game_key, series_key, arg)
            elif kind == "cs":
                self.menu_consoles(chat_id, message_id)
            elif kind == "cn":
                self.menu_console(chat_id, message_id, game_key)
            elif kind == "wk":
                self.do_wake(chat_id, message_id, game_key)
            elif kind in ("sp", "spy"):
                self.act_power(chat_id, message_id, game_key, "suspend",
                               confirmed=kind == "spy")
            elif kind in ("pw", "pwy"):
                self.act_power(chat_id, message_id, game_key, "poweroff",
                               confirmed=kind == "pwy")
        except source.Unreachable as exc:
            log.info("console unreachable during %s: %s", kind, exc)
            self._offline(chat_id)
        except telegram.TelegramError:
            log.exception("callback %s", data)
