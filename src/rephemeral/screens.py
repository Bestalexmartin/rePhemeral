"""The catalog of replaceable screens on a reMarkable Paper Pro.

Everything here was measured on a device running firmware build
20260827113527 (v3.28.0.172), not taken from documentation. reMarkable
publishes none of this, so treat it as observed fact about one firmware
line rather than a stable contract. `verify_against_device` exists because
the next firmware may move or reshape any of it.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Every screen lives in this directory on the device rootfs.
SCREEN_DIR = "/usr/share/remarkable"

#: Panel geometry of the Paper Pro's full-screen art.
PANEL_WIDTH = 1620
PANEL_HEIGHT = 2160

#: The sleep carousel illustrations are square and composited by xochitl
#: onto its own background, so they are not panel-sized.
CAROUSEL_SIZE = 1012


@dataclass(frozen=True)
class Screen:
    """One replaceable image on the device."""

    key: str
    """Stable identifier used by the config file, API and UI."""

    filename: str
    """Basename within SCREEN_DIR."""

    label: str
    """Short human name for the UI."""

    description: str
    """What the user actually sees, and when."""

    width: int
    height: int

    grayscale: bool = False
    """True where stock art is 8-bit single-channel rather than RGBA.

    The Paper Pro is a color panel and accepts RGBA everywhere, so this
    is descriptive of the stock asset rather than a constraint we must
    honor. It matters only when reporting drift: a grayscale original
    replaced by RGBA is an expected difference, not corruption.
    """

    risky: bool = False
    """True where replacing the image degrades a diagnostic signal.

    These screens tell you something has gone wrong. Overwriting them
    with decorative art means a future failure looks like a normal
    screen. The UI warns before touching one.
    """

    @property
    def path(self) -> str:
        return f"{SCREEN_DIR}/{self.filename}"


SCREENS: tuple[Screen, ...] = (
    Screen(
        key="suspended",
        filename="suspended.png",
        label="Sleep",
        description="Shown whenever the tablet is asleep. The one most people mean.",
        width=PANEL_WIDTH,
        height=PANEL_HEIGHT,
    ),
    Screen(
        key="starting",
        filename="starting.png",
        label="Starting up",
        description="Shown while the tablet boots.",
        width=PANEL_WIDTH,
        height=PANEL_HEIGHT,
    ),
    Screen(
        key="starting_first",
        filename="starting_first.png",
        label="First start",
        description="Shown on the very first boot after a factory reset.",
        width=PANEL_WIDTH,
        height=PANEL_HEIGHT,
    ),
    Screen(
        key="rebooting",
        filename="rebooting.png",
        label="Restarting",
        description=(
            "Shown while the tablet restarts. Note that restart-crashed.png "
            "is a symlink to this file, so replacing it also changes the "
            "screen shown after a crash."
        ),
        width=PANEL_WIDTH,
        height=PANEL_HEIGHT,
    ),
    Screen(
        key="poweroff",
        filename="poweroff.png",
        label="Powering off",
        description="Shown while the tablet shuts down.",
        width=PANEL_WIDTH,
        height=PANEL_HEIGHT,
    ),
    Screen(
        key="batteryempty",
        filename="batteryempty.png",
        label="Battery empty",
        description=(
            "Shown when the battery is flat. Replacing it means a dead "
            "tablet no longer tells you why it is dead."
        ),
        width=PANEL_WIDTH,
        height=PANEL_HEIGHT,
        risky=True,
    ),
    Screen(
        key="remotewipe",
        filename="remotewipe.png",
        label="Remote wipe",
        description=(
            "Shown while the device is being erased remotely. Stock art is "
            "grayscale. Replacing it hides a security-relevant event."
        ),
        width=PANEL_WIDTH,
        height=PANEL_HEIGHT,
        grayscale=True,
        risky=True,
    ),
    Screen(
        key="factory",
        filename="factory.png",
        label="Factory reset",
        description=(
            "Shown during a factory reset. Replacing it hides a "
            "destructive operation in progress."
        ),
        width=PANEL_WIDTH,
        height=PANEL_HEIGHT,
        risky=True,
    ),
    Screen(
        key="carousel_1",
        filename="carousel/sleep_Illustration_01.png",
        label="Sleep illustration 1",
        description="One of three square illustrations cycled on the sleep screen.",
        width=CAROUSEL_SIZE,
        height=CAROUSEL_SIZE,
    ),
    Screen(
        key="carousel_2",
        filename="carousel/sleep_Illustration_02.png",
        label="Sleep illustration 2",
        description="One of three square illustrations cycled on the sleep screen.",
        width=CAROUSEL_SIZE,
        height=CAROUSEL_SIZE,
    ),
    Screen(
        key="carousel_3",
        filename="carousel/sleep_Illustration_03.png",
        label="Sleep illustration 3",
        description="One of three square illustrations cycled on the sleep screen.",
        width=CAROUSEL_SIZE,
        height=CAROUSEL_SIZE,
    ),
)

BY_KEY: dict[str, Screen] = {s.key: s for s in SCREENS}

#: Known symlinks in SCREEN_DIR, mapping link name to its target's basename.
#: Writing through one of these would silently clobber the target, so the
#: device layer resolves links before writing and refuses to follow them.
#: Detected at runtime as well; this is documentation, not the check.
KNOWN_SYMLINKS = {"restart-crashed.png": "rebooting.png"}


def get(key: str) -> Screen:
    try:
        return BY_KEY[key]
    except KeyError:
        raise KeyError(
            f"unknown screen {key!r}; known keys: {', '.join(sorted(BY_KEY))}"
        ) from None
