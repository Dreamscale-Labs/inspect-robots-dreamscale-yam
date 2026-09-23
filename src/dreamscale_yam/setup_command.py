"""Idempotent interactive setup for facts software cannot infer safely."""

from __future__ import annotations

import re
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from dreamscale.config import load_config
from dreamscale.quickstart import run_login

from dreamscale_yam.camera_preview import CameraCandidate, CameraPreview, announce
from dreamscale_yam.can_identity import (
    CanAssignment,
    CanFlowDependencies,
    CanIdentifier,
    CanInterface,
    read_can_interfaces,
    select_can_from_list,
    stdin_interactive,
)
from dreamscale_yam.config import (
    RigConfig,
    load_rig,
    migrate_generated_rig,
    resolve_rig_path,
    rig_path,
    rig_profiles,
    save_rig,
)
from dreamscale_yam.errors import UserFacingError
from dreamscale_yam.prompts import select as _select
from dreamscale_yam.prompts import yes_no as _yes_no
from dreamscale_yam.rig_lock import all_rig_paths, hold_rig_locks

_USB_PORT = re.compile(r"(?:^|/)(\d+-\d+(?:\.\d+)*)(?=[:/]|$)")


def _physical_id(value: str | None, fallback: str) -> str:
    if value:
        matches = _USB_PORT.findall(value)
        if matches:
            return f"usb:{matches[-1]}"
    return fallback


def _discover_v4l_devices() -> list[dict[str, str]]:
    """Use Inspect Robots' color-capability probe and stable-name trust ladder."""
    from inspect_robots._setup import (  # pyright: ignore[reportPrivateUsage]
        V4L_BY_ID,
        V4L_BY_PATH,
        _ambiguous_identities,
        _camera_inventory,
        _preferred_name,
    )

    inventory = _camera_inventory(V4L_BY_ID, V4L_BY_PATH, Path("/sys/class/video4linux"))
    ambiguous = _ambiguous_identities(inventory)
    records: list[dict[str, str]] = []
    for record in inventory:
        source = _preferred_name([record], ambiguous, prefer_by_id=True)
        if source.startswith("/dev/video"):
            # A raw kernel index is not stable enough for a physical rig config.
            continue
        serial_key = f"serial:{record.serial}" if record.serial else ""
        records.append(
            {
                "source": source,
                "physical_id": _physical_id(
                    record.camera,
                    serial_key or f"node:{record.node}",
                ),
                "model": record.model or "V4L2 camera",
                "serial": record.serial or "",
                "product": _usb_product(record.camera, record.node),
                "usb_dir": record.camera or "",
            }
        )
    return records


def _read_sysfs(path: Path) -> str:
    try:
        return " ".join(path.read_text(encoding="utf-8").split())
    except (OSError, UnicodeDecodeError):
        return ""


def _usb_product(usb_dir: str | None, node: str) -> str:
    """Return the USB product string, else the V4L2 card name, for a menu label."""
    if usb_dir:
        product = _read_sysfs(Path(usb_dir) / "product")
        if product:
            return product
    return _read_sysfs(Path("/sys/class/video4linux") / Path(node).name / "name")


def _usb_dir_from_port(port: str) -> str:
    """Return the sysfs USB device directory for a librealsense physical port path."""
    if not port.startswith("/sys/"):
        return ""
    path = Path(port)
    for ancestor in (path, *path.parents):
        if (ancestor / "idVendor").is_file():
            return str(ancestor)
    return ""


def _rs_info(device: Any, rs: Any, name: str) -> str:
    info = getattr(rs.camera_info, name, None)
    if info is None:
        return ""
    try:
        if hasattr(device, "supports") and not device.supports(info):
            return ""
        return str(device.get_info(info)).strip()
    except Exception:
        return ""


def _discover_realsense_devices() -> list[dict[str, str]]:
    try:
        import pyrealsense2 as rs
    except ImportError:
        return []
    records: list[dict[str, str]] = []
    for device in rs.context().query_devices():
        serial = _rs_info(device, rs, "serial_number")
        asic_serial = _rs_info(device, rs, "asic_serial_number")
        selected_serial = serial or asic_serial
        if not selected_serial:
            continue
        port = _rs_info(device, rs, "physical_port")
        model = _rs_info(device, rs, "name") or "Intel RealSense"
        records.append(
            {
                "source": f"realsense:{selected_serial}",
                "physical_id": _physical_id(port, f"serial:{selected_serial}"),
                "model": model,
                "serial": selected_serial,
                "asic_serial": asic_serial,
                "usb_dir": _usb_dir_from_port(port),
            }
        )
    return records


def _source_key(record: Mapping[str, str]) -> str:
    return record.get("source", "")


def _prefer_camera_sources(
    v4l_devices: Sequence[Mapping[str, str]],
    realsense_devices: Sequence[Mapping[str, str]],
) -> list[str]:
    """Select one stable backend per physical camera."""
    return [source for source, _records in _camera_groups(v4l_devices, realsense_devices)]


def _camera_groups(
    v4l_devices: Sequence[Mapping[str, str]],
    realsense_devices: Sequence[Mapping[str, str]],
) -> list[tuple[str, list[Mapping[str, str]]]]:
    """Select one stable backend per physical camera, with every record seen for it.

    Robocurve's documented primary layout uses a D435 through V4L2 and D405
    wrist cameras through isolated RealSense processes. Physical USB identity
    joins the two Linux representations before that backend preference is
    applied, so one camera cannot be offered twice when the kernel exposes it
    through both APIs.
    """
    records = [*v4l_devices, *realsense_devices]
    parent: dict[str, str] = {}

    def find(value: str) -> str:
        parent.setdefault(value, value)
        if parent[value] != value:
            parent[value] = find(parent[value])
        return parent[value]

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    serial_records: dict[str, list[tuple[str, bool]]] = defaultdict(list)
    for record in records:
        physical = record.get("physical_id", "")
        if not physical:
            continue
        find(physical)
        is_realsense = _source_key(record).startswith("realsense:")
        for key in ("serial", "asic_serial"):
            serial = record.get(key, "")
            if not serial:
                continue
            serial_records[serial].append((physical, is_realsense))
    for aliases in serial_records.values():
        v4l_physical = {physical for physical, is_rs in aliases if not is_rs}
        rs_physical = {physical for physical, is_rs in aliases if is_rs}
        # Join only a unique cross-backend match. A duplicated serial within
        # one backend is ambiguous and must never collapse physical cameras.
        if len(v4l_physical) == 1 and len(rs_physical) == 1:
            union(next(iter(v4l_physical)), next(iter(rs_physical)))

    grouped: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for record in records:
        source = _source_key(record)
        physical = record.get("physical_id", "")
        if source and physical:
            grouped[find(physical)].append(record)

    selected: list[tuple[str, str, list[Mapping[str, str]]]] = []
    for physical, records in grouped.items():
        v4l = [record for record in records if not _source_key(record).startswith("realsense:")]
        realsense = [
            record for record in records if _source_key(record).startswith("realsense:")
        ]
        model = " ".join(record.get("model", "") for record in records).lower()
        if "d405" in model and realsense:
            chosen = realsense[0]
        elif "d435" in model and v4l:
            chosen = v4l[0]
        elif realsense:
            chosen = realsense[0]
        elif v4l:
            chosen = v4l[0]
        else:  # pragma: no cover - records are filtered before grouping
            continue
        selected.append((physical, _source_key(chosen), records))
    selected.sort(key=lambda item: (item[0], item[1]))
    return [(source, records) for _physical, source, records in selected]


_TRADEMARKS = re.compile(r"\s*\((?:R|TM)\)")
_REALSENSE_PRODUCT = re.compile(r"Intel RealSense Depth Camera (\d{3}\w*)\Z")


def _display_model(records: Sequence[Mapping[str, str]]) -> str:
    """Return the most specific camera model name any backend reported."""
    for record in records:
        if _source_key(record).startswith("realsense:") and record.get("model"):
            return record["model"]
    for record in records:
        product = " ".join(_TRADEMARKS.sub("", record.get("product", "")).split())
        if product:
            realsense = _REALSENSE_PRODUCT.fullmatch(product)
            return f"Intel RealSense D{realsense.group(1)}" if realsense else product
    for record in records:
        model = record.get("model", "")
        if model and model != "V4L2 camera":
            return f"USB camera {model}"
    return ""


def _candidate(source: str, records: Sequence[Mapping[str, str]]) -> CameraCandidate:
    realsense = [record for record in records if _source_key(record).startswith("realsense:")]
    serial = next(
        (record["serial"] for record in (*realsense, *records) if record.get("serial")), ""
    )
    usb_dir = next((record["usb_dir"] for record in records if record.get("usb_dir")), "")
    return CameraCandidate(
        source=source,
        model=_display_model(records),
        serial=serial,
        usb_dir=usb_dir,
    )


def discover_camera_candidates(
    *,
    v4l_devices: Sequence[Mapping[str, str]] | None = None,
    realsense_devices: Sequence[Mapping[str, str]] | None = None,
) -> list[CameraCandidate]:
    """Return one stable camera source per physical camera, with its model and serial."""
    v4l = list(v4l_devices) if v4l_devices is not None else _discover_v4l_devices()
    realsense = (
        list(realsense_devices)
        if realsense_devices is not None
        else _discover_realsense_devices()
    )
    return [_candidate(source, records) for source, records in _camera_groups(v4l, realsense)]


def discover_cameras(
    *,
    v4l_devices: Sequence[Mapping[str, str]] | None = None,
    realsense_devices: Sequence[Mapping[str, str]] | None = None,
) -> list[str]:
    """Return one stable, color-capable source per physical camera."""
    v4l = list(v4l_devices) if v4l_devices is not None else _discover_v4l_devices()
    realsense = (
        list(realsense_devices)
        if realsense_devices is not None
        else _discover_realsense_devices()
    )
    return _prefer_camera_sources(v4l, realsense)


def discover_can_interfaces() -> list[str]:
    """Return SocketCAN interfaces reported by Linux sysfs."""
    return list(read_can_interfaces())


def _authenticated() -> bool:
    return bool(load_config().api_key)


def _login() -> None:
    run_login(print_next_steps=False)


class PreviewHandle(Protocol):
    """What the interview needs from a running camera preview."""

    def assign(self, source: str, role: str) -> None: ...

    def close(self, timeout_s: float = ...) -> bool: ...


def _start_preview(
    candidates: Sequence[CameraCandidate],
    output: Callable[[str], None],
) -> PreviewHandle:
    preview = CameraPreview(candidates)
    preview.start()
    try:
        announce(preview, output)
    except BaseException:
        preview.close()
        raise
    return preview


@dataclass
class SetupDependencies:
    discover_cameras: Callable[[], Sequence[str | CameraCandidate]] = (
        discover_camera_candidates
    )
    discover_can: Callable[[], list[str]] = discover_can_interfaces
    authenticated: Callable[[], bool] = _authenticated
    login: Callable[[], None] = _login
    input: Callable[[str], str] = input
    output: Callable[[str], None] = print
    #: Whether a person is at this terminal. When unset, setup treats the
    #: builtin ``input`` on a TTY as interactive and any injected ``input`` as a
    #: script, so scripted runs keep the plain numbered questions.
    interactive: Callable[[], bool] | None = None
    can_interfaces: Callable[[], Mapping[str, CanInterface]] = read_can_interfaces
    start_preview: Callable[
        [Sequence[CameraCandidate], Callable[[str], None]], PreviewHandle
    ] = _start_preview
    can_flow: CanFlowDependencies | None = None


def _is_interactive(deps: SetupDependencies) -> bool:
    if deps.interactive is not None:
        return deps.interactive()
    return deps.input is input and stdin_interactive()


def _as_candidate(item: str | CameraCandidate) -> CameraCandidate:
    return item if isinstance(item, CameraCandidate) else CameraCandidate(source=item)


def _float(prompt: str, input_fn: Callable[[str], str], output: Callable[[str], None]) -> float:
    while True:
        try:
            return float(input_fn(prompt).strip())
        except ValueError:
            output("Enter one number in metres or radians as labelled.")


def _xyz(
    prompt: str,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> tuple[float, float, float]:
    while True:
        parts = input_fn(prompt).replace(",", " ").split()
        try:
            values = tuple(float(part) for part in parts)
        except ValueError:
            values = ()
        if len(values) == 3:
            return values[0], values[1], values[2]
        output("Enter exactly three numbers: x y z (metres).")


def _setup_path(
    rig_name: str | None,
    *,
    input_fn: Callable[[str], str],
    output: Callable[[str], None],
) -> Path:
    if rig_name is not None:
        return rig_path(rig_name)
    configured = rig_profiles()
    if not configured:
        return rig_path("default")
    if len(configured) == 1:
        return next(iter(configured.values()))
    if configured:
        output(f"Configured rig profiles: {', '.join(sorted(configured))}")
    while True:
        answer = input_fn("Rig profile name (for example jay-rig-1): ").strip()
        if answer in configured:
            return configured[answer]
        try:
            return rig_path(answer)
        except ValueError as exc:
            output(str(exc))


def _close_preview(preview: PreviewHandle, output: Callable[[str], None]) -> None:
    """Close the preview; a second Ctrl-C while cameras close does not skip their release."""
    interrupted = False
    while True:
        try:
            closed = preview.close()
        except KeyboardInterrupt:
            if interrupted:
                raise
            interrupted = True
            output("Closing the cameras; one moment.")
            continue
        except Exception as exc:
            output(f"The camera preview did not close cleanly ({exc}).")
            return
        break
    if not closed:
        output("A preview camera is still closing; it will be released when setup exits.")


def _assign_cameras(
    candidates: Sequence[CameraCandidate],
    deps: SetupDependencies,
    *,
    interactive: bool,
) -> tuple[str, str, str]:
    """Ask for the top, left and right cameras, with a live preview when possible."""
    sources = [candidate.source for candidate in candidates]
    labels = {candidate.source: candidate.menu_label() for candidate in candidates}
    preview: PreviewHandle | None = None
    if interactive:
        try:
            preview = deps.start_preview(candidates, deps.output)
        except Exception as exc:
            reason = " ".join(str(exc).split()) or type(exc).__name__
            deps.output(
                f"Camera preview is unavailable ({reason}); choose from the list below."
            )
    try:
        used: set[str] = set()
        chosen: list[str] = []
        for role in ("top", "left", "right"):
            source = _select(
                f"{role} camera",
                sources,
                used,
                input_fn=deps.input,
                output=deps.output,
                labels=labels,
            )
            chosen.append(source)
            if preview is not None:
                try:
                    preview.assign(source, role)
                except Exception:
                    pass
    finally:
        if preview is not None:
            _close_preview(preview, deps.output)
    return chosen[0], chosen[1], chosen[2]


def _assign_can(
    channels: Sequence[str],
    deps: SetupDependencies,
    *,
    interactive: bool,
) -> CanAssignment:
    """Identify each arm's adapter by unplugging it, or pick from the list."""
    if interactive:
        flow = deps.can_flow or CanFlowDependencies(
            interfaces=deps.can_interfaces,
            input=deps.input,
            output=deps.output,
        )
        return CanIdentifier(flow).identify()
    try:
        interfaces = deps.can_interfaces()
    except Exception:
        interfaces = {}
    known = {name: interface for name, interface in interfaces.items() if name in channels}
    missing = {name: CanInterface(name=name, up=False) for name in channels if name not in known}
    return select_can_from_list(
        {**known, **missing},
        input_fn=deps.input,
        output=deps.output,
        names=list(channels),
    )


def setup(
    *,
    rig_name: str | None = None,
    reconfigure: bool = False,
    deps: SetupDependencies | None = None,
) -> Path:
    deps = deps or SetupDependencies()
    path = _setup_path(rig_name, input_fn=deps.input, output=deps.output)
    if path.exists() and not reconfigure:
        if migrate_generated_rig(path):
            deps.output(
                "Setup updated the generated I2RT joint bounds; camera and CAN assignments "
                "were kept."
            )
        load_rig(path)
        deps.output(f"Rig already confirmed: {path}")
        if not deps.authenticated():
            deps.output("Dreamscale credentials are absent; opening login.")
            deps.login()
        return path

    with hold_rig_locks(all_rig_paths(path), purpose="setup"):
        rig = _interview(deps)
        saved = save_rig(rig, path=path, replace=reconfigure)
    deps.output(f"Confirmed rig written to {saved}")
    if not deps.authenticated():
        deps.output("Dreamscale credentials are absent; opening login.")
        deps.login()
    return saved


def _interview(deps: SetupDependencies) -> RigConfig:
    candidates = [_as_candidate(item) for item in deps.discover_cameras()]
    if len(candidates) < 3:
        raise UserFacingError(
            f"Setup found {len(candidates)} usable color cameras, but the YAM rig needs three",
            "Connect and power the top camera and both wrist cameras, close any program using "
            "them, check `v4l2-ctl --list-devices`, then rerun ./setup.sh",
        )
    channels = deps.discover_can()
    if len(channels) < 2:
        raise UserFacingError(
            f"Setup found {len(channels)} CAN interfaces, but the two YAM arms need two",
            "Connect both CAN adapters, bring both interfaces UP, check "
            "`ip -details link show type can`, then rerun ./setup.sh",
        )

    interactive = _is_interactive(deps)
    top, left_camera, right_camera = _assign_cameras(candidates, deps, interactive=interactive)
    can = _assign_can(channels, deps, interactive=interactive)

    deps.output("")
    deps.output("Optional: predictive collision checking can reject a target before it is sent.")
    deps.output("If you enable it, you will enter these five measurements in one rig frame:")
    deps.output("  - left and right arm-base x y z positions (metres)")
    deps.output("  - left and right arm-base yaw angles (radians)")
    deps.output("  - table-top z height (metres)")
    configure_collision = _yes_no(
        "Configure predictive collision geometry now?",
        default=False,
        input_fn=deps.input,
        output=deps.output,
    )
    geometry: dict[str, Any]
    if configure_collision:
        geometry = {
            "collision_guardrail": True,
            "collision_table": True,
            "collision_left_base_pos": _xyz(
                "Left arm base x y z (m): ", deps.input, deps.output
            ),
            "collision_right_base_pos": _xyz(
                "Right arm base x y z (m): ", deps.input, deps.output
            ),
            "collision_left_base_yaw": _float(
                "Left arm base yaw (rad): ", deps.input, deps.output
            ),
            "collision_right_base_yaw": _float(
                "Right arm base yaw (rad): ", deps.input, deps.output
            ),
            "collision_table_height": _float(
                "Table top height z (m): ", deps.input, deps.output
            ),
        }
    else:
        geometry = {
            "collision_guardrail": False,
            "collision_table": False,
        }
        deps.output(
            "Predictive collision checking is off. Joint bounds and strict per-action jump "
            "limits remain on."
        )

    rig = RigConfig(
        top_camera=top,
        left_camera=left_camera,
        right_camera=right_camera,
        left_channel=can.left_channel,
        right_channel=can.right_channel,
        **geometry,
    )
    return rig.with_can_assignment(
        left_channel=can.left_channel,
        right_channel=can.right_channel,
        left_can_id=can.left_can_id,
        right_can_id=can.right_can_id,
    )


def identify_can(
    *,
    rig_name: str | None = None,
    deps: SetupDependencies | None = None,
) -> Path:
    """Re-identify only the CAN adapters of an existing confirmed rig."""
    deps = deps or SetupDependencies()
    path = resolve_rig_path(rig_name)
    if not path.exists():
        raise UserFacingError(
            f"No confirmed rig exists at {path}",
            "Run ./setup.sh first; identify-can updates the CAN adapters of a confirmed rig",
        )
    with hold_rig_locks(all_rig_paths(path), purpose="identify-can"):
        if migrate_generated_rig(path):
            deps.output(
                "Setup updated the generated I2RT joint bounds; camera and CAN assignments "
                "were kept."
            )
        rig = load_rig(path)
        deps.output(f"Rig: {path}")
        deps.output(
            "This updates only the left and right arm CAN adapters. Cameras, collision "
            "geometry and step limits are kept."
        )
        deps.output(
            f"Currently saved: left arm = {rig.left_channel}, right arm = {rig.right_channel}."
        )
        channels = deps.discover_can()
        if len(channels) < 2:
            raise UserFacingError(
                f"Found {len(channels)} CAN interfaces, but the two YAM arms need two",
                "Connect both CAN adapters, bring both interfaces UP, check "
                "`ip -details link show type can`, then rerun ./dreamscale-yam identify-can",
            )
        can = _assign_can(channels, deps, interactive=_is_interactive(deps))
        updated = rig.with_can_assignment(
            left_channel=can.left_channel,
            right_channel=can.right_channel,
            left_can_id=can.left_can_id,
            right_can_id=can.right_can_id,
        )
        save_rig(updated, path=path, replace=True)
    deps.output(f"Updated the CAN adapters in {path}: {can.summary()}.")
    deps.output("Cameras, collision geometry and step limits were not changed.")
    deps.output("Next: run ./dreamscale-yam doctor")
    return path


def camera_preview_command(
    *,
    deps: SetupDependencies | None = None,
    wait: Callable[[], None] | None = None,
) -> int:
    """Show the numbered camera preview until Ctrl-C, without changing any config."""
    deps = deps or SetupDependencies()
    with hold_rig_locks(all_rig_paths(), purpose="camera preview"):
        candidates = [_as_candidate(item) for item in deps.discover_cameras()]
        if not candidates:
            raise UserFacingError(
                "No stable color cameras were found",
                "Connect and power the cameras, close any program using them, check "
                "`v4l2-ctl --list-devices`, then repeat ./dreamscale-yam cameras",
            )
        deps.output("Detected cameras:")
        for number, candidate in enumerate(candidates, 1):
            deps.output(f"  {number}. {candidate.menu_label()}")
        try:
            preview = deps.start_preview(candidates, deps.output)
        except Exception as exc:
            reason = " ".join(str(exc).split()) or type(exc).__name__
            raise UserFacingError(
                f"Camera preview is unavailable ({reason})",
                "Use the numbered list above, or send the output of ./dreamscale-yam doctor "
                "--json to Dreamscale",
            ) from exc
        deps.output("Press Ctrl-C to stop the preview. No configuration is changed.")
        try:
            (wait or _wait_forever)()
        except KeyboardInterrupt:
            deps.output("")
        finally:
            _close_preview(preview, deps.output)
    deps.output("Camera preview stopped; every camera is closed.")
    return 0


def _wait_forever() -> None:
    while True:
        time.sleep(3600)
