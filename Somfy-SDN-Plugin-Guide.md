# Somfy SDN (RS485) — Indigo Plugin
## Installation & Operation Guide

*Plugin version 1.0.0 · Brian Lloyd (lloyd.aero) · for Indigo Domotics*

---

## 1. Overview

The **Somfy SDN (RS485)** plugin lets Indigo control Somfy **SDN** (Sonesse Digital Network) roller
shades directly over a USB-to-RS485 adapter, replacing a dedicated controller such as the Autelis
SC100SDN. It provides:

- **Per-shade control** — open, close, stop, and set an exact position (0–100 %).
- **Group control** — one frame moves an SDN hardware group.
- **Live position feedback** while a shade travels, plus a periodic re-sync that also catches
  changes made at a wall remote.
- **Closed-loop reliability** — a command that doesn't move the motor (e.g. a bus collision) is
  detected and automatically reissued.

The plugin **commands** motors that have already been configured (limits, rotation, group
membership) by an external SDN tool. It does **not** yet program motors — see §9.

---

## 2. Requirements

- **Indigo** 2022.1 or later (developed and tested on 2025.2, plugin API 3.0).
- A **USB ↔ RS485 adapter.** An *isolated* adapter is recommended in RF-noisy environments.
  Tested: DSD TECH **SH-U11H** (isolated; Prolific **PL2303GC**). FTDI-based adapters also work and
  have the advantage of a built-in serial number, so they always resolve to the same device node.
  - On **macOS Sequoia / Apple Silicon**, a Prolific adapter needs the **"PL2303 Serial"** DriverKit
    driver from the Mac App Store. After installing it, the adapter appears as
    `/dev/cu.PL2303G-USBtoUART…`. The Prolific GC has no unique serial, so its node is keyed to the
    USB **port location** — **plug it into a fixed USB port** for a stable node.
- An RS485 connection to the Somfy SDN bus: the **A/B** differential pair. At 4800 baud over
  house-length runs, bus termination is normally unnecessary; if a *distant* motor is unreliable,
  add a single **120 Ω** resistor across A/B at the far end.
- Somfy **SDN** motors (e.g. Ø30 DC Sonesse) that are already configured with travel limits and,
  if desired, group memberships (see §4).

---

## 3. Installing the plugin

1. Connect the USB-RS485 adapter and confirm its node exists: `ls /dev/cu.*`. Install the Prolific
   driver first if the node is missing.
2. Double-click the **`Somfy-RS485.indigoPlugin`** bundle. Indigo loads and enables it.
3. *(Optional, recommended during setup)* enable **debug logging** in the plugin's preferences — it
   prints every frame sent and received, which is invaluable while mapping motors.

---

## 4. Configure the motors first (external system)

**This plugin controls motors; it does not program them.** Before using it, set up each motor with
an **external SDN configuration system** — the Autelis SC100SDN you are replacing, or Somfy's SDN
ConfigTool driving an SDN programming interface:

- Set each motor's **up / down limits** and **rotation direction**.
- Define any **groups** you want (group memberships) — e.g. "living room", "master bedroom".
- Optionally **label** the motors.

You then carry the resulting **addresses** into this plugin — each motor's **NodeID** (§6) and each
group's **GroupID** (§7). Once that is done you can disconnect or retire the external controller;
the plugin replaces it for everyday control.

> A single bus is a shared medium with one master at a time. Don't leave the old controller
> actively polling the bus while the plugin runs — two masters collide. Use one or the other.

---

## 5. Create the Bus device

1. **New Device → Type: `Somfy Bus (RS485)`**.
2. **USB RS485 dongle:** select the adapter's serial port.
3. **Controller (MASTER) NodeID:** leave blank for the default **`7F:7F:7F`**. Only change it if a
   different live controller on the bus already uses that address.
4. Save. The device auto-names **`SomfySDN1`** (rename it if you wish — whatever you choose is
   remembered and **pre-selected** when you create shade and group devices). Create one bus per
   adapter; multiple buses are supported.

The Indigo log should show `… SDN bus open on /dev/cu.… @ 4800 8-O-1`.

---

## 6. Get the motor addresses from the motors (discovery)

Every motor has a 3-byte **NodeID** (e.g. `06:64:AB`). To read them off the bus:

1. **Plugins → Somfy SDN (RS485) → Discover Somfy Motors (scan bus).**
2. The plugin broadcasts an address query; every motor answers with its NodeID. Results are logged
   and added to the motor pick-list used in §7.
3. **Run it several times.** All motors reply at once, so on any single scan some replies collide;
   the plugin **accumulates** discovered motors across scans. Re-run until the *known total* stops
   growing.
4. **A motor may not self-discover** if it is electrically marginal — the most distant motor on an
   unterminated bus, for example. Discovery is the bus's most collision-prone operation, so a weak
   node can lose every round even though it works perfectly for normal (one-at-a-time) control. In
   that case, get its NodeID from your external configuration tool (or by elimination) and enter it
   manually in §7.

---

## 7. Create the shade and group devices

### Roller shades — one per motor

1. **New Device → Type: `Somfy Roller Shade`**.
2. **Somfy Bus:** already selected (the default bus).
3. **Motor:** pick a discovered NodeID from the list, or choose **"— enter NodeID manually —"** and
   type it as `AA:BB:CC`.
4. Name it for the window. Save.
5. **Confirm which motor is which:** command the shade (e.g. set 50 %) and watch which window moves;
   rename if needed. NodeIDs are not in any meaningful order, so verify by observation.

### Shade groups — one per SDN hardware group

1. **New Device → Type: `Somfy Shade Group`**.
2. **Somfy Bus:** already selected.
3. **Group address (GroupID):** enter the group's ID as `AA:BB:CC`, taken from your external
   configuration tool. If unsure, enter a candidate and command the group — the shades that move
   tell you which group it is.
4. Save. Commanding the group moves all its members in a single frame.

---

## 8. Operating the shades

> **Convention:** **On = up = open**; **Off = down = closed**. The slider and brightness are
> **percent _open_** — **100 % = fully open (up)**, **0 % = fully closed (down)**. So turning the
> device "on" raises the shade, "off" lowers it, and 30 % means 30 % open.

- **Open / Close** — the on/off control next to the slider, the in-device buttons, or the Indigo
  actions **Open (up)** / **Close (down)**.
- **Set position** — the brightness slider (**% open**, 100 = fully open) or the **Set Position (%)**
  action.
- **Stop** — the **Stop** action.
- **Status** — while a shade travels the plugin polls it ~2×/second and the position tracks live;
  when idle it re-syncs every **30 s**, so a change made at a wall remote appears within ~30 s. The
  **Window State** shows *open / closed / partial / moving*; the **Position %** state reports how far
  **closed** the shade is (the complement of the slider's % open).
- **Reliability** — every position command is checked against the motor's reported motion. If a
  command produced *no* movement (e.g. a collision swallowed it), the plugin reissues it
  automatically; after repeated failures it marks the shade *blocked*.
- **Groups** — a group device is **command-only**: a hardware group has no single position to
  report. Its member shade devices each show their own position and re-sync on the periodic scan.

**Indigo Actions** (for triggers, schedules, and control pages): *Open*, *Close*, *Stop*,
*Set Position (%)*, and — per shade — *Query Position*.

---

## 9. Programming the shades (future)

This release controls motors that were configured elsewhere (§4). A **future version will add a
companion application to program the shades directly** — setting up/down limits, jogging a motor to
a position, and defining groups and presets — so an external configuration system will no longer be
required.

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| No serial port to choose, or *"no serial port selected"* | Adapter driver not installed or node missing. Confirm `/dev/cu.*` exists, install the Prolific driver, use a fixed USB port. |
| Shade shows *"waiting for bus"* | The Bus device isn't running. Open it, confirm the serial port, and check the log for `SDN bus open`. |
| A motor won't appear in discovery but controls fine | Expected for a marginal/distant motor — enter its NodeID manually (§7); optionally fit 120 Ω termination at the far end of the bus. |
| Positions don't refresh / seem stale | Confirm the Bus device is running and only this plugin (not the old controller) is driving the bus. Enable debug logging to watch the polls and replies. |
| Need to see what's on the wire | Enable **debug logging** in the plugin preferences — it logs every TX and RX frame. |

---

## 11. License

Free to use under the **MIT License** — you may use, modify, and redistribute the plugin provided
the copyright notice and attribution to **Brian Lloyd (lloyd.aero)** are retained. See the
`LICENSE` file included with the plugin.

---

*Somfy SDN (RS485) plugin for Indigo · © 2026 Brian Lloyd (lloyd.aero) · MIT License.*
