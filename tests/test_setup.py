from __future__ import annotations

from pathlib import Path

import pytest
import tomli_w
from dreamscale import errors as dreamscale_errors

from dreamscale_yam.config import load_rig
from dreamscale_yam.errors import UserFacingError
from dreamscale_yam.setup_command import SetupDependencies, _login, discover_cameras, setup


def test_default_login_uses_the_locked_sdk_and_suppresses_generic_next_steps(monkeypatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(
        "dreamscale_yam.setup_command.run_login",
        lambda *, print_next_steps: calls.append(print_next_steps),
    )

    _login()

    assert calls == [False]


def test_setup_prompts_only_for_unavoidable_assignments_and_geometry(isolated_paths: Path) -> None:
    answers = iter(
        [
            "1",
            "2",
            "3",  # top, left, right cameras
            "1",
            "2",  # left and right CAN
            "y",  # opt in to predictive collision geometry
            "-0.25 0 0",
            "0.25 0 0",
            "0",
            "3.14159",
            "0",
        ]
    )
    prompts: list[str] = []
    login_calls: list[bool] = []
    deps = SetupDependencies(
        discover_cameras=lambda: [
            "/dev/v4l/by-id/cam-a",
            "/dev/v4l/by-id/cam-b",
            "/dev/v4l/by-id/cam-c",
        ],
        discover_can=lambda: ["can0", "can1"],
        authenticated=lambda: False,
        login=lambda: login_calls.append(True),
        input=lambda prompt: (prompts.append(prompt), next(answers))[1],
        output=lambda _line: None,
    )

    path = setup(deps=deps)
    rig = load_rig(path)

    from dreamscale_yam import config

    assert path == config.rig_path("default")
    assert (rig.top_camera, rig.left_camera, rig.right_camera) == (
        "/dev/v4l/by-id/cam-a",
        "/dev/v4l/by-id/cam-b",
        "/dev/v4l/by-id/cam-c",
    )
    assert (rig.left_channel, rig.right_channel) == ("can0", "can1")
    assert rig.collision_guardrail is True
    assert login_calls == [True]
    assert len(prompts) == 11


def test_setup_reports_the_saved_rig_before_login_failure(isolated_paths: Path) -> None:
    answers = iter(["1", "2", "3", "1", "2", "n"])
    output: list[str] = []
    failure = dreamscale_errors.catalog("cli_login_start_failed", detail="HTTP 503")
    deps = SetupDependencies(
        discover_cameras=lambda: [
            "/dev/v4l/by-id/cam-a",
            "/dev/v4l/by-id/cam-b",
            "/dev/v4l/by-id/cam-c",
        ],
        discover_can=lambda: ["can0", "can1"],
        authenticated=lambda: False,
        login=lambda: (_ for _ in ()).throw(failure),
        input=lambda _prompt: next(answers),
        output=output.append,
    )

    with pytest.raises(dreamscale_errors.DreamscaleError):
        setup(deps=deps)

    assert load_rig().top_camera == "/dev/v4l/by-id/cam-a"
    assert any(line.startswith("Confirmed rig written to ") for line in output)

    recovered = setup(
        deps=SetupDependencies(
            discover_cameras=lambda: (_ for _ in ()).throw(AssertionError("rediscovered")),
            discover_can=lambda: (_ for _ in ()).throw(AssertionError("rediscovered")),
            authenticated=lambda: True,
            login=lambda: (_ for _ in ()).throw(AssertionError("login repeated")),
            input=lambda _prompt: (_ for _ in ()).throw(AssertionError("prompt repeated")),
            output=lambda _line: None,
        )
    )
    assert recovered.exists()


def test_setup_explains_and_allows_skipping_collision_geometry(isolated_paths: Path) -> None:
    answers = iter(["1", "2", "3", "1", "2", "n"])
    prompts: list[str] = []
    output: list[str] = []
    deps = SetupDependencies(
        discover_cameras=lambda: [
            "/dev/v4l/by-id/cam-a",
            "/dev/v4l/by-id/cam-b",
            "/dev/v4l/by-id/cam-c",
        ],
        discover_can=lambda: ["can0", "can1"],
        authenticated=lambda: True,
        login=lambda: None,
        input=lambda prompt: (prompts.append(prompt), next(answers))[1],
        output=output.append,
    )

    rig = load_rig(setup(deps=deps))

    assert rig.collision_guardrail is False
    assert rig.collision_table is False
    assert rig.collision_left_base_pos is None
    assert rig.collision_right_base_pos is None
    assert rig.collision_table_height is None
    assert len(prompts) == 6
    explanation = "\n".join(output)
    assert "optional" in explanation.lower()
    assert "left and right arm-base x y z" in explanation.lower()
    assert "predictive collision" in explanation.lower()


def test_setup_camera_failure_is_plain_and_actionable(isolated_paths: Path) -> None:
    deps = SetupDependencies(
        discover_cameras=lambda: [],
        discover_can=lambda: ["can0", "can1"],
        authenticated=lambda: True,
        login=lambda: None,
        input=lambda _prompt: "",
        output=lambda _line: None,
    )

    with pytest.raises(UserFacingError) as caught:
        setup(deps=deps)

    assert "found 0 usable color cameras" in str(caught.value)
    assert "Connect and power" in caught.value.next_step


def test_setup_is_idempotent_and_does_not_prompt_when_rig_exists(rig, isolated_paths: Path) -> None:
    from dreamscale_yam.config import save_rig

    expected = save_rig(rig, profile="jay-rig-1")
    deps = SetupDependencies(
        discover_cameras=lambda: (_ for _ in ()).throw(AssertionError("discovery called")),
        discover_can=lambda: (_ for _ in ()).throw(AssertionError("discovery called")),
        authenticated=lambda: True,
        login=lambda: (_ for _ in ()).throw(AssertionError("login called")),
        input=lambda _prompt: (_ for _ in ()).throw(AssertionError("prompted")),
        output=lambda _line: None,
    )

    assert setup(deps=deps) == expected
    assert load_rig(expected) == rig


def test_setup_preserves_advanced_step_limits_without_prompting(rig, isolated_paths: Path) -> None:
    from dreamscale_yam.config import RigConfig, save_rig

    configured = RigConfig(**{**rig.as_dict(), "step_limits": (0.5,) * 14})
    expected = save_rig(configured, profile="default")
    deps = SetupDependencies(
        discover_cameras=lambda: (_ for _ in ()).throw(AssertionError("discovery called")),
        discover_can=lambda: (_ for _ in ()).throw(AssertionError("discovery called")),
        authenticated=lambda: True,
        login=lambda: (_ for _ in ()).throw(AssertionError("login called")),
        input=lambda _prompt: (_ for _ in ()).throw(AssertionError("prompted")),
        output=lambda _line: None,
    )

    assert setup(deps=deps) == expected
    assert load_rig(expected).step_limits == (0.5,) * 14


def test_v0117_rig_parses_and_setup_does_not_rewrite_or_prompt(isolated_paths: Path) -> None:
    from dreamscale_yam import config

    fixture = Path(__file__).parent / "fixtures" / "v0.1.17-rig.toml"
    expected = fixture.read_bytes()
    path = config.rig_path("default")
    path.parent.mkdir(parents=True)
    path.write_bytes(expected)
    deps = SetupDependencies(
        discover_cameras=lambda: (_ for _ in ()).throw(AssertionError("discovery called")),
        discover_can=lambda: (_ for _ in ()).throw(AssertionError("discovery called")),
        authenticated=lambda: True,
        login=lambda: (_ for _ in ()).throw(AssertionError("login called")),
        input=lambda _prompt: (_ for _ in ()).throw(AssertionError("prompted")),
        output=lambda _line: None,
    )

    assert load_rig(path).step_limits == config.STRICT_STEP_LIMITS
    assert setup(deps=deps) == path
    assert path.read_bytes() == expected


def test_setup_migrates_generated_xml_bounds_without_reasking_for_rig_assignments(
    rig, isolated_paths: Path
) -> None:
    from dreamscale_yam import config

    path = config.rig_path("default")
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        **rig.as_dict(),
        "schema_version": 1,
        "joint_low": list(
            (-2.61799, 0.0, 0.0, -1.5708, -1.5708, -2.0944, 0.0) * 2
        ),
        "joint_high": list(
            (3.05433, 3.65, 3.66519, 1.5708, 1.5708, 2.0944, 1.0) * 2
        ),
    }
    path.write_text(tomli_w.dumps({"rig": legacy}), encoding="utf-8")
    output: list[str] = []
    deps = SetupDependencies(
        discover_cameras=lambda: (_ for _ in ()).throw(AssertionError("discovery called")),
        discover_can=lambda: (_ for _ in ()).throw(AssertionError("discovery called")),
        authenticated=lambda: True,
        login=lambda: (_ for _ in ()).throw(AssertionError("login called")),
        input=lambda _prompt: (_ for _ in ()).throw(AssertionError("prompted")),
        output=output.append,
    )

    assert setup(deps=deps) == path

    migrated = load_rig(path)
    assert migrated.schema_version == 2
    assert migrated.top_camera == rig.top_camera
    assert migrated.left_camera == rig.left_camera
    assert migrated.right_camera == rig.right_camera
    assert migrated.left_channel == rig.left_channel
    assert migrated.right_channel == rig.right_channel
    assert migrated.joint_low[1:3] == (-0.15, -0.15)
    assert "updated the generated i2rt joint bounds" in "\n".join(output).lower()


@pytest.mark.parametrize("customization", ["gripper", "rig-key", "top-level"])
def test_setup_refuses_to_silently_migrate_a_customized_v1_rig(
    rig, isolated_paths: Path, customization: str
) -> None:
    from dreamscale_yam import config

    path = config.rig_path("default")
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        **rig.as_dict(),
        "schema_version": 1,
        "joint_low": list(
            (-2.61799, 0.0, 0.0, -1.5708, -1.5708, -2.0944, 0.0) * 2
        ),
        "joint_high": list(
            (3.05433, 3.65, 3.66519, 1.5708, 1.5708, 2.0944, 1.0) * 2
        ),
    }
    payload: dict[str, object] = {"rig": legacy}
    if customization == "gripper":
        legacy["gripper_type"] = "LINEAR_3507"
    elif customization == "rig-key":
        legacy["custom_setting"] = True
    else:
        payload["custom"] = {"setting": True}
    original = tomli_w.dumps(payload)
    path.write_text(original, encoding="utf-8")
    deps = SetupDependencies(
        discover_cameras=lambda: (_ for _ in ()).throw(AssertionError("discovery called")),
        discover_can=lambda: (_ for _ in ()).throw(AssertionError("discovery called")),
        authenticated=lambda: True,
        login=lambda: (_ for _ in ()).throw(AssertionError("login called")),
        input=lambda _prompt: (_ for _ in ()).throw(AssertionError("prompted")),
        output=lambda _line: None,
    )

    with pytest.raises(ValueError, match="unsupported rig format"):
        setup(deps=deps)

    assert path.read_text(encoding="utf-8") == original


def test_discovery_prefers_mixed_documented_realsense_backends_without_index0_assumption() -> None:
    cameras = discover_cameras(
        v4l_devices=[
            {
                "source": "/dev/v4l/by-id/d435-video-index0",
                "physical_id": "usb:1-1",
                "model": "Intel RealSense D435",
            },
            {
                "source": "/dev/v4l/by-path/pci-usb-1-2-video-index4",
                "physical_id": "usb:1-2",
                "model": "Intel RealSense D405",
            },
            {
                "source": "/dev/v4l/by-path/pci-usb-1-3-video-index4",
                "physical_id": "usb:1-3",
                "model": "Intel RealSense D405",
            },
        ],
        realsense_devices=[
            {
                "source": "realsense:D435-SERIAL",
                "physical_id": "usb:1-1",
                "model": "Intel RealSense D435",
            },
            {
                "source": "realsense:D405-LEFT",
                "physical_id": "usb:1-2",
                "model": "Intel RealSense D405",
            },
            {
                "source": "realsense:D405-RIGHT",
                "physical_id": "usb:1-3",
                "model": "Intel RealSense D405",
            },
        ],
    )

    assert cameras == [
        "/dev/v4l/by-id/d435-video-index0",
        "realsense:D405-LEFT",
        "realsense:D405-RIGHT",
    ]


def test_discovery_accepts_a_color_capable_by_path_node_when_by_id_is_ambiguous() -> None:
    cameras = discover_cameras(
        v4l_devices=[
            {
                "source": "/dev/v4l/by-path/pci-usb-1-2-video-index4",
                "physical_id": "usb:1-2",
                "model": "unknown",
            }
        ],
        realsense_devices=[],
    )

    assert cameras == ["/dev/v4l/by-path/pci-usb-1-2-video-index4"]


def test_discovery_joins_realsense_device_and_asic_serial_namespaces() -> None:
    cameras = discover_cameras(
        v4l_devices=[
            {
                "source": "/dev/v4l/by-id/usb-Intel_D405_ASIC-123-video-index4",
                "physical_id": "node:/dev/video8",
                "model": "8086:0b5b",
                "serial": "ASIC-123",
            }
        ],
        realsense_devices=[
            {
                "source": "realsense:DEVICE-456",
                "physical_id": "serial:DEVICE-456",
                "model": "Intel RealSense D405",
                "serial": "DEVICE-456",
                "asic_serial": "ASIC-123",
            }
        ],
    )

    assert cameras == ["realsense:DEVICE-456"]


def test_discovery_never_collapses_two_physical_cameras_with_a_duplicated_serial() -> None:
    cameras = discover_cameras(
        v4l_devices=[
            {
                "source": "/dev/v4l/by-path/pci-usb-1-2-video-index4",
                "physical_id": "usb:1-2",
                "model": "8086:0b5b",
                "serial": "DUPLICATED",
            },
            {
                "source": "/dev/v4l/by-path/pci-usb-1-3-video-index4",
                "physical_id": "usb:1-3",
                "model": "8086:0b5b",
                "serial": "DUPLICATED",
            },
        ],
        realsense_devices=[],
    )

    assert cameras == [
        "/dev/v4l/by-path/pci-usb-1-2-video-index4",
        "/dev/v4l/by-path/pci-usb-1-3-video-index4",
    ]


# -- v0.1.21: camera preview and CAN identity ---------------------------------

from dreamscale_yam.camera_preview import CameraCandidate  # noqa: E402
from dreamscale_yam.can_identity import CanAssignment, CanInterface  # noqa: E402
from dreamscale_yam.config import rig_path, save_rig  # noqa: E402
from dreamscale_yam.rig_lock import hold_rig_locks  # noqa: E402
from dreamscale_yam.setup_command import (  # noqa: E402
    camera_preview_command,
    discover_camera_candidates,
    identify_can,
)

CANDIDATES = [
    CameraCandidate("/dev/v4l/by-id/d435-video-index0", "Intel RealSense D435", "146322072458"),
    CameraCandidate("realsense:261022277065", "Intel RealSense D405", "261022277065"),
    CameraCandidate("realsense:261022270000", "Intel RealSense D405", "261022270000"),
]


class FakePreview:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.assigned: list[tuple[str, str]] = []
        self.closed = 0

    def assign(self, source: str, role: str) -> None:
        self.assigned.append((source, role))
        self.events.append(f"assign {role}")

    def close(self, timeout_s: float = 20.0) -> bool:
        self.closed += 1
        self.events.append("preview closed")
        return True


def _interactive_deps(
    monkeypatch: pytest.MonkeyPatch,
    answers: list[str],
    events: list[str],
    output: list[str],
    *,
    preview: FakePreview | None = None,
    preview_error: Exception | None = None,
    assignment: CanAssignment | None = None,
) -> SetupDependencies:
    remaining = iter(answers)

    def start_preview(candidates, out):
        events.append(f"preview started with {len(candidates)}")
        if preview_error is not None:
            raise preview_error
        return preview

    class FakeIdentifier:
        def __init__(self, _deps) -> None:
            pass

        def identify(self) -> CanAssignment:
            events.append("can identified")
            return assignment or CanAssignment(
                "can_follower_l",
                "can_follower_r",
                "usb-serial:208137AD45465006",
                "usb-serial:206437AA45465006",
            )

    monkeypatch.setattr("dreamscale_yam.setup_command.CanIdentifier", FakeIdentifier)
    return SetupDependencies(
        discover_cameras=lambda: list(CANDIDATES),
        discover_can=lambda: ["can_follower_l", "can_follower_r"],
        authenticated=lambda: True,
        login=lambda: None,
        input=lambda prompt: (events.append(f"ask {prompt.split(' [')[0]}"), next(remaining))[1],
        output=output.append,
        interactive=lambda: True,
        start_preview=start_preview,
    )


def test_interactive_setup_previews_cameras_then_identifies_can_by_adapter(
    isolated_paths: Path, monkeypatch
) -> None:
    events: list[str] = []
    output: list[str] = []
    preview = FakePreview(events)
    deps = _interactive_deps(monkeypatch, ["1", "3", "2", "n"], events, output, preview=preview)

    rig = load_rig(setup(deps=deps))

    assert events == [
        "preview started with 3",
        "ask top camera",
        "assign top",
        "ask left camera",
        "assign left",
        "ask right camera",
        "assign right",
        "preview closed",
        "can identified",
        "ask Configure predictive collision geometry now?",
    ]
    assert preview.assigned == [
        ("/dev/v4l/by-id/d435-video-index0", "top"),
        ("realsense:261022270000", "left"),
        ("realsense:261022277065", "right"),
    ]
    assert "  2. Intel RealSense D405 · serial 261022277065 · realsense:261022277065" in output
    assert rig.schema_version == 3
    assert (rig.left_channel, rig.left_can_id) == (
        "can_follower_l",
        "usb-serial:208137AD45465006",
    )
    assert rig.right_can_id == "usb-serial:206437AA45465006"
    assert rig.top_camera == "/dev/v4l/by-id/d435-video-index0"


def test_preview_failure_prints_one_line_and_the_list_still_works(
    isolated_paths: Path, monkeypatch
) -> None:
    events: list[str] = []
    output: list[str] = []
    deps = _interactive_deps(
        monkeypatch,
        ["1", "2", "3", "n"],
        events,
        output,
        preview_error=OSError("[Errno 98] Address already in use"),
    )

    rig = load_rig(setup(deps=deps))

    assert (
        "Camera preview is unavailable ([Errno 98] Address already in use); choose from the "
        "list below."
    ) in output
    assert output.index(
        "Camera preview is unavailable ([Errno 98] Address already in use); choose from the "
        "list below."
    ) < output.index("Assign top camera:")
    assert rig.left_camera == "realsense:261022277065"


def test_preview_is_closed_when_the_operator_presses_ctrl_c(
    isolated_paths: Path, monkeypatch
) -> None:
    events: list[str] = []
    preview = FakePreview(events)
    deps = _interactive_deps(monkeypatch, [], events, [], preview=preview)
    deps.input = lambda _prompt: (_ for _ in ()).throw(KeyboardInterrupt)

    with pytest.raises(KeyboardInterrupt):
        setup(deps=deps)

    assert preview.closed == 1
    assert "can identified" not in events
    assert not rig_path("default").exists()


def test_scripted_setup_has_no_preview_and_still_remembers_can_identity(
    isolated_paths: Path,
) -> None:
    answers = iter(["1", "2", "3", "2", "1", "n"])
    deps = SetupDependencies(
        discover_cameras=lambda: list(CANDIDATES),
        discover_can=lambda: ["can0", "can1"],
        can_interfaces=lambda: {
            "can0": CanInterface("can0", True, usb_serial="AAA", usb_port="1-2"),
            "can1": CanInterface("can1", True, usb_port="1-3"),
            "vcan0": CanInterface("vcan0", True),
        },
        authenticated=lambda: True,
        login=lambda: None,
        input=lambda _prompt: next(answers),
        output=lambda _line: None,
        start_preview=lambda *_args: (_ for _ in ()).throw(AssertionError("preview")),
    )

    rig = load_rig(setup(deps=deps))

    assert (rig.left_channel, rig.right_channel) == ("can1", "can0")
    assert (rig.left_can_id, rig.right_can_id) == ("usb-port:1-3", "usb-serial:AAA")


def test_setup_refuses_while_a_run_holds_the_rig(rig, isolated_paths: Path) -> None:
    path = save_rig(rig, profile="default")
    deps = SetupDependencies(
        discover_cameras=lambda: (_ for _ in ()).throw(AssertionError("discovered")),
        input=lambda _prompt: (_ for _ in ()).throw(AssertionError("prompted")),
        output=lambda _line: None,
    )

    with hold_rig_locks([path], purpose="run"):
        with pytest.raises(UserFacingError, match="in use by another dreamscale-yam command"):
            setup(reconfigure=True, deps=deps)


def test_identify_can_updates_only_the_can_fields(
    rig, isolated_paths: Path, monkeypatch
) -> None:
    from dreamscale_yam.config import RigConfig

    configured = RigConfig(**{**rig.as_dict(), "step_limits": (0.3,) * 14})
    path = save_rig(configured, profile="default")
    events: list[str] = []
    output: list[str] = []
    deps = _interactive_deps(monkeypatch, [], events, output)

    assert identify_can(deps=deps) == path

    updated = load_rig(path)
    assert (updated.left_channel, updated.right_channel) == ("can_follower_l", "can_follower_r")
    assert updated.left_can_id == "usb-serial:208137AD45465006"
    assert updated.schema_version == 3
    unchanged = {
        key: value
        for key, value in updated.as_dict().items()
        if key
        not in {"left_channel", "right_channel", "left_can_id", "right_can_id", "schema_version"}
    }
    assert unchanged == {
        key: value
        for key, value in configured.as_dict().items()
        if key not in {"left_channel", "right_channel", "schema_version"}
    }
    assert events == ["can identified"]
    text = "\n".join(output)
    assert "This updates only the left and right arm CAN adapters." in text
    assert "Cameras, collision geometry and step limits were not changed." in text
    assert path.stat().st_mode & 0o777 == 0o600


def test_identify_can_needs_a_confirmed_rig(isolated_paths: Path) -> None:
    with pytest.raises(UserFacingError, match="No confirmed rig exists"):
        identify_can(deps=SetupDependencies(output=lambda _line: None))


def test_camera_preview_command_runs_until_ctrl_c_and_closes(isolated_paths: Path) -> None:
    events: list[str] = []
    output: list[str] = []
    preview = FakePreview(events)
    deps = SetupDependencies(
        discover_cameras=lambda: list(CANDIDATES),
        output=output.append,
        start_preview=lambda candidates, _out: preview,
    )

    def interrupt() -> None:
        raise KeyboardInterrupt

    assert camera_preview_command(deps=deps, wait=interrupt) == 0
    assert preview.closed == 1
    assert output[0] == "Detected cameras:"
    assert "  1. Intel RealSense D435 · serial 146322072458 · by-id/d435-video-index0" in output
    assert output[-1] == "Camera preview stopped; every camera is closed."
    assert not rig_path("default").exists()


def test_camera_preview_command_refuses_while_a_run_holds_a_rig(
    rig, isolated_paths: Path
) -> None:
    path = save_rig(rig, profile="default")
    deps = SetupDependencies(
        discover_cameras=lambda: (_ for _ in ()).throw(AssertionError("discovered")),
        output=lambda _line: None,
    )

    with hold_rig_locks([path], purpose="run"):
        with pytest.raises(UserFacingError, match="in use"):
            camera_preview_command(deps=deps, wait=lambda: None)


def test_camera_preview_command_reports_an_unavailable_preview(isolated_paths: Path) -> None:
    deps = SetupDependencies(
        discover_cameras=lambda: list(CANDIDATES),
        output=lambda _line: None,
        start_preview=lambda *_args: (_ for _ in ()).throw(OSError("no free port")),
    )

    with pytest.raises(UserFacingError, match=r"Camera preview is unavailable \(no free port\)"):
        camera_preview_command(deps=deps, wait=lambda: None)


def test_candidates_carry_model_and_serial_for_the_menu(tmp_path: Path) -> None:
    candidates = discover_camera_candidates(
        v4l_devices=[
            {
                "source": "/dev/v4l/by-id/usb-Intel_435_ASIC1-video-index0",
                "physical_id": "usb:2-1",
                "model": "8086:0b07",
                "serial": "ASIC1",
                "product": "Intel(R) RealSense(TM) Depth Camera 435",
                "usb_dir": "/sys/devices/usb2/2-1",
            },
            {
                "source": "/dev/v4l/by-path/pci-usb-1-9-video-index0",
                "physical_id": "usb:1-9",
                "model": "046d:085e",
                "serial": "",
                "product": "Logitech BRIO",
                "usb_dir": "/sys/devices/usb1/1-9",
            },
            {
                "source": "/dev/v4l/by-path/pci-usb-3-1-video-index0",
                "physical_id": "usb:3-1",
                "model": "V4L2 camera",
                "serial": "",
            },
        ],
        realsense_devices=[
            {
                "source": "realsense:146322072458",
                "physical_id": "usb:2-1",
                "model": "Intel RealSense D435",
                "serial": "146322072458",
                "asic_serial": "ASIC1",
            },
            {
                "source": "realsense:261022277065",
                "physical_id": "usb:6-1.4",
                "model": "Intel RealSense D405",
                "serial": "261022277065",
                "usb_dir": "/sys/devices/usb6/6-1.4",
            },
        ],
    )

    assert candidates == [
        CameraCandidate(
            "/dev/v4l/by-path/pci-usb-1-9-video-index0",
            "Logitech BRIO",
            "",
            "/sys/devices/usb1/1-9",
        ),
        CameraCandidate(
            "/dev/v4l/by-id/usb-Intel_435_ASIC1-video-index0",
            "Intel RealSense D435",
            "146322072458",
            "/sys/devices/usb2/2-1",
        ),
        CameraCandidate("/dev/v4l/by-path/pci-usb-3-1-video-index0"),
        CameraCandidate(
            "realsense:261022277065",
            "Intel RealSense D405",
            "261022277065",
            "/sys/devices/usb6/6-1.4",
        ),
    ]
    assert [candidate.source for candidate in candidates] == discover_cameras(
        v4l_devices=[
            {"source": c.source, "physical_id": p, "model": "x"}
            for c, p in ((candidates[0], "usb:1-9"), (candidates[2], "usb:3-1"))
        ]
        + [
            {
                "source": "/dev/v4l/by-id/usb-Intel_435_ASIC1-video-index0",
                "physical_id": "usb:2-1",
                "model": "8086:0b07",
                "serial": "ASIC1",
            }
        ],
        realsense_devices=[
            {
                "source": "realsense:146322072458",
                "physical_id": "usb:2-1",
                "model": "Intel RealSense D435",
                "serial": "146322072458",
                "asic_serial": "ASIC1",
            },
            {
                "source": "realsense:261022277065",
                "physical_id": "usb:6-1.4",
                "model": "Intel RealSense D405",
            },
        ],
    )


def test_numbered_prompt_rejects_out_of_range_and_reused_answers() -> None:
    from dreamscale_yam.prompts import select

    output: list[str] = []
    answers = iter(["0", "-1", "x", "9", "1", "2"])
    used = {"b"}

    assert (
        select(
            "left arm CAN",
            ["b", "a"],
            used,
            input_fn=lambda _prompt: next(answers),
            output=output.append,
            labels={"a": "a · USB serial A"},
        )
        == "a"
    )
    assert output.count("Enter one listed number.") == 4
    assert "That device is already assigned; choose another." in output
    assert "  1. b (already assigned)" in output
    assert "  2. a · USB serial A" in output
    with pytest.raises(UserFacingError, match="no unused device"):
        select("right arm CAN", ["a"], {"a"}, input_fn=input, output=output.append)


def test_menu_names_come_from_usb_product_or_card_name(tmp_path: Path, monkeypatch) -> None:
    from dreamscale_yam import setup_command

    usb = tmp_path / "devices" / "usb2" / "2-1"
    (usb / "2-1:1.0" / "video4linux" / "video4").mkdir(parents=True)
    (usb / "idVendor").write_text("8086\n")
    (usb / "product").write_text("Intel(R) RealSense(TM) Depth Camera 435 \n")
    port = str(usb / "2-1:1.0" / "video4linux" / "video4")

    assert setup_command._usb_product(str(usb), "/dev/video4") == (
        "Intel(R) RealSense(TM) Depth Camera 435"
    )
    assert setup_command._usb_dir_from_port(port) == ""  # only real /sys paths are trusted
    monkeypatch.setattr(
        setup_command, "Path", lambda value: Path(str(value).replace("/sys", str(tmp_path), 1))
    )
    assert setup_command._usb_dir_from_port("/sys/devices/usb2/2-1/2-1:1.0/video4linux/video4")
    assert setup_command._display_model(
        [{"source": "/dev/v4l/by-id/x", "product": "Intel(R) RealSense(TM) Depth Camera 405"}]
    ) == "Intel RealSense D405"
    assert setup_command._display_model(
        [{"source": "/dev/v4l/by-id/x", "model": "046d:085e"}]
    ) == "USB camera 046d:085e"
