# Changelog

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
