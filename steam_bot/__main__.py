"""Entry point: the polling loop.

Nothing here may raise its way out. The console suspends, loses the network and
comes back; the bot has to survive all of it, and systemd's Restart=always is
the backstop, not the plan.
"""

from __future__ import annotations

import logging
import sys
import time

from . import config, source, telegram
from .bot import Bot

log = logging.getLogger("steam_bot")

# Exit codes. Kubernetes restarts on any of them — there is no systemd
# RestartPreventExitStatus here — so these exist to make the log readable, not
# to change what the platform does. A 409 therefore shows up as a visible
# CrashLoopBackOff, which is the loud failure that case wants.
EXIT_CONFLICT = 3


def _connect(client: telegram.Client) -> dict:
    """Reach Telegram, retrying for as long as it takes.

    This used to be a bare call, and a DNS lookup that was not ready at boot
    killed the process for good: the failure exited 1, which was also the code
    reserved for "another poller holds this token", so the restart guard
    suppressed the restart. Startup is not the place to give up.
    """
    delay = 1
    while True:
        try:
            return client.get_me()
        except Exception as exc:
            log.warning("cannot reach Telegram yet (%s); retrying in %ss", exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    client = telegram.Client(config.token())
    me = _connect(client)
    log.info("connected as @%s", me.get("username"))
    log.info(
        "captures via %s%s",
        source.active().name,
        f" from {config.SSH_USER}@{config.SSH_HOST}"
        if config.CAPTURE_SOURCE != "local" else "",
    )
    log.info("capture roots: %s", ", ".join(str(r) for r in config.SCREENSHOT_ROOTS))

    bot = Bot(client)
    bot.start()

    offset = 0
    backoff = 1
    while True:
        try:
            updates = client.get_updates(offset, poll=config.POLL_TIMEOUT)
            backoff = 1
        except telegram.ConflictError:
            # Another process is polling with this token. Two bots would steal
            # each other's updates at random, so refuse to be the second one.
            log.error(
                "409 Conflict: another process is already polling getUpdates with "
                "this token. Only one bot per token can run."
            )
            return EXIT_CONFLICT
        except Exception as exc:
            log.warning("getUpdates failed (%s); retrying in %ss", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        for update in updates:
            offset = max(offset, update["update_id"] + 1)
            try:
                if "message" in update:
                    bot.handle_message(update["message"])
                elif "callback_query" in update:
                    bot.handle_callback(update["callback_query"])
            except Exception:
                log.exception("update %s", update.get("update_id"))


if __name__ == "__main__":
    sys.exit(main())
