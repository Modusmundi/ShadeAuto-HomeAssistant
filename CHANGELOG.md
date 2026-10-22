# Changelog

All notable changes to this integration are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.4.0] - 2026-10-22

### Added
- Vane tilt on sheer (PerfectSheer) shades: `open_cover_tilt`,
  `close_cover_tilt`, `set_cover_tilt_position`, `stop_cover_tilt` and the
  `current_tilt_position` attribute (0 = closed, 100 = open). Tilt commands
  lower the shade if it is not already down, because the hub only honours
  vanes at the bottom.
- `stop_cover` for the lift, using the hub's `MotorStop` command.
- Faster polling while a shade is moving (`motion_poll_sec` option).
- `tilt_enabled` option to hide tilt on shades without vanes.
- Reconfigure flow for changing the hub's address.
- Device page shows the shade model (PerfectSheer) and motor firmware.
- `tools/hubwatch.py`, a stdlib tool that watches the hub and can send
  single commands; `docs/HUB_PROTOCOL.md` and `docs/ARCHITECTURE.md`.
- Test suite with a fake hub, and CI (ruff, mypy, pytest, bandit, hassfest,
  HACS validation, CodeQL).

### Changed
- The hub is now the source of truth. Positions and vane state converge to
  what the hub reports, including moves made from the ShadeAuto app or a
  remote; the slider animation is cosmetic and yields to the hub.
- Every lift command carries an explicit vane target so the hub cannot
  silently close the vanes; lowering fully keeps the current vane setting.
- Shades are named after the hub's `PeripheralName` instead of
  "Shade <id>". Entity ids and unique ids are unchanged.
- Verification re-sends a command at most once and skips the retry when the
  shade was re-commanded from the app or a remote in the meantime.
- Options changes reload the entry; a hub that is briefly unreachable at
  startup is retried instead of failing setup.
- Requires Home Assistant 2026.10 or newer.

### Fixed
- Home Assistant showing a stale position after a command had settled, after
  a failed command, or after the shade was moved from the app or a remote.
- Hub rejections of a command (`Error` in the reply) are reported as service
  errors instead of being ignored.

### Removed
- The `/notification` long-poll watcher and the `notification_timeout_sec`
  option; the hub never delivers notifications. Stored values of the option
  are ignored.
- `Archive.zip`, an old snapshot of the source tree.

## [0.3.0] - 2025-11-17

Upstream release by kevinlester: lift control, battery sensors, position
estimation and verify-and-retry.
