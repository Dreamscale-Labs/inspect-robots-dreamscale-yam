"""Identify each arm's USB-CAN adapter and resolve it to its current interface name.

Linux kernel names such as ``can0`` and ``can1`` can swap across reboots or
replugs, and udev names such as ``can_follower_l`` exist only on rigs that
installed matching rules. The adapter itself is stable: its USB serial, or the
USB port path when it has no usable serial. Setup saves that identity and every
later command resolves it to whatever the interface is called now, so a swapped
name can never silently drive the wrong arm.

Everything here reads sysfs without sudo and never sends a CAN frame.
"""

from __future__ import annotations

import functools
import os
import re
import select
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from dreamscale_yam.config import CAN_ID_PATTERN, RigConfig
from dreamscale_yam.errors import UserFacingError
from dreamscale_yam.prompts import select as select_numbered
from dreamscale_yam.prompts import yes_no

SYSFS_NET = Path("/sys/class/net")
#: I2RT's YAM motors and its reference udev rule both use 1 Mbit/s.
CAN_BITRATE = 1_000_000
_ARPHRD_CAN = "280"
_USB_PORT_NAME = re.compile(r"\d+-\d+(?:\.\d+)*\Z")


@dataclass(frozen=True)
class CanInterface:
    """One SocketCAN interface as sysfs reports it right now."""

    name: str
    up: bool
    usb_serial: str | None = None
    usb_port: str | None = None
    channel: int = 0


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None


def _usb_device_dir(net_dir: Path) -> Path | None:
    """Return the USB device (not interface) directory that owns an interface.

    The walk stops at the first ancestor with ``idVendor``. Continuing further
    would reach a hub or the root hub, whose ``serial`` (for example the PCI
    address ``0000:65:00.3``) is shared by every adapter behind it.
    """
    try:
        device = (net_dir / "device").resolve(strict=True)
    except OSError:
        return None
    for ancestor in (device, *device.parents):
        if (ancestor / "idVendor").is_file():
            return ancestor
    return None


def read_can_interface(net_dir: Path) -> CanInterface:
    """Read one interface's state and USB identity from its sysfs directory."""
    usb_dir = _usb_device_dir(net_dir)
    serial: str | None = None
    port: str | None = None
    if usb_dir is not None:
        raw_serial = _read_text(usb_dir / "serial")
        if raw_serial and not any(character.isspace() for character in raw_serial):
            serial = raw_serial
        if _USB_PORT_NAME.fullmatch(usb_dir.name):
            port = usb_dir.name
    try:
        channel = max(0, int(_read_text(net_dir / "dev_port") or "0"))
    except ValueError:
        channel = 0
    return CanInterface(
        name=net_dir.name,
        up=_read_text(net_dir / "operstate") == "up",
        usb_serial=serial,
        usb_port=port,
        channel=channel,
    )


def read_can_interfaces(root: Path = SYSFS_NET) -> dict[str, CanInterface]:
    """Return every SocketCAN interface Linux reports, by current name."""
    result: dict[str, CanInterface] = {}
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return result
    for entry in entries:
        if _read_text(entry / "type") == _ARPHRD_CAN:
            result[entry.name] = read_can_interface(entry)
    return result


def stable_can_id(interface: CanInterface, interfaces: Iterable[CanInterface]) -> str | None:
    """Return the identity that names this adapter among the connected ones.

    The USB serial is preferred. A serial shared by two connected adapters (a
    cheap clone) falls back to the USB port path, and an adapter with neither,
    such as a non-USB CAN card, has no identity at all.
    """
    others = list(interfaces)
    suffix = f"#{interface.channel}" if interface.channel > 0 else ""
    if interface.usb_serial and (
        sum(
            1
            for other in others
            if other.usb_serial == interface.usb_serial and other.channel == interface.channel
        )
        == 1
    ):
        return f"usb-serial:{interface.usb_serial}{suffix}"
    if interface.usb_port and (
        sum(
            1
            for other in others
            if other.usb_port == interface.usb_port and other.channel == interface.channel
        )
        == 1
    ):
        return f"usb-port:{interface.usb_port}{suffix}"
    return None


def can_id_matches(can_id: str, interface: CanInterface) -> bool:
    """Whether a saved identity names this connected interface."""
    match = CAN_ID_PATTERN.fullmatch(can_id)
    if match is None:
        return False
    kind, value, channel = match.group(1), match.group(2), int(match.group(3) or 0)
    if interface.channel != channel:
        return False
    if kind == "usb-serial":
        return interface.usb_serial == value
    return interface.usb_port == value


def describe_can_id(can_id: str | None) -> str:
    """Return a short human description of one saved identity."""
    if can_id is None:
        return "no USB identity"
    match = CAN_ID_PATTERN.fullmatch(can_id)
    if match is None:
        return can_id
    kind, value, channel = match.group(1), match.group(2), match.group(3)
    text = f"USB serial {value}" if kind == "usb-serial" else f"USB port {value}"
    return f"{text}, channel {channel}" if channel else text


def bring_up_command(name: str) -> list[str]:
    """Return the exact command that brings one CAN interface UP for the YAM arms."""
    return [
        "sudo",
        "ip",
        "link",
        "set",
        name,
        "up",
        "type",
        "can",
        "bitrate",
        str(CAN_BITRATE),
    ]


@dataclass(frozen=True)
class CanResolution:
    """Current interface names for a rig's saved CAN assignment."""

    left_channel: str
    right_channel: str
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    remediation: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems


def resolve_can_channels(
    rig: RigConfig,
    interfaces: Mapping[str, CanInterface] | None = None,
) -> CanResolution:
    """Resolve saved adapter identities to current interface names.

    A side without a saved identity keeps its saved name exactly, without
    reading sysfs, so rigs confirmed before identities existed behave as before.
    """
    needs_sysfs = rig.left_can_id is not None or rig.right_can_id is not None
    current = (
        interfaces if interfaces is not None else (read_can_interfaces() if needs_sysfs else {})
    )
    resolved: dict[str, str] = {}
    problems: list[str] = []
    notes: list[str] = []
    remediation: list[str] = []
    for side, saved_name, can_id in (
        ("left", rig.left_channel, rig.left_can_id),
        ("right", rig.right_channel, rig.right_can_id),
    ):
        if can_id is None:
            resolved[side] = saved_name
            continue
        described = describe_can_id(can_id)
        matches = sorted(
            interface.name for interface in current.values() if can_id_matches(can_id, interface)
        )
        if not matches:
            problems.append(f"The {side.upper()} arm's CAN adapter ({described}) is not connected")
            where = (
                "back into the same USB port it used during setup"
                if can_id.startswith("usb-port:")
                else "back in"
            )
            remediation.append(f"Plug the {side.upper()} arm's USB-CAN adapter {where}")
            resolved[side] = saved_name
        elif len(matches) > 1:
            problems.append(
                f"More than one connected CAN interface matches the {side.upper()} arm's "
                f"adapter ({described}): {', '.join(matches)}"
            )
            remediation.append("Disconnect the extra CAN adapter")
            resolved[side] = saved_name
        else:
            resolved[side] = matches[0]
            if matches[0] != saved_name:
                notes.append(
                    f"the {side} arm's adapter ({described}) is now {matches[0]} "
                    f"(saved as {saved_name})"
                )
    if not problems and resolved["left"] == resolved["right"]:
        problems.append(
            f"Both arms resolve to the same CAN interface {resolved['left']}; the left and "
            "right arms need two different adapters"
        )
        remediation.append("Run ./dreamscale-yam identify-can to assign each arm's adapter again")
    if problems:
        remediation.append(
            "confirm it with `ip -details link show type can`, then rerun ./dreamscale-yam "
            "doctor. If you replaced an adapter, run ./dreamscale-yam identify-can"
        )
    return CanResolution(
        left_channel=resolved["left"],
        right_channel=resolved["right"],
        problems=tuple(problems),
        notes=tuple(notes),
        remediation=", then ".join(remediation),
    )


def resolve_rig_can(rig: RigConfig) -> RigConfig:
    """Return the rig driving the current interface names, or refuse to guess."""
    resolution = resolve_can_channels(rig)
    if not resolution.ok:
        raise UserFacingError("; ".join(resolution.problems), resolution.remediation)
    if (resolution.left_channel, resolution.right_channel) == (
        rig.left_channel,
        rig.right_channel,
    ):
        return rig
    return rig.with_channels(resolution.left_channel, resolution.right_channel)


@dataclass(frozen=True)
class CanAssignment:
    """The interfaces chosen for each arm and the identities to remember them by."""

    left_channel: str
    right_channel: str
    left_can_id: str | None
    right_can_id: str | None

    def summary(self) -> str:
        return (
            f"left arm = {self.left_channel} ({describe_can_id(self.left_can_id)}), "
            f"right arm = {self.right_channel} ({describe_can_id(self.right_can_id)})"
        )


def _poll_stdin_line(timeout_s: float) -> str | None:
    """Wait up to ``timeout_s`` for one typed line without blocking longer."""
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout_s)
    except (OSError, ValueError, TypeError):
        time.sleep(timeout_s)
        return None
    if not ready:
        return None
    line = sys.stdin.readline()
    if not line:
        # EOF: stop selecting on a closed stdin, which would otherwise spin.
        time.sleep(timeout_s)
        return None
    return line


def _run_command(command: Sequence[str]) -> int:
    try:
        return subprocess.run(list(command), check=False).returncode
    except OSError:
        return 127


@dataclass
class CanFlowDependencies:
    """Injectable edges of the unplug-to-identify flow."""

    interfaces: Callable[[], Mapping[str, CanInterface]] = read_can_interfaces
    input: Callable[[str], str] = input
    output: Callable[[str], None] = print
    poll_line: Callable[[float], str | None] = _poll_stdin_line
    clock: Callable[[], float] = time.monotonic
    run_command: Callable[[Sequence[str]], int] = _run_command
    timeout_s: float = 120.0
    poll_s: float = 0.2
    settle_up_s: float = 5.0


class _ListRequested(Exception):
    """The operator asked for the numbered list instead of unplugging."""


def _ids(interfaces: Mapping[str, CanInterface]) -> dict[str, str | None]:
    return {
        name: stable_can_id(interface, interfaces.values())
        for name, interface in interfaces.items()
    }


def _label(name: str, can_id: str | None) -> str:
    return f"{name} · {describe_can_id(can_id)}"


def select_can_from_list(
    interfaces: Mapping[str, CanInterface],
    *,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
    names: Sequence[str] | None = None,
) -> CanAssignment:
    """Assign both arms from a numbered list, still remembering each adapter's identity."""
    ids = _ids(interfaces)
    candidates = list(names) if names is not None else list(interfaces)
    labels = {name: _label(name, ids.get(name)) for name in candidates}
    used: set[str] = set()
    left = select_numbered(
        "left arm CAN", candidates, used, input_fn=input_fn, output=output, labels=labels
    )
    right = select_numbered(
        "right arm CAN", candidates, used, input_fn=input_fn, output=output, labels=labels
    )
    return CanAssignment(left, right, ids.get(left), ids.get(right))


class CanIdentifier:
    """Interactive flow: unplug an arm's adapter, see which interface disappears."""

    def __init__(self, deps: CanFlowDependencies | None = None) -> None:
        self._deps = deps or CanFlowDependencies()

    # -- waiting ----------------------------------------------------------

    def _watch(
        self,
        condition: Callable[[Mapping[str, CanInterface]], bool],
        *,
        allow_list: bool,
    ) -> Mapping[str, CanInterface] | None:
        """Poll sysfs until ``condition`` holds; None after the timeout."""
        deps = self._deps
        deadline = deps.clock() + deps.timeout_s
        while True:
            current = deps.interfaces()
            if condition(current):
                return current
            if deps.clock() >= deadline:
                return None
            line = deps.poll_line(deps.poll_s)
            if line is not None and allow_list and line.strip().lower() == "list":
                raise _ListRequested

    def _keep_waiting(self, message: str, *, allow_list: bool) -> None:
        """Explain a timeout; Enter keeps waiting, ``list`` switches to the list."""
        deps = self._deps
        deps.output(message)
        if allow_list:
            answer = deps.input(
                "Press Enter to keep waiting, or type list to choose from a numbered list: "
            )
            if answer.strip().lower() == "list":
                raise _ListRequested
        else:
            deps.input("Press Enter to keep waiting (Ctrl-C cancels): ")

    def _wait_for(
        self,
        condition: Callable[[Mapping[str, CanInterface]], bool],
        timeout_message: str,
        *,
        allow_list: bool,
    ) -> Mapping[str, CanInterface]:
        while True:
            current = self._watch(condition, allow_list=allow_list)
            if current is not None:
                return current
            self._keep_waiting(timeout_message, allow_list=allow_list)

    # -- steps ------------------------------------------------------------

    def _unplugged(
        self,
        side: str,
        leave: str,
        before: Mapping[str, CanInterface],
        excluded: set[str],
        *,
        allow_list: bool,
    ) -> str:
        """Return the one interface that disappeared after the operator unplugged it."""
        deps = self._deps
        seconds = int(deps.timeout_s)
        watched = [name for name in before if name not in excluded]
        before_ids = _ids(before)

        def one_gone(now: Mapping[str, CanInterface]) -> bool:
            return any(name not in now for name in watched)

        while True:
            deps.output(f"Unplug the USB-CAN adapter of the {side} arm ({leave}).")
            if allow_list:
                deps.output(
                    "Setup continues on its own when the adapter disappears. Can't reach the "
                    "cables? Type list and press Enter to pick from a numbered list instead."
                )
            current = self._wait_for(
                one_gone,
                f"No CAN adapter was unplugged in {seconds} seconds. Unplug the USB cable of the "
                f"{side} arm's CAN adapter, at the computer or at the adapter.",
                allow_list=allow_list,
            )
            gone = [name for name in watched if name not in current]
            if len(gone) == 1:
                return gone[0]
            deps.output(
                f"More than one CAN adapter disappeared ({', '.join(gone)}). Plug them all back "
                f"in; then unplug only the {side} arm's adapter."
            )

            def all_back(
                now: Mapping[str, CanInterface],
                missing: tuple[str, ...] = tuple(gone),
            ) -> bool:
                return all(
                    self._present(name, before_ids.get(name), now) is not None for name in missing
                )

            self._wait_for(
                all_back,
                f"The adapters ({', '.join(gone)}) have not all come back after {seconds} "
                "seconds. Plug every CAN adapter back in.",
                allow_list=False,
            )

    def _is_present(
        self, name: str, can_id: str | None, interfaces: Mapping[str, CanInterface]
    ) -> bool:
        return self._present(name, can_id, interfaces) is not None

    @staticmethod
    def _present(
        name: str,
        can_id: str | None,
        interfaces: Mapping[str, CanInterface],
    ) -> str | None:
        """Return the current name of an adapter, by identity when it has one."""
        if can_id is None:
            return name if name in interfaces else None
        matches = [
            interface.name for interface in interfaces.values() if can_id_matches(can_id, interface)
        ]
        return matches[0] if len(matches) == 1 else None

    def _replugged(self, side: str, name: str, can_id: str | None) -> str:
        """Wait for the unplugged adapter to return, possibly under another name."""
        deps = self._deps
        seconds = int(deps.timeout_s)
        where = (
            "back into the same USB port"
            if can_id is not None and can_id.startswith("usb-port:")
            else "back in"
        )
        deps.output(f"Found it: the {side} arm uses {name} ({describe_can_id(can_id)}).")
        deps.output(f"Plug it {where} now.")
        current = self._wait_for(
            lambda now: self._present(name, can_id, now) is not None,
            f"The {side} arm's adapter has not come back after {seconds} seconds. Plug it "
            f"{where}.",
            allow_list=False,
        )
        returned = self._present(name, can_id, current)
        assert returned is not None
        if returned != name:
            deps.output(f"It came back as {returned}; setup will use that name.")
        self._ensure_up(returned)
        return returned

    def _ensure_up(self, name: str) -> None:
        """Give udev a moment to bring the interface UP, else explain how."""
        deps = self._deps

        def up(now: Mapping[str, CanInterface]) -> bool:
            interface = now.get(name)
            return interface is not None and interface.up

        settle_deadline = deps.clock() + deps.settle_up_s
        while True:
            if up(deps.interfaces()):
                return
            if deps.clock() >= settle_deadline:
                break
            deps.poll_line(deps.poll_s)
        command = bring_up_command(name)
        rendered = shlex.join(command)
        deps.output(
            f"{name} is DOWN. The arm needs it UP at {CAN_BITRATE // 1_000_000} Mbit/s; "
            "this command does that:"
        )
        deps.output(f"  {rendered}")
        if yes_no(
            "Run it now with sudo?",
            default=False,
            input_fn=deps.input,
            output=deps.output,
        ):
            if deps.run_command(command) != 0:
                deps.output(f"That command failed. Run it yourself in another terminal: {rendered}")
        else:
            deps.output("Run it in another terminal; setup continues when the interface is UP.")
        self._wait_for(
            up,
            f"{name} is still DOWN after {int(deps.timeout_s)} seconds. Run: {rendered}",
            allow_list=False,
        )

    # -- flow -------------------------------------------------------------

    def identify(self) -> CanAssignment:
        """Identify both arms' adapters by unplugging them, with a list escape hatch."""
        deps = self._deps
        while True:
            before = dict(deps.interfaces())
            if len(before) < 2:
                raise UserFacingError(
                    f"Setup found {len(before)} CAN interfaces, but the two YAM arms need two",
                    "Connect both CAN adapters, bring both interfaces UP, check "
                    "`ip -details link show type can`, then rerun the command",
                )
            ids = _ids(before)
            deps.output("")
            deps.output(
                "Next, identify each arm's USB-CAN adapter by unplugging it briefly. Nothing is "
                "sent to the arms."
            )
            two = len(before) == 2
            try:
                left_name = self._unplugged(
                    "LEFT",
                    "leave the RIGHT one plugged in" if two else "leave the others plugged in",
                    before,
                    set(),
                    allow_list=True,
                )
            except _ListRequested:
                deps.output("")
                return select_can_from_list(
                    deps.interfaces(), input_fn=deps.input, output=deps.output
                )
            left_id = ids[left_name]
            left_current = self._replugged("LEFT", left_name, left_id)
            if two:
                right_name = next(name for name in before if name != left_name)
                right_id = ids[right_name]
                right_current = self._present(right_name, right_id, deps.interfaces())
                if right_current is None:
                    deps.output(
                        f"The RIGHT arm's adapter ({right_name}) was unplugged too. Plug it back "
                        "in; identification then starts again."
                    )
                    self._wait_for(
                        functools.partial(self._is_present, right_name, right_id),
                        f"The RIGHT arm's adapter ({right_name}) has not come back after "
                        f"{int(deps.timeout_s)} seconds. Plug it back in.",
                        allow_list=False,
                    )
                    continue
                self._ensure_up(right_current)
            else:
                # Every adapter is plugged in again; identities are computed with
                # all of them present, exactly as later commands will see them.
                again = dict(deps.interfaces())
                again_ids = _ids(again)
                right_name = self._unplugged(
                    "RIGHT", "leave the others plugged in", again, {left_current}, allow_list=False
                )
                right_id = again_ids[right_name]
                right_current = self._replugged("RIGHT", right_name, right_id)
            assignment = CanAssignment(left_current, right_current, left_id, right_id)
            for side, can_id, current_name in (
                ("LEFT", left_id, left_current),
                ("RIGHT", right_id, right_current),
            ):
                if can_id is None:
                    deps.output(
                        f"Warning: the {side} arm's CAN interface has no USB identity, so it is "
                        f"saved by its name {current_name}, which can change after a reboot or "
                        "replug."
                    )
            deps.output("")
            deps.output(assignment.summary())
            if left_current == right_current:
                deps.output("Both arms resolved to the same interface; starting again.")
                continue
            if yes_no("Is this correct?", default=True, input_fn=deps.input, output=deps.output):
                return assignment
            deps.output("Starting CAN identification again.")


def stdin_interactive() -> bool:
    """Whether a person is typing into this terminal."""
    try:
        return os.isatty(sys.stdin.fileno()) and os.isatty(sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        return False
