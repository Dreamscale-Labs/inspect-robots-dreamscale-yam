from __future__ import annotations

import os
import subprocess
from importlib.metadata import version
from pathlib import Path

from dreamscale_yam import __version__


def test_bootstrap_is_locked_and_has_one_explicit_sudo_confirmation() -> None:
    root = Path(__file__).resolve().parents[1]
    setup = (root / "setup.sh").read_text(encoding="utf-8")
    wrapper = (root / "dreamscale-yam").read_text(encoding="utf-8")

    assert setup.count("read -r -p") == 1
    assert "sudo apt-get install" in setup
    assert '"$uv_bin" sync' in setup and "--locked --extra hardware" in setup
    assert "dreamscale-yam setup" in setup
    assert "--locked --extra hardware" in wrapper
    assert "api_key" not in setup + wrapper
    assert "Error:" in setup and "Next:" in setup
    assert "Error:" in wrapper and "Next:" in wrapper


def test_linux_hardware_extra_installs_realsense_discovery_runtime() -> None:
    root = Path(__file__).resolve().parents[1]
    project = (root / "pyproject.toml").read_text(encoding="utf-8")

    assert 'pyrealsense2>=2.50; sys_platform == "linux"' in project


def test_readme_uses_the_customer_facing_stable_branch_without_a_rig_flag() -> None:
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert "git clone --branch stable --depth 1" in readme
    assert './dreamscale-yam doctor\n' in readme
    assert './dreamscale-yam run "Pack container"' in readme
    assert "--max-steps 3600" in readme
    assert "[Y/n]" in readme
    assert "elapsed seconds" in readme
    assert "paid shadow inference" not in readme
    assert "./dreamscale-yam login" in readme
    assert "without repeating camera or CAN selection" in readme


def test_runtime_version_matches_installed_package_metadata() -> None:
    assert __version__ == version("inspect-robots-dreamscale-yam")


def _run_rename_move(home: Path) -> str:
    setup = (Path(__file__).resolve().parents[1] / "setup.sh").read_text(encoding="utf-8")
    block = setup.split("# BEGIN one-time rename move\n", 1)[1]
    block = block.split("# END one-time rename move", 1)[0]
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", block],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_setup_moves_a_pre_rename_rig_once_and_never_overwrites(tmp_path: Path) -> None:
    """Catch an upgraded rig losing its confirmed config, or a rerun clobbering it."""
    old_config = tmp_path / ".config" / "dropbear-yam"
    (old_config / "rigs").mkdir(parents=True)
    (old_config / "rigs" / "default.toml").write_text("confirmed = true\n")
    old_state = tmp_path / ".local" / "state" / "dropbear-yam"
    (old_state / "logs").mkdir(parents=True)

    _run_rename_move(tmp_path)

    new_config = tmp_path / ".config" / "dreamscale-yam"
    assert (new_config / "rigs" / "default.toml").read_text() == "confirmed = true\n"
    assert (tmp_path / ".local" / "state" / "dreamscale-yam" / "logs").is_dir()
    assert not old_config.exists() and not old_state.exists()

    old_config.mkdir()
    (old_config / "rig.toml").write_text("stale = true\n")
    output = _run_rename_move(tmp_path)

    assert "left untouched" in output
    assert (old_config / "rig.toml").is_file()
    assert not (new_config / "rig.toml").exists()
