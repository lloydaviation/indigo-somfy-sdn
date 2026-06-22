# Changelog

## 1.0.3 — 2026-06-21

Bug fix: lowering or setting a partial position could drive a shade back the wrong way.

### Fixed
- **Reissue-on-no-motion sent a corrupted target.** Closed-loop convergence stores the target in
  0–255 wire units, but the stall-reissue path re-scaled it as a 0–100 percent (`target × 255 / 100`),
  which overflowed and wrapped — a *close* (255) came back out as 138 (~46% open). A shade that didn't
  reach its target on the first command was then driven the wrong way (the "multiple up commands" seen
  while lowering). Raising was unaffected (0 maps to 0). The reissue now sends the stored 0–255 target
  unchanged — identical to the original command.
- **Motion detection hardened.** Convergence now counts a change in *reported position* as motion, not
  only the reverse-engineered encoder-pulse field, so a shade still travelling toward its target is no
  longer mistaken for "stalled" and reissued early.

### Changed
- **Internal cleanup.** Position state and all control logic now use the 0–255 wire byte throughout;
  percent is converted only at the Indigo boundary (one helper in, one out). This removes the
  dual-representation that let the reissue bug exist in the first place.

## 1.0.2 — 2026-06-21

Serial-link resilience: the bus recovers from USB/serial drops on its own, tracks a reconnect
trend, and can alert you when a fault persists. Validated on hardware.

### Added
- **Automatic serial recovery.** On a serial I/O error (a power glitch, a USB-hub brown-out, an
  unplugged adapter) the bus closes the dead handle, reopens the same port, retries the failed frame
  once, and keeps retrying every ~3 s while down — no manual plugin reload. Previously a drop left
  the bus wedged until you reloaded the plugin.
- **Reliability trend.** Each outage logs a clean *detected → recovered* pair, and the Somfy Bus
  device gains **Reconnects** (a running count) and **Last Reconnect** (timestamp) states — so a
  trigger can warn you when reconnects start climbing (a dongle, cable, or power feed degrading
  before it fails outright).
- **Offline alert.** A per-bus *"Alert when the serial link has been down this many seconds"*
  setting (default 300 s). Past the threshold the plugin fires a **"Somfy bus offline"** trigger
  event you can route to Send Email/SMS.
- Bus device **Status** now reflects *connected / reconnecting / offline*.

### Changed
- Idle position re-sync interval lowered from 30 s to **5 s** — it doubles as the keepalive that
  makes drop-detection prompt (≤5 s). Load is negligible: the motors are mains-powered, and
  unchanged-value state updates are near-free in Indigo.

## 1.0.0 — 2026-06-19

Initial public release. Per-shade and SDN hardware-group control over USB-RS485, live position
feedback while travelling, bus discovery, and closed-loop reissue-on-no-motion.
