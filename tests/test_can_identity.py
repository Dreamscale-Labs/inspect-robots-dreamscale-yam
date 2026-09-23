from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from dreamscale_yam.can_identity import (
    CanFlowDependencies,
    CanIdentifier,
    CanInterface,
    bring_up_command,
    can_id_matches,
    describe_can_id,
    read_can_interfaces,
    resolve_can_channels,
    resolve_rig_can,
    select_can_from_list,
    stable_can_id,
)
from dreamscale_yam.config import RigConfig
from dreamscale_yam.errors import UserFacingError

# -- fake sysfs ---------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def _sysfs(tmp_path: Path, adapters: list[dict[str, object]]) -> Path:
    """Build a sysfs tree shaped like a7's: adapters behind a hub on usb1."""
    root_hub = tmp_path / "devices" / "pci0000:00" / "0000:65:00.3" / "usb1"
    _write(root_hub / "idVendor", "1d6b")
    _write(root_hub / "serial", "0000:65:00.3")  # must never be mistaken for an adapter
    hub = root_hub / "1-5"
    _write(hub / "idVendor", "05e3")
    net = tmp_path / "class" / "net"
    for adapter in adapters:
        port = str(adapter["port"])
        usb = hub / port
        _write(usb / "idVendor", "1d50")
        if adapter.get("serial"):
            _write(usb / "serial", str(adapter["serial"]))
        interface_dir = usb / f"{port}:1.0"
        interface_dir.mkdir(parents=True, exist_ok=True)
        entry = net / str(adapter["name"])
        _write(entry / "type", "280")
        _write(entry / "operstate", "up" if adapter.get("up", True) else "down")
        _write(entry / "dev_port", str(adapter.get("dev_port", 0)))
        (entry / "device").symlink_to(interface_dir)
    _write(net / "enp1s0" / "type", "1")
    _write(net / "lo" / "type", "772")
    vcan = net / "vcan0"
    _write(vcan / "type", "280")
    _write(vcan / "operstate", "unknown")
    return net


def test_sysfs_identity_reads_the_adapter_serial_never_the_hub_serial(tmp_path: Path) -> None:
    net = _sysfs(
        tmp_path,
        [
            {"name": "can_follower_l", "port": "1-5.4", "serial": "208137AD45465006"},
            {"name": "can_follower_r", "port": "1-5.1", "serial": "206437AA45465006"},
            {"name": "can2", "port": "1-5.2", "up": False},
        ],
    )

    interfaces = read_can_interfaces(net)

    assert list(interfaces) == ["can2", "can_follower_l", "can_follower_r", "vcan0"]
    left = interfaces["can_follower_l"]
    assert (left.usb_serial, left.usb_port, left.up, left.channel) == (
        "208137AD45465006",
        "1-5.4",
        True,
        0,
    )
    assert interfaces["can2"].usb_serial is None
    assert interfaces["can2"].usb_port == "1-5.2"
    assert interfaces["can2"].up is False
    assert interfaces["vcan0"] == CanInterface("vcan0", up=False)
    ids = {name: stable_can_id(i, interfaces.values()) for name, i in interfaces.items()}
    assert ids == {
        "can_follower_l": "usb-serial:208137AD45465006",
        "can_follower_r": "usb-serial:206437AA45465006",
        "can2": "usb-port:1-5.2",
        "vcan0": None,
    }


def test_missing_sysfs_reads_as_no_interfaces(tmp_path: Path) -> None:
    assert read_can_interfaces(tmp_path / "absent") == {}


def test_duplicate_serial_falls_back_to_port_and_channel_suffixes_are_kept() -> None:
    clones = [
        CanInterface("can0", True, usb_serial="SAME", usb_port="1-2"),
        CanInterface("can1", True, usb_serial="SAME", usb_port="1-3"),
        CanInterface("can2", True, usb_serial="DUAL", usb_port="1-4", channel=0),
        CanInterface("can3", True, usb_serial="DUAL", usb_port="1-4", channel=1),
    ]

    assert [stable_can_id(item, clones) for item in clones] == [
        "usb-port:1-2",
        "usb-port:1-3",
        "usb-serial:DUAL",
        "usb-serial:DUAL#1",
    ]
    assert can_id_matches("usb-serial:DUAL#1", clones[3])
    assert not can_id_matches("usb-serial:DUAL#1", clones[2])
    assert describe_can_id("usb-serial:DUAL#1") == "USB serial DUAL, channel 1"
    assert describe_can_id("usb-port:1-2") == "USB port 1-2"
    assert describe_can_id(None) == "no USB identity"


# -- resolution at use time ---------------------------------------------------


def _identified(rig: RigConfig) -> RigConfig:
    return rig.with_can_assignment(
        left_channel="can0",
        right_channel="can1",
        left_can_id="usb-serial:LEFT",
        right_can_id="usb-serial:RIGHT",
    )


def test_swapped_kernel_names_still_drive_the_correct_arm(rig: RigConfig) -> None:
    identified = _identified(rig)
    swapped = {
        "can0": CanInterface("can0", True, usb_serial="RIGHT", usb_port="1-3"),
        "can1": CanInterface("can1", True, usb_serial="LEFT", usb_port="1-2"),
    }

    resolution = resolve_can_channels(identified, swapped)

    assert resolution.ok
    assert (resolution.left_channel, resolution.right_channel) == ("can1", "can0")
    assert "the left arm's adapter (USB serial LEFT) is now can1 (saved as can0)" in (
        resolution.notes
    )


def test_missing_adapter_is_a_clear_failure_with_a_next_step(rig: RigConfig) -> None:
    resolution = resolve_can_channels(
        _identified(rig),
        {"can0": CanInterface("can0", True, usb_serial="RIGHT")},
    )

    assert not resolution.ok
    assert resolution.problems == (
        "The LEFT arm's CAN adapter (USB serial LEFT) is not connected",
    )
    assert "Plug the LEFT arm's USB-CAN adapter back in" in resolution.remediation
    assert "identify-can" in resolution.remediation


def test_port_identity_asks_for_the_same_port(rig: RigConfig) -> None:
    identified = rig.with_can_assignment(
        left_channel="can0",
        right_channel="can1",
        left_can_id="usb-port:1-2",
        right_can_id="usb-port:1-3",
    )

    resolution = resolve_can_channels(
        identified, {"can1": CanInterface("can1", True, usb_port="1-3")}
    )

    assert resolution.problems == ("The LEFT arm's CAN adapter (USB port 1-2) is not connected",)
    assert "into the same USB port" in resolution.remediation


def test_ambiguous_or_shared_resolution_never_guesses(rig: RigConfig) -> None:
    identified = _identified(rig)
    duplicated = {
        "can0": CanInterface("can0", True, usb_serial="LEFT"),
        "can1": CanInterface("can1", True, usb_serial="LEFT"),
        "can2": CanInterface("can2", True, usb_serial="RIGHT"),
    }
    assert "More than one connected CAN interface" in resolve_can_channels(
        identified, duplicated
    ).problems[0]

    same = rig.with_can_assignment(
        left_channel="can0",
        right_channel="can1",
        left_can_id="usb-serial:LEFT",
        right_can_id="usb-port:1-2",
    )
    both = resolve_can_channels(
        same, {"can0": CanInterface("can0", True, usb_serial="LEFT", usb_port="1-2")}
    )
    assert both.problems == (
        "Both arms resolve to the same CAN interface can0; the left and right arms need two "
        "different adapters",
    )


def test_rig_without_identities_resolves_to_saved_names_without_reading_sysfs(
    rig: RigConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "dreamscale_yam.can_identity.read_can_interfaces",
        lambda: (_ for _ in ()).throw(AssertionError("sysfs read")),
    )

    resolution = resolve_can_channels(rig)

    assert resolution.ok
    assert (resolution.left_channel, resolution.right_channel) == ("can0", "can1")
    assert resolve_rig_can(rig) is rig


def test_resolve_rig_can_returns_current_names_or_refuses(
    rig: RigConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    identified = _identified(rig)
    current = {
        "can_a": CanInterface("can_a", True, usb_serial="LEFT"),
        "can_b": CanInterface("can_b", True, usb_serial="RIGHT"),
    }
    monkeypatch.setattr("dreamscale_yam.can_identity.read_can_interfaces", lambda: current)

    resolved = resolve_rig_can(identified)

    assert (resolved.left_channel, resolved.right_channel) == ("can_a", "can_b")
    assert resolved.yam_kwargs()["left_channel"] == "can_a"
    assert resolved.left_can_id == "usb-serial:LEFT"
    del current["can_a"]
    with pytest.raises(UserFacingError, match="LEFT arm's CAN adapter"):
        resolve_rig_can(identified)


# -- the unplug flow -----------------------------------------------------------


class FakeHost:
    """Scripted CAN adapters, a fake clock and scripted keyboard input."""

    def __init__(
        self,
        adapters: list[CanInterface],
        events: dict[int, Callable[[FakeHost], None]] | None = None,
        lines: dict[int, str] | None = None,
        answers: list[str] | None = None,
    ) -> None:
        self.current = {adapter.name: adapter for adapter in adapters}
        self.events = dict(events or {})
        self.lines = dict(lines or {})
        self.answers = list(answers or [])
        self.prompts: list[str] = []
        self.output: list[str] = []
        self.commands: list[list[str]] = []
        self.now = 0.0
        self.polls = 0

    def interfaces(self) -> dict[str, CanInterface]:
        return dict(self.current)

    def poll_line(self, timeout: float) -> str | None:
        self.now += timeout
        self.polls += 1
        event = self.events.pop(self.polls, None)
        if event is not None:
            event(self)
        return self.lines.pop(self.polls, None)

    def input(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {prompt}")
        return self.answers.pop(0)

    def run_command(self, command: list[str]) -> int:
        self.commands.append(list(command))
        name = command[4]
        self.current[name] = CanInterface(**{**self.current[name].__dict__, "up": True})
        return 0

    def deps(self) -> CanFlowDependencies:
        return CanFlowDependencies(
            interfaces=self.interfaces,
            input=self.input,
            output=self.output.append,
            poll_line=self.poll_line,
            clock=lambda: self.now,
            run_command=self.run_command,
        )

    @property
    def text(self) -> str:
        return "\n".join(self.output)


def unplug(*names: str) -> Callable[[FakeHost], None]:
    def event(host: FakeHost) -> None:
        for name in names:
            host.current.pop(name)

    return event


def plug(*interfaces: CanInterface) -> Callable[[FakeHost], None]:
    def event(host: FakeHost) -> None:
        for interface in interfaces:
            host.current[interface.name] = interface

    return event


LEFT = CanInterface("can0", True, usb_serial="208137AD45465006", usb_port="1-5.4")
RIGHT = CanInterface("can1", True, usb_serial="206437AA45465006", usb_port="1-5.1")


def test_happy_path_identifies_left_by_unplug_and_right_as_the_remaining_adapter() -> None:
    host = FakeHost(
        [LEFT, RIGHT],
        events={3: unplug("can0"), 6: plug(LEFT)},
        answers=[""],  # Is this correct? [Y/n]
    )

    assignment = CanIdentifier(host.deps()).identify()

    assert assignment.left_channel == "can0"
    assert assignment.right_channel == "can1"
    assert assignment.left_can_id == "usb-serial:208137AD45465006"
    assert assignment.right_can_id == "usb-serial:206437AA45465006"
    assert "Unplug the USB-CAN adapter of the LEFT arm (leave the RIGHT one plugged in)." in (
        host.output
    )
    assert "type list" in host.text.lower()
    assert "Found it: the LEFT arm uses can0 (USB serial 208137AD45465006)." in host.output
    assert "Plug it back in now." in host.output
    assert (
        "left arm = can0 (USB serial 208137AD45465006), right arm = can1 "
        "(USB serial 206437AA45465006)"
    ) in host.output
    # Auto-continues: the only question was the final confirmation.
    assert host.prompts == ["Is this correct? [Y/n] "]
    assert host.commands == []


def test_unplug_timeout_explains_and_keeps_waiting_on_enter() -> None:
    host = FakeHost(
        [LEFT, RIGHT],
        events={700: unplug("can0"), 705: plug(LEFT)},
        answers=["", ""],  # keep waiting, then confirm
    )

    assignment = CanIdentifier(host.deps()).identify()

    assert assignment.left_channel == "can0"
    assert (
        "No CAN adapter was unplugged in 120 seconds. Unplug the USB cable of the LEFT arm's "
        "CAN adapter, at the computer or at the adapter."
    ) in host.output
    assert host.prompts[0].startswith("Press Enter to keep waiting, or type list")


def test_timeout_prompt_offers_the_list_instead() -> None:
    host = FakeHost([LEFT, RIGHT], answers=["list", "2", "1"])

    assignment = CanIdentifier(host.deps()).identify()

    assert (assignment.left_channel, assignment.right_channel) == ("can1", "can0")
    assert assignment.left_can_id == "usb-serial:206437AA45465006"


def test_two_adapters_disappearing_at_once_is_retried() -> None:
    host = FakeHost(
        [LEFT, RIGHT],
        events={
            2: unplug("can0", "can1"),
            4: plug(LEFT, RIGHT),
            6: unplug("can1"),
            8: plug(RIGHT),
        },
        answers=[""],
    )

    assignment = CanIdentifier(host.deps()).identify()

    assert "More than one CAN adapter disappeared (can0, can1)." in host.text
    assert host.text.count("Unplug the USB-CAN adapter of the LEFT arm") == 2
    # The operator then unplugged can1, so can1 is the LEFT arm.
    assert (assignment.left_channel, assignment.right_channel) == ("can1", "can0")
    assert assignment.left_can_id == "usb-serial:206437AA45465006"


def test_adapter_returning_under_a_different_name_is_followed_by_identity() -> None:
    renamed = CanInterface("can2", True, usb_serial=LEFT.usb_serial, usb_port="1-5.3")
    host = FakeHost(
        [LEFT, RIGHT],
        events={2: unplug("can0"), 4: plug(renamed)},
        answers=[""],
    )

    assignment = CanIdentifier(host.deps()).identify()

    assert assignment.left_channel == "can2"
    assert assignment.left_can_id == "usb-serial:208137AD45465006"
    assert "It came back as can2; setup will use that name." in host.output


def test_adapter_returning_down_prints_and_can_run_the_exact_command() -> None:
    down = CanInterface("can0", False, usb_serial=LEFT.usb_serial, usb_port=LEFT.usb_port)
    host = FakeHost(
        [LEFT, RIGHT],
        events={2: unplug("can0"), 4: plug(down)},
        answers=["y", ""],  # run it with sudo, then confirm
    )

    assignment = CanIdentifier(host.deps()).identify()

    assert assignment.left_channel == "can0"
    expected = ["sudo", "ip", "link", "set", "can0", "up", "type", "can", "bitrate", "1000000"]
    assert bring_up_command("can0") == expected
    assert host.commands == [expected]
    assert "  sudo ip link set can0 up type can bitrate 1000000" in host.output
    assert "Run it now with sudo? [y/N] " in host.prompts


def test_adapter_returning_down_waits_when_the_operator_declines_sudo() -> None:
    down = CanInterface("can0", False, usb_serial=LEFT.usb_serial, usb_port=LEFT.usb_port)
    host = FakeHost(
        [LEFT, RIGHT],
        events={2: unplug("can0"), 4: plug(down), 80: plug(LEFT)},
        answers=["", ""],  # decline sudo, then confirm
    )

    assignment = CanIdentifier(host.deps()).identify()

    assert assignment.left_channel == "can0"
    assert host.commands == []
    assert "Run it in another terminal; setup continues when the interface is UP." in host.output


def test_list_escape_hatch_at_the_first_prompt_still_remembers_identity() -> None:
    host = FakeHost([LEFT, RIGHT], lines={2: "list\n"}, answers=["2", "1"])

    assignment = CanIdentifier(host.deps()).identify()

    assert (assignment.left_channel, assignment.right_channel) == ("can1", "can0")
    assert (assignment.left_can_id, assignment.right_can_id) == (
        "usb-serial:206437AA45465006",
        "usb-serial:208137AD45465006",
    )
    assert "  1. can0 · USB serial 208137AD45465006" in host.output


def test_adapters_without_serial_are_remembered_by_usb_port() -> None:
    left = CanInterface("can0", True, usb_port="1-2")
    right = CanInterface("can1", True, usb_port="1-3")
    host = FakeHost([left, right], events={2: unplug("can0"), 4: plug(left)}, answers=[""])

    assignment = CanIdentifier(host.deps()).identify()

    assert (assignment.left_can_id, assignment.right_can_id) == ("usb-port:1-2", "usb-port:1-3")
    assert "Plug it back into the same USB port now." in host.output


def test_non_usb_adapter_falls_back_to_its_name_with_a_warning() -> None:
    left = CanInterface("can0", True)
    right = CanInterface("can1", True, usb_serial="RIGHT")
    host = FakeHost([left, right], events={2: unplug("can0"), 4: plug(left)}, answers=[""])

    assignment = CanIdentifier(host.deps()).identify()

    assert assignment.left_can_id is None
    assert assignment.right_can_id == "usb-serial:RIGHT"
    assert any("has no USB identity" in line for line in host.output)


def test_three_adapters_repeat_the_unplug_for_right() -> None:
    spare = CanInterface("can2", True, usb_serial="SPARE")
    host = FakeHost(
        [LEFT, RIGHT, spare],
        events={2: unplug("can0"), 4: plug(LEFT), 6: unplug("can2"), 8: plug(spare)},
        answers=[""],
    )

    assignment = CanIdentifier(host.deps()).identify()

    assert (assignment.left_channel, assignment.right_channel) == ("can0", "can2")
    assert assignment.right_can_id == "usb-serial:SPARE"
    assert "Unplug the USB-CAN adapter of the LEFT arm (leave the others plugged in)." in (
        host.output
    )
    assert "Unplug the USB-CAN adapter of the RIGHT arm (leave the others plugged in)." in (
        host.output
    )


def test_rejected_summary_starts_again() -> None:
    host = FakeHost(
        [LEFT, RIGHT],
        events={2: unplug("can0"), 4: plug(LEFT), 12: unplug("can1"), 14: plug(RIGHT)},
        answers=["n", ""],
    )

    assignment = CanIdentifier(host.deps()).identify()

    assert "Starting CAN identification again." in host.output
    assert assignment.left_channel == "can1"


def test_fewer_than_two_adapters_is_refused() -> None:
    host = FakeHost([LEFT])

    with pytest.raises(UserFacingError, match="found 1 CAN interfaces"):
        CanIdentifier(host.deps()).identify()


def test_list_selection_marks_interfaces_without_identity() -> None:
    output: list[str] = []
    answers = iter(["1", "2"])

    assignment = select_can_from_list(
        {"can0": CanInterface("can0", True), "can1": RIGHT},
        input_fn=lambda _prompt: next(answers),
        output=output.append,
    )

    assert assignment.left_can_id is None
    assert "  1. can0 · no USB identity" in output


def test_failed_sudo_command_is_explained_and_setup_waits_for_up() -> None:
    down = CanInterface("can0", False, usb_serial=LEFT.usb_serial, usb_port=LEFT.usb_port)
    host = FakeHost(
        [LEFT, RIGHT],
        events={2: unplug("can0"), 4: plug(down), 60: plug(LEFT)},
        answers=["y", ""],
    )
    host.run_command = lambda command: host.commands.append(list(command)) or 1  # type: ignore

    CanIdentifier(host.deps()).identify()

    assert (
        "That command failed. Run it yourself in another terminal: sudo ip link set can0 up "
        "type can bitrate 1000000"
    ) in host.output


def test_replug_timeout_keeps_waiting_on_enter() -> None:
    host = FakeHost(
        [LEFT, RIGHT],
        events={2: unplug("can0"), 700: plug(LEFT)},
        answers=["", ""],
    )

    assert CanIdentifier(host.deps()).identify().left_channel == "can0"
    assert (
        "The LEFT arm's adapter has not come back after 120 seconds. Plug it back in."
        in host.output
    )
    assert host.prompts[0] == "Press Enter to keep waiting (Ctrl-C cancels): "


def test_right_adapter_disappearing_during_the_flow_starts_again() -> None:
    host = FakeHost(
        [LEFT, RIGHT],
        events={
            2: unplug("can0"),
            3: unplug("can1"),
            4: plug(LEFT),
            10: plug(RIGHT),
            12: unplug("can0"),
            14: plug(LEFT),
        },
        answers=[""],
    )

    assignment = CanIdentifier(host.deps()).identify()

    assert (
        "The RIGHT arm's adapter (can1) was unplugged too. Plug it back in; identification then "
        "starts again."
    ) in host.output
    assert (assignment.left_channel, assignment.right_channel) == ("can0", "can1")


def test_stdin_polling_returns_a_typed_line_or_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import os

    from dreamscale_yam import can_identity

    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "r") as reader:
        monkeypatch.setattr(can_identity.sys, "stdin", reader)
        assert can_identity._poll_stdin_line(0.01) is None
        os.write(write_fd, b"list\n")
        assert can_identity._poll_stdin_line(1.0) == "list\n"
        os.close(write_fd)
        assert can_identity._poll_stdin_line(0.01) is None  # EOF never spins
    monkeypatch.setattr(can_identity.sys, "stdin", io.StringIO("unselectable"))
    assert can_identity._poll_stdin_line(0.01) is None
    assert can_identity.stdin_interactive() is False


def test_missing_command_reports_a_failure_code() -> None:
    from dreamscale_yam.can_identity import _run_command

    assert _run_command(["/nonexistent/dreamscale-yam-test-binary"]) == 127
