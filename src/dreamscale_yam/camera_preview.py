"""A small local web page that shows every discovered camera, numbered like the menu.

The interview lists cameras by stable source (a RealSense serial or a long
``/dev/v4l`` name), which does not tell an operator which one is the top view.
This page shows a live picture per camera so the numbers can be matched by eye.

It uses only the standard library HTTP server in a daemon thread and opens each
camera through the same YAM fork readers the doctor and live run use, one
reader per camera so a busy or broken camera only affects its own tile. The URL
carries a random token; every other path is a 404. Nothing here may block
setup: callers treat any failure as "no preview" and keep the text menu.
"""

from __future__ import annotations

import contextlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import webbrowser
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol
from urllib.parse import urlsplit

import numpy as np
import numpy.typing as npt

from dreamscale_yam.config import camera_source

logger = logging.getLogger(__name__)

PREVIEW_FPS = 4.0
PREVIEW_WIDTH = 640
PREVIEW_HEIGHT = 360
JPEG_QUALITY = 75
MAX_READ_FAILURES = 10
_FRAME_PATH = re.compile(r"frame/([1-9][0-9]*)\.jpg\Z")
_SOURCE_WIDTH = 60
_IGNORED_INTERFACES = ("lo", "docker", "br-", "veth", "virbr")


@dataclass(frozen=True)
class CameraCandidate:
    """One discovered camera: the stable source setup saves, plus what a person sees."""

    source: str
    model: str = ""
    serial: str = ""
    usb_dir: str = ""

    def menu_label(self) -> str:
        """Return ``Model · serial S · source`` when known, else the source itself."""
        parts = [self.model] if self.model else []
        if self.serial:
            parts.append(f"serial {self.serial}")
        if not parts:
            return self.source
        return " · ".join([*parts, short_source(self.source)])


def short_source(source: str, width: int = _SOURCE_WIDTH) -> str:
    """Shorten a long ``/dev/v4l`` name, keeping its serial and index at the end."""
    text = source.removeprefix("/dev/v4l/")
    if len(text) <= width:
        return text
    head = width // 3
    tail = width - head - 1
    return f"{text[:head]}…{text[-tail:]}"


class FrameSource(Protocol):
    """One open camera that returns RGB frames."""

    def read(self) -> npt.NDArray[np.uint8]: ...

    def close(self) -> None: ...


class _YamReaderSource:
    """Adapt one YAM fork camera reader to a single-camera frame source."""

    _SLOT = "preview"
    _SIZE: Any = SimpleNamespace(cam_width=PREVIEW_WIDTH, cam_height=PREVIEW_HEIGHT)

    def __init__(self, reader: Any) -> None:
        self._reader = reader

    def read(self) -> npt.NDArray[np.uint8]:
        captured = self._reader(self._SIZE)
        return np.asarray(captured[self._SLOT], dtype=np.uint8)

    def close(self) -> None:
        self._reader.close()


def open_frame_source(candidate: CameraCandidate) -> FrameSource:
    """Open one camera through the reader class the doctor and live run use.

    V4L2 sources use the YAM fork's draining OpenCV reader; RealSense serials
    use its isolated capture process, exactly as ``realsense_capture="process"``
    configures for a run. Devices open on the first ``read``.
    """
    from inspect_robots_yam.embodiment import (  # pyright: ignore[reportPrivateUsage]
        _OpenCVCameraReader,
        _ProcessRealsenseCameraReader,
    )

    kind, value = camera_source(candidate.source)
    slot = _YamReaderSource._SLOT
    if kind == "realsense":
        return _YamReaderSource(_ProcessRealsenseCameraReader({slot: value}, depth_fps=30))
    return _YamReaderSource(_OpenCVCameraReader({slot: value}))


def encode_jpeg(rgb: npt.NDArray[np.uint8]) -> bytes:
    """Encode one RGB frame as JPEG with the locked OpenCV build."""
    import cv2

    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    ok, buffer = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return bytes(buffer.tobytes())


def device_holders(candidate: CameraCandidate, proc: Path = Path("/proc")) -> list[str]:
    """Return other processes that hold this camera's device nodes open.

    A camera already streaming in another program (another run, or a
    recorder) is reported instead of being opened a second time.
    """
    if not candidate.usb_dir:
        return []
    usb_dir = Path(candidate.usb_dir)
    nodes = {f"/dev/{video.name}" for video in usb_dir.glob("*/video4linux/video*")}
    try:
        bus = int((usb_dir / "busnum").read_text(encoding="utf-8").strip())
        device = int((usb_dir / "devnum").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    else:
        nodes.add(f"/dev/bus/usb/{bus:03d}/{device:03d}")
    if not nodes:
        return []
    holders: list[str] = []
    try:
        processes = sorted(proc.iterdir())
    except OSError:
        return []
    for process in processes:
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            descriptors = list((process / "fd").iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if target in nodes:
                try:
                    command = (process / "comm").read_text(encoding="utf-8").strip()
                except OSError:
                    command = ""
                holders.append(f"PID {process.name}" + (f" {command}" if command else ""))
                break
    return holders


def lan_addresses() -> list[tuple[str, str]]:
    """Return ``(IPv4 address, interface)`` for every non-loopback address."""
    try:
        result = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    addresses: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4 or "inet" not in fields:
            continue
        interface = fields[1].rstrip(":")
        address = fields[fields.index("inet") + 1].split("/", 1)[0]
        if address.startswith("127.") or interface.startswith(_IGNORED_INTERFACES):
            continue
        if (address, interface) not in addresses:
            addresses.append((address, interface))
    return addresses


def _reason(exc: BaseException) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text if len(text) <= 240 else text[:239] + "…"


@dataclass
class _Tile:
    number: int
    candidate: CameraCandidate
    status: str = "starting"
    reason: str = ""
    jpeg: bytes | None = None
    frames: int = 0
    role: str = ""


class CameraPreview:
    """Live, numbered camera pictures on a token-protected local web page."""

    def __init__(
        self,
        candidates: Sequence[CameraCandidate],
        *,
        open_source: Callable[[CameraCandidate], FrameSource] = open_frame_source,
        holders: Callable[[CameraCandidate], list[str]] = device_holders,
        encode: Callable[[npt.NDArray[np.uint8]], bytes] = encode_jpeg,
        addresses: Callable[[], list[tuple[str, str]]] = lan_addresses,
        host: str = "0.0.0.0",
        port: int = 0,
        fps: float = PREVIEW_FPS,
        token: str | None = None,
    ) -> None:
        self._tiles = [
            _Tile(number, candidate) for number, candidate in enumerate(candidates, 1)
        ]
        self._open_source = open_source
        self._holders = holders
        self._encode = encode
        self._addresses = addresses
        self._host = host
        self._port = port
        self._period = 1.0 / fps
        self.token = token or secrets.token_urlsafe(18)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # Opening a RealSense camera spawns a capture process; one at a time
        # keeps librealsense device enumeration from racing between them.
        self._realsense_open = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._workers: list[threading.Thread] = []

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Bind the page, then open every camera in its own background thread."""
        server = _PreviewServer((self._host, self._port), _Handler)
        server.preview = self
        self._server = server
        self._server_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="dreamscale-yam-preview-http",
            daemon=True,
        )
        self._server_thread.start()
        for tile in self._tiles:
            worker = threading.Thread(
                target=self._work,
                args=(tile,),
                name=f"dreamscale-yam-preview-camera-{tile.number}",
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()

    def close(self, timeout_s: float = 20.0) -> bool:
        """Stop the page and close every camera; True when all cameras closed in time."""
        self._stop.set()
        server = self._server
        if server is not None:
            with contextlib.suppress(Exception):
                server.shutdown()
            with contextlib.suppress(Exception):
                server.server_close()
        if self._server_thread is not None:
            self._server_thread.join(timeout=2.0)
        deadline = time.monotonic() + timeout_s
        for worker in self._workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        return all(not worker.is_alive() for worker in self._workers)

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("the camera preview has not started")
        return int(self._server.server_address[1])

    def urls(self) -> list[tuple[str, str]]:
        """Return ``(url, where)`` pairs: localhost first, then each LAN address."""
        port = self.port
        found = [(f"http://localhost:{port}/{self.token}/", "on this computer")]
        try:
            addresses = self._addresses()
        except Exception:
            addresses = []
        for address, interface in addresses:
            found.append(
                (f"http://{address}:{port}/{self.token}/", f"from another computer, {interface}")
            )
        return found

    def assign(self, source: str, role: str) -> None:
        """Mark the tile showing ``source`` as assigned to a camera role."""
        with self._lock:
            for tile in self._tiles:
                if tile.candidate.source == source:
                    tile.role = role

    # -- state ------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "closed": self._stop.is_set(),
                "cameras": [
                    {
                        "number": tile.number,
                        "model": tile.candidate.model,
                        "serial": tile.candidate.serial,
                        "source": tile.candidate.source,
                        "status": tile.status,
                        "reason": tile.reason,
                        "role": tile.role,
                        "frames": tile.frames,
                    }
                    for tile in self._tiles
                ],
            }

    @property
    def camera_count(self) -> int:
        return len(self._tiles)

    def frame(self, number: int) -> bytes | None:
        with self._lock:
            if 1 <= number <= len(self._tiles):
                return self._tiles[number - 1].jpeg
        return None

    def page(self) -> str:
        return _render_page(self._tiles)

    def _set(self, tile: _Tile, status: str, reason: str = "") -> None:
        with self._lock:
            tile.status = status
            tile.reason = reason

    def _publish(self, tile: _Tile, rgb: npt.NDArray[np.uint8]) -> None:
        jpeg = self._encode(rgb)
        with self._lock:
            tile.jpeg = jpeg
            tile.frames += 1
            tile.status = "live"
            tile.reason = ""

    # -- camera workers ---------------------------------------------------

    @contextlib.contextmanager
    def _open_slot(self, realsense: bool) -> Iterator[bool]:
        """Serialize RealSense opens; yield False when the preview is closing."""
        if not realsense:
            yield not self._stop.is_set()
            return
        while not self._realsense_open.acquire(timeout=0.2):
            if self._stop.is_set():
                yield False
                return
        try:
            yield not self._stop.is_set()
        finally:
            self._realsense_open.release()

    def _work(self, tile: _Tile) -> None:
        """Open one camera, publish JPEGs at the preview rate, always close it."""
        try:
            holders = self._holders(tile.candidate)
        except Exception:
            holders = []
        if holders:
            self._set(
                tile,
                "unavailable",
                f"in use by another program ({', '.join(holders)})",
            )
            return
        source: FrameSource | None = None
        try:
            realsense = camera_source(tile.candidate.source)[0] == "realsense"
            with self._open_slot(realsense) as proceed:
                if not proceed:
                    return
                source = self._open_source(tile.candidate)
                frame = source.read()
            self._publish(tile, frame)
            failures = 0
            while not self._stop.wait(self._period):
                try:
                    frame = source.read()
                except Exception as exc:
                    failures += 1
                    if failures >= MAX_READ_FAILURES:
                        self._set(
                            tile, "unavailable", f"stopped delivering frames: {_reason(exc)}"
                        )
                        return
                    continue
                failures = 0
                self._publish(tile, frame)
        except Exception as exc:
            self._set(tile, "unavailable", _reason(exc))
        finally:
            if source is not None:
                try:
                    source.close()
                except Exception:
                    logger.debug("closing preview camera %s failed", tile.number, exc_info=True)


class _PreviewServer(ThreadingHTTPServer):
    daemon_threads = True
    preview: CameraPreview


class _Handler(BaseHTTPRequestHandler):
    server: _PreviewServer
    server_version = "dreamscale-yam-preview"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Stay silent: the interview owns the terminal."""

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(send_body=False)

    def do_GET(self) -> None:  # noqa: N802
        self._serve(send_body=True)

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        send_body: bool,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _not_found(self, *, send_body: bool) -> None:
        self._send(404, b"Not found\n", "text/plain; charset=utf-8", send_body=send_body)

    def _serve(self, *, send_body: bool) -> None:
        preview = self.server.preview
        path = urlsplit(self.path).path
        parts = path.split("/", 2)
        token = parts[1] if len(parts) > 1 else ""
        if not hmac.compare_digest(token.encode(), preview.token.encode()):
            self._not_found(send_body=send_body)
            return
        if len(parts) == 2:
            self._send(
                308,
                b"",
                "text/plain; charset=utf-8",
                send_body=send_body,
                headers={"Location": f"/{preview.token}/"},
            )
            return
        rest = parts[2]
        if rest == "":
            body = preview.page().encode()
            self._send(200, body, "text/html; charset=utf-8", send_body=send_body)
            return
        if rest == "state.json":
            body = json.dumps(preview.state()).encode()
            self._send(200, body, "application/json", send_body=send_body)
            return
        match = _FRAME_PATH.fullmatch(rest)
        if match is None or int(match.group(1)) > preview.camera_count:
            self._not_found(send_body=send_body)
            return
        jpeg = preview.frame(int(match.group(1)))
        if jpeg is None:
            self._send(
                503, b"No picture yet\n", "text/plain; charset=utf-8", send_body=send_body
            )
            return
        self._send(200, jpeg, "image/jpeg", send_body=send_body)


def open_in_browser(url: str) -> bool:
    """Open the page on this computer's desktop, never in a terminal browser."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        browser = webbrowser.get()
    except webbrowser.Error:
        return False
    background = isinstance(browser, webbrowser.BackgroundBrowser) or bool(
        isinstance(browser, webbrowser.UnixBrowser) and getattr(browser, "background", False)
    )
    if not background:
        return False
    try:
        return bool(browser.open(url))
    except Exception:
        return False


def announce(preview: CameraPreview, output: Callable[[str], None]) -> None:
    """Print the one-line instruction and every URL the operator can open."""
    output(
        "Camera preview: open a link below in a browser and match each numbered picture to "
        "the numbered list."
    )
    urls = preview.urls()
    for url, where in urls:
        output(f"  {url}   ({where})")
    if urls and open_in_browser(urls[0][0]):
        output("  (opened in this computer's browser)")


_PAGE_STYLE = """
:root { color-scheme: light dark; --bg: #f6f6f4; --card: #ffffff; --ink: #1d1d1b;
  --muted: #6b6b66; --line: #deded8; --live: #1f8a4c; --bad: #b3261e; --accent: #2f5bd3; }
@media (prefers-color-scheme: dark) { :root { --bg: #151514; --card: #1f1f1d; --ink: #ededea;
  --muted: #a2a29b; --line: #34342f; --live: #4cc38a; --bad: #f2b8b5; --accent: #8fb0ff; } }
* { box-sizing: border-box; }
body { margin: 0; padding: 20px 16px 32px; background: var(--bg); color: var(--ink);
  font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
header { max-width: 1200px; margin: 0 auto 16px; }
h1 { font-size: 20px; margin: 0 0 4px; }
header p { margin: 0; color: var(--muted); }
#ended { display: none; margin-top: 10px; padding: 8px 12px; border: 1px solid var(--line);
  border-radius: 8px; background: var(--card); }
main { max-width: 1200px; margin: 0 auto; display: grid; gap: 16px;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 340px), 1fr)); }
article { background: var(--card); border: 2px solid var(--line); border-radius: 12px;
  overflow: hidden; }
article.assigned { border-color: var(--accent); }
.top { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-start;
  padding: 12px 12px 10px; }
.num { flex: none; width: 40px; height: 40px; border-radius: 50%; background: var(--ink);
  color: var(--card); display: grid; place-items: center; font-size: 20px; font-weight: 700; }
.meta { min-width: 0; flex: 1; }
.model { font-weight: 600; }
.serial { color: var(--muted); }
.source { color: var(--muted); font: 12px/1.35 ui-monospace, SFMono-Regular, Menlo, monospace;
  overflow-wrap: anywhere; }
.role { display: none; flex: none; padding: 3px 10px; border-radius: 999px;
  background: var(--accent); color: var(--card); font-weight: 700; font-size: 13px;
  text-transform: uppercase; }
article.assigned .role { display: inline-block; }
.picture { position: relative; aspect-ratio: 16 / 9; background: #000; }
.picture img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; }
.picture [hidden] { display: none; }
.placeholder { position: absolute; inset: 0; display: grid; place-items: center; padding: 16px;
  color: #b9b9b2; text-align: center; font-size: 14px; }
.status { padding: 8px 12px 12px; color: var(--muted); font-size: 14px; }
.status.live::before { content: "\\25CF  "; color: var(--live); }
.status.unavailable { color: var(--bad); }
"""

_PAGE_SCRIPT = """
const cards = new Map();
document.querySelectorAll("article[data-number]").forEach((card) => {
  cards.set(Number(card.dataset.number), {
    card, img: card.querySelector("img"), role: card.querySelector(".role"),
    status: card.querySelector(".status"), placeholder: card.querySelector(".placeholder"),
    refreshing: false,
  });
});
let failures = 0;
let ended = false;
function refresh(number) {
  const entry = cards.get(number);
  if (ended) return;
  entry.img.onload = () => {
    entry.img.hidden = false;
    entry.placeholder.hidden = true;
    setTimeout(() => refresh(number), 200);
  };
  entry.img.onerror = () => setTimeout(() => refresh(number), 1000);
  entry.img.src = `frame/${number}.jpg?t=${Date.now()}`;
}
function render(state) {
  for (const camera of state.cameras) {
    const entry = cards.get(camera.number);
    if (!entry) continue;
    entry.status.className = `status ${camera.status}`;
    entry.status.textContent = camera.status === "live" ? "live"
      : camera.status === "unavailable" ? `unavailable: ${camera.reason}` : "opening camera…";
    if (camera.status !== "live") {
      entry.img.hidden = true;
      entry.placeholder.hidden = false;
      entry.placeholder.textContent = camera.status === "unavailable"
        ? "No picture: this camera is unavailable" : "Opening camera…";
    }
    entry.role.textContent = camera.role ? `assigned: ${camera.role}` : "";
    entry.card.classList.toggle("assigned", Boolean(camera.role));
    if (camera.status === "live" && !entry.refreshing) {
      entry.refreshing = true;
      refresh(camera.number);
    }
  }
  if (state.closed) end();
}
function end() {
  ended = true;
  document.getElementById("ended").style.display = "block";
}
async function poll() {
  if (ended) return;
  try {
    const response = await fetch("state.json", { cache: "no-store" });
    if (!response.ok) throw new Error(String(response.status));
    render(await response.json());
    failures = 0;
  } catch (error) {
    failures += 1;
    if (failures >= 3) { end(); return; }
  }
  setTimeout(poll, 1000);
}
poll();
"""


def _render_page(tiles: Sequence[_Tile]) -> str:
    cards = []
    for tile in tiles:
        candidate = tile.candidate
        model = html.escape(candidate.model or "Camera")
        serial = (
            f'<div class="serial">serial {html.escape(candidate.serial)}</div>'
            if candidate.serial
            else ""
        )
        cards.append(
            f'<article data-number="{tile.number}">'
            f'<div class="top"><div class="num">{tile.number}</div>'
            f'<div class="meta"><div class="model">{model}</div>{serial}'
            f'<div class="source">{html.escape(candidate.source)}</div></div>'
            f'<span class="role"></span></div>'
            f'<div class="picture"><img hidden alt="Camera {tile.number}">'
            '<div class="placeholder">Opening camera…</div></div>'
            f'<div class="status">opening camera…</div></article>'
        )
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"referrer\" content=\"no-referrer\">"
        "<title>YAM camera preview</title>"
        f"<style>{_PAGE_STYLE}</style></head><body>"
        "<header><h1>YAM camera preview</h1>"
        "<p>Each number matches the numbered list in the setup terminal. "
        "Pictures refresh a few times per second.</p>"
        "<div id=\"ended\">The preview has ended; the cameras are closed.</div></header>"
        f"<main>{''.join(cards)}</main>"
        f"<script>{_PAGE_SCRIPT}</script></body></html>"
    )
