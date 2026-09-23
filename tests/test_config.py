from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from inspect_robots.errors import SafetyAbort
from inspect_robots.types import Action
from inspect_robots_yam import YamConfig, YAMEmbodiment

import dreamscale_yam.config as config
from dreamscale_yam.config import (
    I2RT_JOINT_HIGH,
    I2RT_JOINT_LOW,
    RigConfig,
    load_rig,
    save_rig,
)


def test_rig_round_trip_is_fixed_attended_strict_30_hz(
    rig: RigConfig, isolated_paths: Path
) -> None:
    path = save_rig(rig)

    loaded = load_rig(path)

    assert loaded == rig
    assert loaded.control_hz == 30
    assert loaded.auto_start is False
    assert loaded.unattended is False
    assert loaded.keep_warm == 0
    assert loaded.strict_policy_actions is True
    assert "strict_gripper_endpoint_projection" not in loaded.yam_kwargs()
    assert "strict_arm_endpoint_projection" not in loaded.yam_kwargs()
    assert loaded.joints_are_delta is False
    assert loaded.joint_low == I2RT_JOINT_LOW
    assert loaded.joint_high == I2RT_JOINT_HIGH


def test_step_limits_accept_any_finite_positive_fourteen_value_vector(
    rig: RigConfig, isolated_paths: Path
) -> None:
    limits = tuple(0.5 + index / 100 for index in range(14))
    configured = RigConfig(**{**rig.as_dict(), "step_limits": limits})

    path = save_rig(configured)

    assert load_rig(path).step_limits == limits
    assert configured.yam_kwargs()["step_limits"] == limits


@pytest.mark.parametrize(
    "limits",
    [
        (0.2,) * 13,
        (0.2,) * 13 + (0.0,),
        (0.2,) * 13 + (-0.1,),
        (0.2,) * 13 + (float("nan"),),
        (0.2,) * 13 + (float("inf"),),
        ("0.2",) * 14,
    ],
)
def test_step_limits_reject_malformed_vectors(rig: RigConfig, limits: tuple[object, ...]) -> None:
    with pytest.raises(ValueError, match="14 finite positive"):
        RigConfig(**{**rig.as_dict(), "step_limits": limits})


def test_generated_bounds_accept_boundary_and_strict_backstop_rejects_overshoot(
    rig: RigConfig,
) -> None:
    raw_low = np.asarray((-2.61799, 0.0, 0.0, -1.5708, -1.5708, -2.0944))
    raw_high = np.asarray((3.05433, 3.65, 3.66519, 1.5708, 1.5708, 2.0944))
    expected_arm_low = np.concatenate((raw_low - 0.15, (0.0,)))
    expected_arm_high = np.concatenate((raw_high + 0.15, (1.0,)))
    assert rig.schema_version == 2
    np.testing.assert_array_equal(rig.joint_low, np.tile(expected_arm_low, 2))
    np.testing.assert_array_equal(rig.joint_high, np.tile(expected_arm_high, 2))

    embodiment = YAMEmbodiment(YamConfig(**rig.yam_kwargs()))
    for target in (np.tile(expected_arm_low, 2), np.tile(expected_arm_high, 2)):
        validated = embodiment.validate_policy_action(Action(target), reference=target)
        driver_result = np.clip(
            validated,
            np.tile(expected_arm_low, 2),
            np.tile(expected_arm_high, 2),
        )
        np.testing.assert_array_equal(validated, target)
        np.testing.assert_array_equal(driver_result, target)

    outside = np.tile(expected_arm_low, 2)
    outside[0] = np.nextafter(outside[0], -np.inf)
    with pytest.raises(SafetyAbort, match=r"bounds.*left_j0"):
        embodiment.validate_policy_action(Action(outside), reference=np.tile(expected_arm_low, 2))

    gross = np.tile(expected_arm_low, 2)
    gross[0] = 100.0
    with pytest.raises(SafetyAbort, match=r"bounds.*left_j0"):
        embodiment.validate_policy_action(Action(gross), reference=np.tile(expected_arm_low, 2))


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"control_hz": 15}, "exactly 30 Hz"),
        ({"auto_start": True}, "auto_start=false"),
        ({"unattended": True}, "unattended=false"),
        ({"keep_warm": 10}, "keep_warm=0"),
        ({"strict_policy_actions": False}, "strict abort"),
        ({"collision_table_height": None}, "all five collision measurements"),
        (
            {"collision_guardrail": False, "collision_table": False},
            "remove the collision measurements",
        ),
        ({"collision_table": False}, "table collision checking"),
        ({"left_camera": "/dev/video4"}, "stable camera"),
        ({"left_camera": "realsense:"}, "serial"),
    ],
)
def test_rig_refuses_unsafe_product_boundary(
    rig: RigConfig, changes: dict[str, object], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        RigConfig(**{**rig.as_dict(), **changes})


def test_collision_geometry_can_be_deliberately_omitted(
    rig: RigConfig, isolated_paths: Path
) -> None:
    without_geometry = RigConfig(
        **{
            **rig.as_dict(),
            "collision_guardrail": False,
            "collision_table": False,
            "collision_left_base_pos": None,
            "collision_right_base_pos": None,
            "collision_left_base_yaw": None,
            "collision_right_base_yaw": None,
            "collision_table_height": None,
        }
    )

    path = save_rig(without_geometry)
    saved = path.read_text(encoding="utf-8")

    assert load_rig(path) == without_geometry
    assert "collision_left_base_pos" not in saved
    assert without_geometry.yam_kwargs()["collision_guardrail"] is False
    assert without_geometry.yam_kwargs()["collision_table"] is False
    assert "collision_table_height" not in without_geometry.yam_kwargs()


def test_save_rig_does_not_replace_confirmed_values_without_force(
    rig: RigConfig, isolated_paths: Path
) -> None:
    path = save_rig(rig)
    changed = RigConfig(**{**rig.as_dict(), "left_channel": "can9"})

    with pytest.raises(FileExistsError, match="--reconfigure"):
        save_rig(changed)

    assert load_rig(path) == rig
    save_rig(changed, replace=True)
    assert load_rig(path).left_channel == "can9"


def test_mixed_stable_camera_sources_map_to_the_yam_backends(rig: RigConfig) -> None:
    mixed = RigConfig(
        **{
            **rig.as_dict(),
            "top_camera": "/dev/v4l/by-id/d435-video-index0",
            "left_camera": "realsense:LEFT-D405",
            "right_camera": "/dev/v4l/by-path/pci-usb-right-video-index4",
        }
    )

    kwargs = mixed.yam_kwargs()

    assert kwargs["top_cam_device"] == "/dev/v4l/by-id/d435-video-index0"
    assert kwargs["left_depth_serial"] == "LEFT-D405"
    assert kwargs["right_cam_device"] == "/dev/v4l/by-path/pci-usb-right-video-index4"
    assert "left_cam_device" not in kwargs
    assert "right_depth_serial" not in kwargs
    assert kwargs["realsense_capture"] == "process"
    assert kwargs["depth_fps"] == 30


def test_named_profiles_are_isolated_and_ambiguous_implicit_selection_fails(
    rig: RigConfig, isolated_paths: Path
) -> None:
    left_path = save_rig(rig, profile="jay-left")
    right_rig = RigConfig(**{**rig.as_dict(), "left_channel": "can2", "right_channel": "can3"})
    right_path = save_rig(right_rig, profile="jay-right")

    assert left_path == config.rig_path("jay-left")
    assert right_path == config.rig_path("jay-right")
    assert load_rig(profile="jay-left") == rig
    assert load_rig(profile="jay-right") == right_rig
    assert config.resolve_rig_path("jay-left") == left_path
    with pytest.raises(ValueError, match="multiple rig profiles"):
        config.resolve_rig_path()


@pytest.mark.parametrize("profile", ["", "../escape", "a/b", "with space"])
def test_named_profile_cannot_escape_config_home(profile: str, isolated_paths: Path) -> None:
    with pytest.raises(ValueError, match="rig profile"):
        config.rig_path(profile)


def test_config_home_is_the_dreamscale_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catch the runtime reading a pre-rename directory; setup.sh moves it once."""
    monkeypatch.delenv("DREAMSCALE_YAM_CONFIG_HOME", raising=False)
    monkeypatch.setattr(config.Path, "home", classmethod(lambda _cls: tmp_path))
    (tmp_path / ".config" / "dropbear-yam").mkdir(parents=True)

    assert config.config_home() == tmp_path / ".config" / "dreamscale-yam"


GOLDEN_V0117_DIGEST = "0b6aebcadb50a3f78bb28def4c32b5c28ad89f220a229ecf4b02b3ab253b4fcd"


def test_rig_without_can_identity_is_unchanged_in_file_dict_and_digest(
    isolated_paths: Path, tmp_path: Path
) -> None:
    """A rig confirmed before v0.1.21 loads, saves and hashes exactly as before."""
    from dreamscale_yam.runner import configuration_digest

    fixture = Path(__file__).parent / "fixtures" / "v0.1.17-rig.toml"
    loaded = load_rig(fixture)
    lock = tmp_path / "composition.lock.toml"
    lock.write_text("commit='golden'\n")

    assert loaded.schema_version == 2
    assert (loaded.left_can_id, loaded.right_can_id) == (None, None)
    assert "left_can_id" not in loaded.as_dict()
    assert loaded.digest_dict() == loaded.as_dict()
    # Computed with the v0.1.20 code for this exact fixture and lock text.
    assert configuration_digest(loaded, lock) == GOLDEN_V0117_DIGEST
    copy = tmp_path / "copy.toml"
    copy.write_bytes(fixture.read_bytes())
    save_rig(loaded, copy)
    assert copy.read_bytes() == fixture.read_bytes()
    assert config.migrate_generated_rig(copy) is False
    written = save_rig(loaded, tmp_path / "rewritten.toml")
    assert load_rig(written) == loaded
    assert "can_id" not in written.read_text(encoding="utf-8")


def test_can_identity_round_trips_as_schema_3_with_private_permissions(
    rig: RigConfig, isolated_paths: Path
) -> None:
    identified = rig.with_can_assignment(
        left_channel="can_follower_l",
        right_channel="can_follower_r",
        left_can_id="usb-serial:208137AD45465006",
        right_can_id="usb-port:1-5.1",
    )

    path = save_rig(identified)
    text = path.read_text(encoding="utf-8")

    assert identified.schema_version == 3
    assert load_rig(path) == identified
    assert 'left_can_id = "usb-serial:208137AD45465006"' in text
    assert "schema_version = 3" in text
    assert path.stat().st_mode & 0o777 == 0o600
    assert config.migrate_generated_rig(path) is False
    assert identified.yam_kwargs()["left_channel"] == "can_follower_l"
    assert identified.collision_left_base_pos == rig.collision_left_base_pos
    plain = identified.with_can_assignment(
        left_channel="can0", right_channel="can1", left_can_id=None, right_can_id=None
    )
    assert plain.schema_version == 2
    assert plain == rig


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"left_can_id": "usb-serial:A"}, "schema_version 3"),
        ({"schema_version": 3}, "schema_version 3"),
        ({"schema_version": 4}, "unsupported rig schema_version"),
        ({"schema_version": 3, "left_can_id": "serial A"}, "usb-serial:<serial>"),
        ({"schema_version": 3, "left_can_id": "usb-serial:"}, "usb-serial:<serial>"),
        (
            {"schema_version": 3, "left_can_id": "usb-serial:A", "right_can_id": "usb-serial:A"},
            "two different CAN adapter",
        ),
    ],
)
def test_can_identity_is_validated(
    rig: RigConfig, changes: dict[str, object], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        RigConfig(**{**rig.as_dict(), **changes})


def test_digest_keys_on_adapter_identity_not_kernel_name(rig: RigConfig, tmp_path: Path) -> None:
    from dreamscale_yam.runner import configuration_digest

    lock = tmp_path / "composition.lock.toml"
    lock.write_text("commit='one'\n")
    identified = rig.with_can_assignment(
        left_channel="can0",
        right_channel="can1",
        left_can_id="usb-serial:LEFT",
        right_can_id="usb-serial:RIGHT",
    )
    renamed = identified.with_channels("can_follower_l", "can_follower_r")
    swapped = rig.with_can_assignment(
        left_channel="can0",
        right_channel="can1",
        left_can_id="usb-serial:RIGHT",
        right_can_id="usb-serial:LEFT",
    )

    assert configuration_digest(renamed, lock) == configuration_digest(identified, lock)
    assert configuration_digest(swapped, lock) != configuration_digest(identified, lock)
    assert configuration_digest(identified, lock) != configuration_digest(rig, lock)
    assert "left_channel" not in identified.digest_dict()
    assert identified.digest_dict()["left_can_id"] == "usb-serial:LEFT"
