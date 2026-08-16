"""Checks the bot's moving parts against whatever captures the console has.

Runs against the configured source, so by default it exercises the real SSH
path to the Steam Machine — discovery, the menus and a real encode. Point
CAPTURE_SOURCE=local at a fake tree to rehearse a scale this console has not
reached yet.
"""

import sys
import tempfile
from pathlib import Path

from steam_bot import bot as bot_mod
from steam_bot import config, games, media, source, telegram

fails = []


def check(label, condition, detail=""):
    print(f"  [{'OK  ' if condition else 'FAIL'}] {label} {detail}")
    if not condition:
        fails.append(label)


print("== discovery ==")
game = games.get("stardew")
series = game.list_series()
check("series found", bool(series), f"({len(series)})")
for s in series:
    print(f"      {s.key:22} {s.title():30} {game.summary(s)}")
    shots = game.captures(s)
    check(f"  {s.key} has captures", bool(shots), f"({len(shots)})")
    check(
        f"  {s.key} sorted",
        [c.sort_key for c in shots] == sorted(c.sort_key for c in shots),
    )
    # The captures are on the console, not here, so "does the file exist" is
    # answered by the scan that found it: a directory or an unreadable entry
    # never carries a size.
    check(f"  {s.key} paths are real files", all(c.size > 0 for c in shots))

print("== date selection ==")
check("stardew implements the browse API", games.browsable(game))
missing = [n for n in games.BROWSE_API if not hasattr(game, n)]
if missing:
    check("  nothing missing", False, f"({', '.join(missing)})")
for s in series:
    shots = game.captures(s)
    if not s.animatable or not shots:
        continue
    first, last = shots[0], shots[-1]
    whole = game.span(game.day_code(first), game.day_code(last))
    year = game.drill(first.year)
    season = game.drill(first.year, first.season)
    check(
        f"  {s.key} full span selects everything",
        len(game.select(shots, whole)) == len(shots),
        f"({len(game.select(shots, whole))}/{len(shots)})",
    )
    check(
        f"  {s.key} a single day selects one",
        len(game.select(shots, game.day_code(first))) == 1,
    )
    check(
        f"  {s.key} an inverted range still selects the span",
        game.select(shots, game.span(game.day_code(last), game.day_code(first)))
        == game.select(shots, whole),
    )
    check(
        f"  {s.key} a year is a subset",
        set(game.select(shots, year)) <= set(shots)
        and len(game.select(shots, year)) > 0,
    )
    check(
        f"  {s.key} a season is inside its year",
        set(game.select(shots, season)) <= set(game.select(shots, year)),
    )
    check(f"  {s.key} unreadable spec selects nothing",
          game.select(shots, "wat") == [])
    check(f"  {s.key} labels read back",
          game.spec_label(game.drill()) == "everything"
          and "→" in game.spec_label(whole))

print("== callback_data fits in 64 bytes ==")
for s in series:
    shots = game.captures(s)
    args = ["-"]
    if s.animatable and shots:
        first, last = shots[0], shots[-1]
        args += [
            game.drill(first.year),
            game.drill(first.year, first.season),
            game.day_code(last),
            # The two longest a button ever carries: a full range, and the
            # half-built range the end picker walks around with.
            game.span(game.day_code(first), game.day_code(last)),
            f"{game.day_code(first)}:{game.drill(last.year, last.season)}",
        ]
    for kind in ("dt", "ph", "og", "dp", "tl", "rs", "re", "go"):
        for arg in args:
            data = f"{kind}|stardew|{arg}|{s.key}"
            size = len(data.encode())
            check(f"  {data}", size <= 64, f"({size} B)")

print("== every menu path builds, and every button it emits fits ==")


class FakeClient:
    """Records what the menus would send. Nothing leaves the machine."""

    def __init__(self):
        self.markups = []

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.markups.append(reply_markup)
        return {}

    def send_message(self, chat_id, text, reply_markup=None):
        self.markups.append(reply_markup)
        return {}

    def answer_callback_query(self, query_id, text=None):
        return {}


# Walking the real menus is what catches a lambda that closes over the wrong
# thing or a level that forgets to thread its state — the hand-written cases
# above only prove the budget, not that the buttons are reachable.
# "cs"/"cn" only render menus and read liveness, so they are safe to crawl.
# NEVER add "wk", "sp", "spy", "pw" or "pwy": the crawler would suspend or power
# off a real console. "sp" is not even a confirmation screen on the Steam
# Machine — it suspends immediately, because there it is reversible.
NAV_KINDS = {"mg", "sl", "dt", "dp", "tl", "rs", "re", "cs", "cn"}

fake = FakeClient()
crawler = bot_mod.Bot(fake)
crawler.limits.action = lambda chat_id: True  # a full crawl outruns the limiter

pending, seen, buttons = ["mg"], set(), 0
while pending:
    data = pending.pop()
    if data in seen:
        continue
    seen.add(data)
    fake.markups.clear()
    crawler.handle_callback(
        {"id": "0", "data": data, "message": {"chat": {"id": 1}, "message_id": 1}}
    )
    for markup in fake.markups:
        for row in (markup or {}).get("inline_keyboard", []):
            for button in row:
                target = button["callback_data"]
                size = len(target.encode())
                buttons += 1
                if size > 64:
                    check(f"  {target}", False, f"({size} B)")
                if target.split("|", 1)[0] in NAV_KINDS:
                    pending.append(target)

check("menus crawled", bool(seen), f"({len(seen)} screens, {buttons} buttons)")
check(
    "the day, range and season paths are all reachable",
    all(any(s.startswith(k) for s in seen) for k in ("dp|", "tl|", "rs|", "re|")),
)

print("== fetching frames, because ffmpeg cannot read over SSH ==")
animatable = [s for s in series if s.animatable and len(game.captures(s)) > 1]
frames: list[Path] = []
staging = Path(tempfile.mkdtemp(prefix="selftest-frames-"))
if animatable:
    shots = game.captures(animatable[0])
    frames = source.active().fetch([c.path for c in shots], staging)
    check("every frame arrived", len(frames) == len(shots), f"({len(frames)})")
    check("and is readable", all(f.is_file() and f.stat().st_size for f in frames))

print("== GIF (only the CLI's --gif builds one now) ==")
if not animatable:
    print("  (no animatable series, skipping the encode)")
else:
    s = animatable[0]
    shots = game.captures(s)
    out = Path(tempfile.gettempdir()) / "selftest.gif"
    gif = media.build_gif(frames, out)
    print(
        f"      {gif.frames} frames, {gif.width}px, every {gif.every}, "
        f"{gif.size / 1e6:.2f} MB"
    )
    check("fits the budget", gif.size <= config.UPLOAD_BUDGET)
    check("really is a GIF", out.read_bytes()[:6] in (b"GIF89a", b"GIF87a"))
    check("more than one frame", gif.frames > 1)
    out.unlink(missing_ok=True)

print("== MP4, which is what the bot always ships ==")
if animatable:
    s = animatable[0]
    shots = game.captures(s)
    out = Path(tempfile.gettempdir()) / "selftest.mp4"
    clip = media.build_mp4(
        frames, out,
        fps=config.VIDEO_FPS, budget=config.UPLOAD_BUDGET,
    )
    print(
        f"      {clip.frames} frames, {clip.width}x{clip.height}, "
        f"{clip.size / 1e6:.2f} MB, {clip.duration:.0f}s"
    )
    check("fits the budget", clip.size <= config.UPLOAD_BUDGET)
    check("really is an MP4", out.read_bytes()[4:8] == b"ftyp")
    # Downscaling pixel art costs bits as well as detail, so a span that fits
    # should have been left at the captures' own resolution.
    native = media.probe_size(frames[0])[0]
    check("kept native resolution", clip.width == native, f"({clip.width}px)")
    # sendVideo only renders a player when it is told the geometry up front.
    check(
        "geometry and duration reported",
        clip.width > 0 and clip.height > 0 and clip.duration > 0,
    )
    out.unlink(missing_ok=True)

print("== an impossible budget must raise ==")
if animatable:
    s = animatable[0]
    shots = game.captures(s)
    try:
        media.build_gif(
            frames,
            Path(tempfile.gettempdir()) / "tiny.gif",
            budget=1024,
            widths=[480],
        )
        check("build_gif complains when it will not fit", False, "(it did not)")
    except media.EncodeError as exc:
        check("build_gif complains when it will not fit", True, f"({exc})")

source.scratch(staging)
check("the fetched frames are gone again", not staging.exists())

print("== Telegram ==")
client = telegram.Client(config.token())
me = client.get_me()
check("getMe", bool(me.get("username")), f"(@{me.get('username')})")

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("all OK")
