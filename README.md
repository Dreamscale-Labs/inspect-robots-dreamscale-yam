# DreamZero-YAM through Dreamscale

This public composition is the attended, fail-closed path for running DreamZero-YAM through
Dreamscale on a bimanual I2RT YAM rig. It is deliberately detachable from Dreamscale core: YAM
hardware behavior lives in Dreamscale's YAM fork, the generic policy bridge remains in
`inspect-robots-dreamscale`, and this repo owns only installation, rig configuration, diagnostics,
gates and cleanup.

No credentials or rig-specific configuration are stored in this repository.

## Copy, paste, run

On the Linux computer connected to both arms and all three cameras:

```bash
git clone --branch stable --depth 1 \
  https://github.com/Dreamscale-Labs/inspect-robots-dreamscale-yam.git
cd inspect-robots-dreamscale-yam
./setup.sh
./dreamscale-yam doctor
./dreamscale-yam run "Pack container"
```

`"Pack container"` is an exact in-distribution task from
[`allenai/01122025-box-01`](https://huggingface.co/datasets/allenai/01122025-box-01),
one of the repositories recorded in the checkpoint's
[`experiment_cfg/conf.yaml`](https://huggingface.co/robocurve/dreamzero-yam-molmoact2/blob/f9b72b8dfa124f7283c5b1d467ce2ff9253c737a/experiment_cfg/conf.yaml)
training mixture. Use it only with a scene arranged for that task; replace the quoted text with
another trained task when the scene differs.

`setup.sh` installs `uv` when needed and creates the locked Python 3.12 project environment. No
manual virtual environment activation is required.

`stable` is the customer-facing release channel. Dreamscale fast-forwards it only after an
immutable versioned release passes the local and Linux release gates.

### Upgrading a rig set up before the rename

This repository and its command were renamed to Dreamscale. On a computer that already has the
older checkout, run from that checkout:

```bash
git remote set-url origin https://github.com/Dreamscale-Labs/inspect-robots-dreamscale-yam.git
git pull --ff-only
./setup.sh
./dreamscale-yam doctor
```

`./setup.sh` moves the confirmed rig from `~/.config/dropbear-yam` to `~/.config/dreamscale-yam`,
and local run state from `~/.local/state/dropbear-yam` to `~/.local/state/dreamscale-yam`, once. It
never overwrites an existing Dreamscale directory, so the rig interview keeps your confirmed values.
An existing sign-in keeps working. Use `./dreamscale-yam` from then on.

### Upgrading to v0.1.21: remember each arm's CAN adapter

From an existing checkout, `git pull --ff-only`, `./setup.sh`, then `./dreamscale-yam doctor`. A rig
confirmed by an earlier release keeps working exactly as before: it still drives the CAN interface
names it saved, and its shadow receipt stays valid.

Kernel names such as `can0` and `can1` can swap after a reboot or replug, which would drive the
wrong arm. To remember each arm by its USB-CAN adapter instead, run once at the rig:

```bash
./dreamscale-yam identify-can
./dreamscale-yam doctor
```

It changes only the CAN part of the confirmed rig; cameras, collision geometry and step limits are
kept. The first run afterwards records one new shadow; later renames do not. The saved file then
uses rig `schema_version = 3`, which v0.1.20 and older refuse with a clear message, so do not go
back to an older checkout with that rig file.

`setup.sh` is safe to rerun. It installs missing Debian/Ubuntu build prerequisites only after one
explicit sudo confirmation, installs `uv` when absent, reproduces `uv.lock`, and launches the rig
interview. The first rig is automatically stored as `default`; Jay does not name it or
pass `--rig`. Existing confirmed values are kept. To deliberately replace it:

```bash
./dreamscale-yam setup --reconfigure
```

If browser sign-in is temporarily unavailable, the confirmed rig remains saved. Retry with
`./dreamscale-yam login`, then run `./dreamscale-yam doctor`, without repeating camera or CAN selection.

Camera discovery probes every color-capable V4L2 node; it does not assume that color is
`video-index0`. It joins V4L2 and librealsense identities when Linux exposes a common USB port or
RealSense serial; ambiguous duplicate serials are deliberately kept separate. It prefers
Robocurve's documented layout—D435 top over V4L2 and each D405 wrist through an isolated
RealSense process. An
unambiguous `/dev/v4l/by-id` name is preferred; `/dev/v4l/by-path` is the stable fallback for
identical cameras with ambiguous or empty serials.

The interview asks only for facts software cannot safely infer:

- which detected stable camera source is top, left and right;
- which USB-CAN adapter controls the left and right arm;
- whether to enable optional predictive collision checking; if yes, it first shows that you will
  need measured left/right arm-base `(x, y, z)` and yaw plus table-top `z` in one shared frame; and
- Dreamscale login, but only when credentials are absent.

Each camera is listed with its model and serial when known, plus its stable source, for example
`2. Intel RealSense D405 · serial 261022277065 · realsense:261022277065`. Before the first camera
question, setup starts a small camera preview page and prints its links:

```text
Camera preview: open a link below in a browser and match each numbered picture to the numbered list.
  http://localhost:41873/Wq3...Zt/   (on this computer)
  http://10.0.0.243:41873/Wq3...Zt/   (from another computer, enp1s0)
```

Open either link (the second one works from a laptop on the same network while you are SSH'd in).
Each numbered tile shows a live picture from the camera with the same number in the list, and is
marked `assigned: top`, `left` or `right` as you answer. A camera that another program is using
shows `unavailable` with the reason. The link contains a random token; nothing else is served. The
page stops and every camera is closed as soon as the third camera is chosen, and also on an error or
Ctrl-C. If the page cannot start, setup prints one line and the numbered list works as before.

For the arms, setup identifies each USB-CAN adapter by watching which one disappears when you unplug
it:

1. `Unplug the USB-CAN adapter of the LEFT arm (leave the RIGHT one plugged in).` Setup continues on
   its own once it sees the adapter disappear; you do not press Enter.
2. `Plug it back in now.` Setup waits for the same adapter to return, even under a different name.
   If it comes back DOWN, setup prints the exact command,
   `sudo ip link set <interface> up type can bitrate 1000000`, and offers to run it after a `y/N`
   question.
3. With two adapters, the other one is the RIGHT arm; with more, setup repeats step 1 for the RIGHT
   arm. It then shows `left arm = … (USB serial …), right arm = …` and asks you to confirm.

Nothing is sent to the arms during this step. If you are not at the rig, type `list` and press Enter
at the first unplug prompt to pick the interfaces from a numbered list instead. Either way setup
remembers each adapter by its USB serial, or by its USB port when it has no serial; an adapter with
neither is remembered by name with a warning. Doctor and run resolve the saved adapters to their
current interface names every time, and refuse with a clear message if one is missing.

If you answer `n` to collision geometry, those measurements are omitted. The run still enforces
the pinned joint bounds, finite 14-value actions and per-action movement limits. Every finite target
is capped to the intersection of those limits and then continues; requested and applied values are
recorded locally. Malformed actions and hardware failures still stop the run. Without geometry it
simply cannot predict arm/arm or arm/table contact from a geometric model. You can add geometry
later with `./dreamscale-yam setup --reconfigure`.

It writes `~/.config/dreamscale-yam/rigs/default.toml` with permissions `0600`. The file contains no
API key. Authentication remains in Dreamscale's own config. To configure an additional physical
rig, give only that additional rig a name:

```bash
./dreamscale-yam setup --rig jay-rig-2
```

Two more commands help at the rig without changing the robot:

```bash
./dreamscale-yam cameras        # the same numbered camera preview; changes no configuration
./dreamscale-yam identify-can   # re-identify only the arms' CAN adapters (add --rig NAME if needed)
```

`cameras` runs until Ctrl-C and then closes every camera. `identify-can` updates only the CAN part
of an existing confirmed rig and keeps cameras, collision geometry and step limits. Both refuse
while `run` is using a rig, and `run` refuses while either of them is open.

With one configured rig, the short commands above remain unambiguous. Once multiple profiles are
configured, name the physical rig on every command so the program never guesses:

```bash
./dreamscale-yam doctor --rig default
./dreamscale-yam run --rig default "Pack container"
```

## What doctor proves

```bash
./dreamscale-yam doctor
./dreamscale-yam doctor --json
./dreamscale-yam doctor --support-bundle ~/dreamscale-yam-support.tar.gz
```

Doctor performs no robot motion, does not construct the I2RT motor driver, and creates no Dreamscale
model session. It checks the exact locked package commits, Linux/build prerequisites,
authentication, DreamZero-YAM entitlement and target availability, system clock synchronization,
camera roles/shapes/fresh Unix-epoch timestamps, observed cross-camera skew, CAN state, I2RT model
limits, the selected collision-checking mode, end-to-end 30 Hz declarations, and the absence of a
conflicting Dreamscale session. Exactly one owned, parked DreamZero-YAM reservation is accepted so
the next run can reclaim it without another cold start.

Camera-source checks accept RealSense serials plus stable `/dev/v4l/by-id` and
`/dev/v4l/by-path` identities. A raw `/dev/videoN` source is rejected because its number can
change on replug. Doctor opens the same mixed camera-reader composition used by the live run, but
never calls hardware preparation or reset, so the motor driver and gripper calibration remain
behind the later physical-motion gate.

For a rig with saved CAN adapters, `DBY-CAN` first finds each adapter by its USB serial or port and
checks the interface it is attached to now. It passes and names the current interfaces when they
differ from the saved names, and fails with the next step when an adapter is not connected (for
example `The LEFT arm's CAN adapter (USB serial 208137AD45465006) is not connected`). The left and
right arms can never resolve to the same interface.

Doctor reports the measured cross-camera timestamp spread for observability. A spread above 50 ms
is a non-blocking warning, not an inference or motion rejection. The required camera contract is
that each source timestamp is valid and each frame is individually fresh.

Every failure has a stable `DBY-*` code and blocks `run`. The optional support archive contains the
doctor result and redacted configuration for self-guided debugging or sharing with Dreamscale.
Warnings are reserved for non-blocking observability; safety and configuration problems are
failures.

## What run does

`run` always repeats doctor first. It then asks `Continue? [Y/n]` after explaining that the e-stop
must be ready and that connecting will enable I2RT control traffic and calibrate both
`LINEAR_4310` grippers. Keep hands clear of the grippers.

By default, the exact loaded Dreamscale compute stays warm for up to five minutes after the run so a
follow-up run can reclaim it. Warm retention is billed at the full rate. Choose any whole number
from 0 through 60 minutes:

```bash
./dreamscale-yam run --warm=15 "Pack container"
./dreamscale-yam run --warm=0 "Pack container"  # terminate immediately after cleanup
```

The terminal prints the exact parked session and its stop command. The reservation expires
automatically at the selected deadline.

After that confirmation the program:

1. opens cameras and I2RT once, performs required gripper calibration, and observes the real
   pre-home state without sending an arm pose;
2. for a new configuration digest, blocks for exactly one real model chunk, validates its first
   action through the same cap-and-continue projection against the measured pre-home state, and
   never executes that action (an expected initial `async_latest` hold cannot satisfy this shadow
   check);
3. reuses the same hardware connection and, when shadow was needed, the same Dreamscale session;
4. retains the YAM fork's stand-clear homing prompt and scene-ready prompt;
5. runs Inspect Robots programmatically with a composition-owned action projection and, when
   configured, predictive collision guards—no interpolation or hold substitution. Every finite
   target is capped to the intersection of the configured joint bounds and the per-action movement
   window before collision review. Malformed or non-finite actions, impossible references and
   predicted collisions still abort without sending; and
6. synchronously closes hardware and policy on success, abort, exception, signal or operator stop,
   then verifies that the exact owned Dreamscale session parked when `--warm` is positive or
   disappeared when `--warm=0`.

If the requested park does not finish or ordinary zero-warm close does not remove that exact
session, the program explicitly stops only that session and exits nonzero. It never uses a
stop-all operation.

Shadow evidence is stored under `~/.local/state/dreamscale-yam/shadow/`. Any change to the locked
package commits, model target, camera/CAN mapping, rig geometry, cadence, joint bounds or step
limits changes the digest and requires a new shadow. When the rig remembers its CAN adapters, the
digest uses those adapter identities rather than the kernel interface names, so a rename such as
`can0` becoming `can1` keeps the receipt while swapping which adapter drives which arm does not.
The run always drives the interface each saved adapter is attached to now. Shadow validates integration only; it is not a
physical-safety or task-success claim. The strict YAM validator remains active as the
hardware-facing backstop on every action.

### Advanced: change the per-action caps

The default cap is `0.2` radians for each arm joint and `1.0` normalized stroke for each gripper.
Most users should keep it. An advanced operator can hand-edit `step_limits` in the confirmed rig
TOML (normally `~/.config/dreamscale-yam/rigs/default.toml`) using this packed order:

```toml
step_limits = [
  0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 1.0,
  0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 1.0,
]
```

The values must be 14 finite positive numbers. Doctor reports the effective vector and warns—but
does not block—when any value is above the recommended default. A change invalidates the previous
shadow receipt automatically.

While Dreamscale compute is starting, the terminal shows a small loading symbol and elapsed seconds.
The default episode cap is `--max-steps 3600`, which is 120 seconds at the fixed 30 Hz action
timebase. To choose a shorter attended run, pass a smaller positive value explicitly.

## Cadence: exactly 30 Hz

This release supports DreamZero-YAM at exactly 30 Hz. That is the checkpoint/data action timebase;
it is not 30 cloud inference calls per second and it is not I2RT's internal motor servo frequency.
Arbitrary 5–30 Hz operation is intentionally unavailable until observation production, wire
metadata, temporal admission, inference and action execution consume one resolved rate end to end.

## Before the first physical task

Do not proceed without separate authorization for physical motion and inference. For the
first short trained task, an operator must stand clear with the e-stop in hand. Acceptance requires
correct 640x360 camera roles and source times, 30 Hz on both policy and YAM, at least one telemetry
row whose action source is `model`, no silently changed action, working gates/abort behavior, and
either a verified exact parked reservation or, with `--warm=0`, absence of the exact Dreamscale
session afterward.

Run logs, frames, actions and adapter telemetry are under `~/.local/state/dreamscale-yam/logs/`.

## Locked components

`composition.lock.toml` is the human-readable identity contract and `uv.lock` is the complete
resolver lock. They pin the Dreamscale YAM fork, generic Dreamscale adapter, Dreamscale SDK, Inspect
Robots and I2RT. Do not hand-edit installed packages; update the locks and repeat doctor/shadow.
