"""The consoles themselves: liveness, waking, suspending, powering off.

Everything here was measured on 2026-08-08.

**The two consoles are not symmetric, and the difference is the whole reason
this module has a `can_wake` flag.**

The Steam Machine is on Ethernet. Its NIC advertises `magic` and now has it
enabled (`Wake-on: g`, `power/wakeup: enabled`, persisted by NetworkManager and
verified across a cold boot), so a suspended Machine comes back in about five
seconds. A fully powered-off one does not come back at all: the firmware cuts
standby power to the port in S5 and Valve exposes no UEFI setup.

The Steam Deck has **only `wlan0`** — no Ethernet at all unless it is docked, and
it drops Wi-Fi when it suspends. There is nothing to send a magic packet to. So
on the Deck *both* suspend and power off are one-way doors, where on the Machine
only power off is. Anything offering these actions has to say which it is.

Two mechanics worth keeping:

**The magic packet must be a broadcast, sent from the node's network namespace.**
A suspended NIC answers magic packets but not ARP, so the node cannot resolve
its MAC and a unicast is dropped before it reaches the wire (`ip neigh` reads
`FAILED`). Broadcast needs no ARP — but a broadcast from inside the CNI network
never leaves it, which is why the Deployment sets `hostNetwork: true`.

**Liveness is a TCP connect to port 22, not a ping.** ICMP from a container needs
a raw socket or a tuned `ping_group_range`, and "can I SSH to it" is the question
actually being asked.
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, source

log = logging.getLogger(__name__)


def _magic(mac: str) -> bytes:
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError(f"not a MAC address: {mac!r}")
    return b"\xff" * 6 + raw * 16


@dataclass
class Console:
    key: str
    name: str
    icon: str
    host: str
    user: str
    ssh_key: Path
    known_hosts: Path
    # None means there is no way to wake it remotely. The reason is carried
    # alongside so the UI can explain rather than just refuse.
    mac: str | None = None
    no_wake_reason: str = ""
    _ssh: source.SSHSource | None = field(default=None, repr=False, compare=False)

    @property
    def can_wake(self) -> bool:
        return bool(self.mac)

    @property
    def ssh(self) -> source.SSHSource:
        """Its own SSH runner, built lazily.

        Separate from the capture source on purpose: only one console holds the
        captures, but every console can be powered.
        """
        if self._ssh is None:
            self._ssh = source.SSHSource(
                host=self.host, user=self.user, key=self.ssh_key,
                known_hosts=self.known_hosts,
                connect_timeout=config.SSH_CONNECT_TIMEOUT,
            )
        return self._ssh

    def is_up(self, timeout: float = 2.0) -> bool:
        try:
            with socket.create_connection((self.host, 22), timeout=timeout):
                return True
        except OSError:
            return False

    def send_magic_packet(self) -> int:
        """Broadcast a wake-up. Returns how many packets went out.

        Both ports are used because there is no standard one — 9 (discard) is
        the convention, 7 (echo) is what some firmware listens on.
        """
        if not self.mac:
            return 0
        packet = _magic(self.mac)
        sent = 0
        for port in config.WOL_PORTS:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.sendto(packet, (config.WOL_BROADCAST, port))
                s.close()
                sent += 1
            except OSError as exc:
                log.warning("magic packet for %s port %s failed: %s",
                            self.key, port, exc)
        return sent

    def wake(self, timeout: int = config.WAKE_TIMEOUT) -> bool:
        """Send a magic packet and wait for SSH.

        False does not distinguish "was fully off" from "did not wake" — from
        here the two are indistinguishable, and the caller has to say so rather
        than guess.
        """
        if self.is_up():
            return True
        if not self.send_magic_packet():
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_up(timeout=1.5):
                log.info("%s answered SSH after the magic packet", self.key)
                return True
            time.sleep(1)
        log.info("%s did not come up within %ss", self.key, timeout)
        return False

    def _schedule(self, verb: str) -> None:
        """Run a power command a few seconds from now.

        Deferred with `systemd-run --on-active` on purpose: calling `systemctl
        poweroff` directly races the SSH session it arrived on, so the exit
        status says more about when the link dropped than about whether it
        worked. Scheduling lets the call return 0 cleanly first.
        """
        self.ssh.run([
            "sudo", "systemd-run", "--on-active=3",
            "--timer-property=AccuracySec=1s",
            "systemctl", verb,
        ])

    def suspend(self) -> None:
        """Suspend. Reversible only where ``can_wake`` is true."""
        self._schedule("suspend")

    def poweroff(self) -> None:
        """Power off completely. Never reversible from here."""
        self._schedule("poweroff")


STEAM_MACHINE = Console(
    key="machine",
    name="Steam Machine",
    icon="🖥",
    host=config.SSH_HOST,
    user=config.SSH_USER,
    ssh_key=config.SSH_KEY,
    known_hosts=config.SSH_KNOWN_HOSTS,
    mac=config.WOL_MAC,
)

STEAM_DECK = Console(
    key="deck",
    name="Steam Deck",
    icon="🎮",
    host=config.DECK_SSH_HOST,
    user=config.DECK_SSH_USER,
    ssh_key=config.DECK_SSH_KEY,
    known_hosts=config.DECK_SSH_KNOWN_HOSTS,
    mac=None,
    no_wake_reason=(
        "it has no Ethernet — only Wi-Fi, which it drops when it sleeps, so "
        "there is nothing to send a magic packet to"
    ),
)

CONSOLES = {c.key: c for c in (STEAM_MACHINE, STEAM_DECK)}

# The one the captures come from. Everything capture-shaped goes through
# source.active(); this is only for the power menus and the offline notice.
CAPTURE_CONSOLE = STEAM_MACHINE


def get(key: str) -> Console | None:
    return CONSOLES.get(key)
