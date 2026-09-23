from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from dreamscale_yam import camera_preview
from dreamscale_yam.camera_preview import (
    MAX_READ_FAILURES,
    CameraCandidate,
    CameraPreview,
    announce,
    device_holders,
    encode_jpeg,
    lan_addresses,
    open_frame_source,
    open_in_browser,
    short_source,
)

D435_BY_ID = (
    "/dev/v4l/by-id/usb-Intel_R__RealSense_TM__Depth_Camera_435_Intel_R__RealSense_TM__"
    "Depth_Camera_435_150523022342-video-index0"
)


class FakeSource:
    def __init__(self, fail_after: int | None = None) -> None:
        self.reads = 0
        self.closed = False
        self.fail_after = fail_after

    def read(self) -> np.ndarray:
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            raise RuntimeError("frame read failed for preview (/dev/video4)")
        return np.full((360, 640, 3), self.reads % 255, dtype=np.uint8)

    def close(self) -> None:
        self.closed = True


def _wait(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met in time")


def _get(url: str) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - loopback only
            return response.status, response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


def test_menu_label_names_the_model_serial_and_a_shortened_source() -> None:
    wrist = CameraCandidate("realsense:261022277065", "Intel RealSense D405", "261022277065")
    top = CameraCandidate(D435_BY_ID, "Intel RealSense D435", "146322072458")

    assert wrist.menu_label() == (
        "Intel RealSense D405 · serial 261022277065 · realsense:261022277065"
    )
    label = top.menu_label()
    assert label.startswith("Intel RealSense D435 · serial 146322072458 · by-id/usb-Intel_R_")
    assert label.endswith("_150523022342-video-index0")
    assert "…" in label
    assert CameraCandidate("/dev/v4l/by-id/cam-a").menu_label() == "/dev/v4l/by-id/cam-a"
    assert short_source("/dev/v4l/by-path/short") == "by-path/short"
    assert len(short_source(D435_BY_ID)) == 60


def test_page_serves_numbered_tiles_frames_and_404s_every_other_path() -> None:
    sources: dict[str, FakeSource] = {}

    def open_source(candidate: CameraCandidate) -> FakeSource:
        if candidate.source == "realsense:BUSY":
            raise RuntimeError("xioctl(VIDIOC_S_FMT) failed, errno=16 Device or resource busy")
        sources[candidate.source] = FakeSource()
        return sources[candidate.source]

    candidates = [
        CameraCandidate(D435_BY_ID, "Intel RealSense D435", "146322072458"),
        CameraCandidate("realsense:BUSY", "Intel RealSense D405", "BUSY"),
        CameraCandidate("realsense:HELD", "Intel RealSense D405", "HELD", usb_dir="/sys/x"),
        CameraCandidate("realsense:RIGHT", "Intel RealSense D405", "RIGHT"),
    ]
    preview = CameraPreview(
        candidates,
        open_source=open_source,
        holders=lambda c: ["PID 42 recorder"] if c.source == "realsense:HELD" else [],
        addresses=lambda: [("10.0.0.243", "enp1s0")],
        host="127.0.0.1",
        fps=50.0,
        token="token-under-test",
    )
    preview.start()
    try:
        base = f"http://127.0.0.1:{preview.port}"
        _wait(lambda: preview.frame(1) is not None and preview.frame(4) is not None)
        _wait(lambda: preview.state()["cameras"][1]["status"] == "unavailable")

        status, body, content_type = _get(f"{base}/token-under-test/")
        assert status == 200
        assert content_type.startswith("text/html")
        page = body.decode()
        for number in (1, 2, 3, 4):
            assert f'data-number="{number}"' in page
        assert "Intel RealSense D435" in page
        assert "serial 146322072458" in page

        status, body, content_type = _get(f"{base}/token-under-test/frame/1.jpg")
        assert (status, content_type) == (200, "image/jpeg")
        assert body.startswith(b"\xff\xd8")

        assert _get(f"{base}/token-under-test/frame/2.jpg")[0] == 503
        state = json.loads(_get(f"{base}/token-under-test/state.json")[1])
        cameras = {camera["number"]: camera for camera in state["cameras"]}
        assert cameras[1]["status"] == "live"
        assert cameras[2]["status"] == "unavailable"
        assert "Device or resource busy" in cameras[2]["reason"]
        assert cameras[3]["reason"] == "in use by another program (PID 42 recorder)"
        assert "realsense:HELD" not in sources

        for path in (
            "/",
            "/wrong-token/",
            "/token-under-tes/",
            "/token-under-test/frame/9.jpg",
            "/token-under-test/frame/0.jpg",
            "/token-under-test/../etc/passwd",
            "/token-under-test/state.json/x",
            "/favicon.ico",
        ):
            assert _get(f"{base}{path}")[0] == 404, path
        request = urllib.request.Request(f"{base}/token-under-test", method="HEAD")
        with pytest.raises(urllib.error.HTTPError) as redirected:
            urllib.request.build_opener(_NoRedirect()).open(request, timeout=5)
        assert redirected.value.code == 308
        assert redirected.value.headers["Location"] == "/token-under-test/"

        preview.assign(D435_BY_ID, "top")
        state = json.loads(_get(f"{base}/token-under-test/state.json")[1])
        assert state["cameras"][0]["role"] == "top"
        assert [url for url, _where in preview.urls()] == [
            f"http://localhost:{preview.port}/token-under-test/",
            f"http://10.0.0.243:{preview.port}/token-under-test/",
        ]
    finally:
        assert preview.close() is True
    assert all(source.closed for source in sources.values())
    with pytest.raises(OSError):
        urllib.request.urlopen(f"{base}/token-under-test/", timeout=1)  # noqa: S310


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def test_camera_that_stops_delivering_becomes_unavailable_and_is_closed() -> None:
    source = FakeSource(fail_after=1)
    preview = CameraPreview(
        [CameraCandidate("/dev/v4l/by-id/cam-a")],
        open_source=lambda _candidate: source,
        holders=lambda _candidate: [],
        addresses=lambda: [],
        host="127.0.0.1",
        fps=500.0,
    )
    preview.start()
    try:
        _wait(lambda: preview.state()["cameras"][0]["status"] == "unavailable")
        assert source.closed
        assert source.reads == 1 + MAX_READ_FAILURES
        assert preview.state()["cameras"][0]["reason"].startswith("stopped delivering frames")
    finally:
        preview.close()


def test_close_during_a_slow_realsense_open_waits_and_closes_the_camera() -> None:
    started = threading.Event()
    release = threading.Event()
    opened: list[FakeSource] = []

    class SlowSource(FakeSource):
        def read(self) -> np.ndarray:
            started.set()
            release.wait(5)
            return super().read()

    def open_source(_candidate: CameraCandidate) -> FakeSource:
        opened.append(SlowSource())
        return opened[-1]

    preview = CameraPreview(
        [CameraCandidate("realsense:A"), CameraCandidate("realsense:B")],
        open_source=open_source,
        holders=lambda _candidate: [],
        addresses=lambda: [],
        host="127.0.0.1",
    )
    preview.start()
    started.wait(5)
    # One RealSense camera opens at a time; the second is still waiting.
    assert len(opened) == 1
    threading.Timer(0.2, release.set).start()
    assert preview.close(timeout_s=5) is True
    assert len(opened) == 1
    assert opened[0].closed


def test_announce_prints_one_instruction_and_every_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    preview = CameraPreview(
        [CameraCandidate("/dev/v4l/by-id/cam-a")],
        open_source=lambda _candidate: FakeSource(),
        holders=lambda _candidate: [],
        addresses=lambda: [("10.0.0.243", "enp1s0"), ("100.64.1.2", "tailscale0")],
        host="127.0.0.1",
        token="abc",
    )
    preview.start()
    output: list[str] = []
    try:
        announce(preview, output.append)
    finally:
        preview.close()
    port = preview.port
    assert output == [
        "Camera preview: open a link below in a browser and match each numbered picture to "
        "the numbered list.",
        f"  http://localhost:{port}/abc/   (on this computer)",
        f"  http://10.0.0.243:{port}/abc/   (from another computer, enp1s0)",
        f"  http://100.64.1.2:{port}/abc/   (from another computer, tailscale0)",
    ]


def test_default_token_is_long_and_random() -> None:
    first = CameraPreview([]).token
    second = CameraPreview([]).token
    assert first != second
    assert len(first) >= 24


def test_browser_opens_only_on_a_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(
        camera_preview.webbrowser, "get", lambda: (_ for _ in ()).throw(AssertionError("used"))
    )
    assert open_in_browser("http://localhost:1/x/") is False

    opened: list[str] = []

    class Desktop(camera_preview.webbrowser.BackgroundBrowser):
        def open(self, url: str, new: int = 0, autoraise: bool = True) -> bool:
            opened.append(url)
            return True

    class Terminal(camera_preview.webbrowser.GenericBrowser):
        def open(self, url: str, new: int = 0, autoraise: bool = True) -> bool:
            raise AssertionError("a terminal browser would take over the interview")

    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(camera_preview.webbrowser, "get", lambda: Desktop("xdg-open"))
    assert open_in_browser("http://localhost:1/x/") is True
    assert opened == ["http://localhost:1/x/"]
    monkeypatch.setattr(camera_preview.webbrowser, "get", lambda: Terminal("lynx"))
    assert open_in_browser("http://localhost:1/x/") is False


def test_device_holders_reports_other_processes_holding_the_camera(tmp_path: Path) -> None:
    usb = tmp_path / "sys" / "8-1"
    (usb / "8-1:1.0" / "video4linux" / "video18").mkdir(parents=True)
    (usb / "busnum").write_text("8\n")
    (usb / "devnum").write_text("2\n")
    proc = tmp_path / "proc"
    recorder = proc / "282269"
    (recorder / "fd").mkdir(parents=True)
    (recorder / "comm").write_text("python3\n")
    os.symlink("/dev/video18", recorder / "fd" / "8")
    libusb = proc / "4242"
    (libusb / "fd").mkdir(parents=True)
    os.symlink("/dev/bus/usb/008/002", libusb / "fd" / "3")
    unrelated = proc / "999"
    (unrelated / "fd").mkdir(parents=True)
    os.symlink("/dev/video2", unrelated / "fd" / "1")
    mine = proc / str(os.getpid())
    (mine / "fd").mkdir(parents=True)
    os.symlink("/dev/video18", mine / "fd" / "1")
    (proc / "self").mkdir()

    candidate = CameraCandidate("realsense:261922270754", usb_dir=str(usb))

    assert device_holders(candidate, proc) == ["PID 282269 python3", "PID 4242"]
    assert device_holders(CameraCandidate("realsense:X"), proc) == []
    assert device_holders(candidate, tmp_path / "no-proc") == []


def test_lan_addresses_lists_non_loopback_ipv4(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = "\n".join(
        [
            "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever",
            "2: enp1s0    inet 10.0.0.243/24 brd 10.0.0.255 scope global dynamic enp1s0",
            "3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0",
            "11: tailscale0    inet 100.101.1.2/32 scope global tailscale0",
            "garbage",
        ]
    )

    class Result:
        stdout = listing

    monkeypatch.setattr(camera_preview.subprocess, "run", lambda *_a, **_k: Result())
    assert lan_addresses() == [("10.0.0.243", "enp1s0"), ("100.101.1.2", "tailscale0")]
    monkeypatch.setattr(
        camera_preview.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(OSError("no ip"))
    )
    assert lan_addresses() == []


def test_encode_jpeg_uses_the_locked_opencv() -> None:
    image = np.zeros((360, 640, 3), dtype=np.uint8)
    image[:, :, 0] = 255
    assert encode_jpeg(image).startswith(b"\xff\xd8")


def test_open_frame_source_uses_the_yam_fork_readers(monkeypatch: pytest.MonkeyPatch) -> None:
    import inspect_robots_yam.embodiment as embodiment

    assert hasattr(embodiment, "_OpenCVCameraReader")
    assert hasattr(embodiment, "_ProcessRealsenseCameraReader")
    built: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    class Reader:
        def __init__(self, kind: str, devices: dict[str, str], **kwargs: Any) -> None:
            built.append((kind, devices, kwargs))
            self.closed = False

        def __call__(self, cfg: Any) -> dict[str, np.ndarray]:
            assert (cfg.cam_width, cfg.cam_height) == (640, 360)
            return {"preview": np.zeros((360, 640, 3), dtype=np.uint8)}

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        embodiment, "_OpenCVCameraReader", lambda devices: Reader("v4l2", devices)
    )
    monkeypatch.setattr(
        embodiment,
        "_ProcessRealsenseCameraReader",
        lambda serials, **kwargs: Reader("realsense", serials, **kwargs),
    )

    realsense = open_frame_source(CameraCandidate("realsense:261022277065"))
    v4l = open_frame_source(CameraCandidate(D435_BY_ID))

    assert realsense.read().shape == (360, 640, 3)
    assert v4l.read().shape == (360, 640, 3)
    realsense.close()
    assert built == [
        ("realsense", {"preview": "261022277065"}, {"depth_fps": 30}),
        ("v4l2", {"preview": D435_BY_ID}, {}),
    ]
