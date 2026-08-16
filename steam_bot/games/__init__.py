"""Game registry.

A game module exposes ``KEY``, ``NAME``, ``ICON``, ``list_series``,
``find_series``, ``captures`` and ``summary``. Adding a game means writing one
module and adding it here; nothing else in the package changes.

A module may *also* implement the browse extension listed in ``BROWSE_API``,
which is what lets the bot offer a single day and an arbitrary range instead of
only the latest capture and the whole series. It is optional on purpose: time
in Stardew is year/season/day, and another game may have no comparable calendar
at all. Where it is missing the bot quietly falls back to the short menu, so a
new game works before its calendar does.
"""

from __future__ import annotations

from . import stardew

GAMES = {stardew.KEY: stardew}

BROWSE_API = (
    "SPEC_ALL",     # the spec meaning "every capture"
    "select",       # (shots, spec) -> the captures that spec names
    "spec_label",   # (spec) -> how it reads in a menu or caption
    "drill",        # (year?, season?) -> spec for a whole year or season
    "parse_drill",  # (spec) -> (year?, season?)
    "day_code",     # (capture) -> the compact form of one day
    "span",         # (start_code, end_code) -> spec for an inclusive range
    "years",        # (shots) -> the years present
    "seasons",      # (shots, year) -> the seasons present in one year
    "days",         # (shots, year, season) -> the days present in one season
    "season_name",  # (season) -> what to write on the button
)


def get(key: str):
    return GAMES.get(key)


def browsable(game) -> bool:
    """Whether this game can be browsed by date, i.e. implements BROWSE_API."""
    return game is not None and all(hasattr(game, name) for name in BROWSE_API)
