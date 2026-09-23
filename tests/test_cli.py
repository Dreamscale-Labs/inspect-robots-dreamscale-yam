from __future__ import annotations

from pathlib import Path

import pytest
from dreamscale import errors as dreamscale_errors

from dreamscale_yam.cli import _doctor_command, _parser, main
from dreamscale_yam.doctor import Diagnostic, DoctorReport


def test_every_rig_command_accepts_an_explicit_named_profile() -> None:
    parser = _parser()

    assert parser.parse_args(["setup", "--rig", "jay-left"]).rig == "jay-left"
    assert parser.parse_args(["doctor", "--rig", "jay-left"]).rig == "jay-left"
    assert parser.parse_args(["run", "--rig", "jay-left", "Pack container"]).rig == "jay-left"


def test_login_is_a_customer_facing_composition_command() -> None:
    assert _parser().parse_args(["login"]).command == "login"


def test_login_uses_the_locked_sdk_without_generic_next_steps(monkeypatch, capsys) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(
        "dreamscale_yam.cli.run_login",
        lambda *, print_next_steps: calls.append(print_next_steps),
    )

    assert main(["login"]) == 0
    assert calls == [False]
    assert "./dreamscale-yam doctor" in capsys.readouterr().out


def test_setup_login_failure_preserves_sdk_diagnostic_and_yam_recovery(monkeypatch, capsys) -> None:
    failure = dreamscale_errors.catalog(
        "cli_login_start_failed",
        detail="the login service returned HTTP 503; request id req-123",
    )
    monkeypatch.setattr(
        "dreamscale_yam.cli.setup",
        lambda **_kwargs: (_ for _ in ()).throw(failure),
    )

    assert main(["setup"]) == 2
    error = capsys.readouterr().err
    assert "browser login could not start" in error
    assert "HTTP 503" in error
    assert "request id req-123" in error
    assert "YAM rig configuration is saved" in error
    assert "./dreamscale-yam login" in error


def test_run_defaults_to_two_minutes_at_30_hz() -> None:
    args = _parser().parse_args(["run", "Pack container"])

    assert args.max_steps == 3600


def test_run_keeps_compute_warm_for_five_minutes_by_default() -> None:
    args = _parser().parse_args(["run", "Pack container"])

    assert args.warm == 5


@pytest.mark.parametrize("minutes", [0, 1, 60])
def test_run_accepts_bounded_whole_warm_minutes(minutes: int) -> None:
    args = _parser().parse_args(["run", f"--warm={minutes}", "Pack container"])

    assert args.warm == minutes


@pytest.mark.parametrize("minutes", ["-1", "61", "1.5", "five"])
def test_run_rejects_unusable_warm_minutes(minutes: str) -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(["run", f"--warm={minutes}", "Pack container"])


def test_cli_passes_warm_minutes_to_the_run(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(instruction, rig, **kwargs):
        captured.update(instruction=instruction, rig=rig, **kwargs)
        return 0

    monkeypatch.setattr("dreamscale_yam.cli.load_rig", lambda **_kwargs: "rig")
    monkeypatch.setattr("dreamscale_yam.cli.run", fake_run)

    assert main(["run", "--warm=12", "Pack container"]) == 0
    assert captured["warm_minutes"] == 12


def test_cli_error_has_plain_message_and_next_step(monkeypatch, capsys) -> None:
    missing = Path("/tmp/example-rig.toml")
    monkeypatch.setattr(
        "dreamscale_yam.cli.load_rig",
        lambda **_kwargs: (_ for _ in ()).throw(
            FileNotFoundError(f"rig config missing: {missing}")
        ),
    )

    assert main(["doctor"]) == 2
    error = capsys.readouterr().err
    assert "Error: No YAM rig has been configured yet." in error
    assert "Next: Run ./setup.sh" in error


def test_doctor_prints_action_for_warning_as_well_as_failure(monkeypatch, capsys) -> None:
    report = DoctorReport(
        checks=(
            Diagnostic(
                "DBY-GEOMETRY",
                "warn",
                "Predictive collision checking is turned off.",
                "Run ./dreamscale-yam setup --reconfigure to add it.",
            ),
        )
    )
    monkeypatch.setattr("dreamscale_yam.cli.load_rig", lambda **_kwargs: object())
    monkeypatch.setattr("dreamscale_yam.cli.doctor", lambda _rig: report)

    assert _doctor_command(False, None) == 0
    output = capsys.readouterr().out
    assert "[WARN] DBY-GEOMETRY" in output
    assert "next: Run ./dreamscale-yam setup --reconfigure" in output


def test_cameras_and_identify_can_are_customer_commands() -> None:
    parser = _parser()

    assert parser.parse_args(["cameras"]).command == "cameras"
    parsed = parser.parse_args(["identify-can", "--rig", "jay-left"])
    assert (parsed.command, parsed.rig) == ("identify-can", "jay-left")
    assert parser.parse_args(["identify-can"]).rig is None


def test_cli_dispatches_cameras_and_identify_can(monkeypatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr("dreamscale_yam.cli.camera_preview_command", lambda: calls.append("c") or 0)
    monkeypatch.setattr(
        "dreamscale_yam.cli.identify_can", lambda *, rig_name: calls.append(rig_name)
    )

    assert main(["cameras"]) == 0
    assert main(["identify-can", "--rig", "jay-left"]) == 0
    assert calls == ["c", "jay-left"]


def test_run_holds_the_rig_lock_so_identify_can_refuses(rig, isolated_paths, capsys) -> None:
    from dreamscale_yam.config import save_rig
    from dreamscale_yam.setup_command import SetupDependencies, identify_can

    save_rig(rig, profile="default")
    refused: list[str] = []

    def fake_run(_instruction, _rig, **_kwargs):
        try:
            identify_can(
                deps=SetupDependencies(
                    discover_can=lambda: (_ for _ in ()).throw(AssertionError("discovered")),
                    input=lambda _prompt: (_ for _ in ()).throw(AssertionError("prompted")),
                    output=lambda _line: None,
                )
            )
        except Exception as exc:  # noqa: BLE001 - the refusal is the assertion
            refused.append(str(exc))
        return 0

    import dreamscale_yam.cli as cli

    original = cli.run
    cli.run = fake_run
    try:
        assert main(["run", "Pack container"]) == 0
    finally:
        cli.run = original

    assert len(refused) == 1
    assert "is in use by another dreamscale-yam command (PID" in refused[0]
    assert ": run)" in refused[0]


def test_identify_can_refusal_is_plain_on_the_command_line(rig, isolated_paths, capsys) -> None:
    from dreamscale_yam.config import rig_path, save_rig
    from dreamscale_yam.rig_lock import hold_rig_locks

    save_rig(rig, profile="default")
    with hold_rig_locks([rig_path("default")], purpose="run"):
        assert main(["identify-can"]) == 2

    error = capsys.readouterr().err
    assert "Error: The rig configured in" in error
    assert "Next: Wait for that command to finish" in error
