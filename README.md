# Somfy SDN (RS485) — Indigo Plugin

Control Somfy **SDN** (Sonesse Digital Network) roller shades directly from
[Indigo](https://www.indigodomo.com/) over a USB-to-RS485 adapter — a software replacement for a
dedicated controller such as the Autelis SC100SDN.

## Features

- Per-shade **open / close / stop / set position (0–100 %)**.
- **Group** control — one frame moves an SDN hardware group.
- **Live position** feedback while a shade moves, plus a periodic re-sync that catches changes made
  at a wall remote.
- **Closed-loop reliability** — a command that doesn't move the motor (e.g. a bus collision) is
  detected and reissued automatically.

The plugin **commands** motors that have already been configured (limits, rotation, groups) by an
external SDN tool; it does not yet *program* motors — a future version will add that.

## Requirements

- Indigo 2022.1 or later (tested on 2025.2).
- A USB ↔ RS485 adapter (isolated recommended; FTDI or Prolific). **pyserial ships with Indigo**, so
  there is nothing else to install.
- Somfy SDN motors already set up with travel limits (and any groups) via an external SDN tool.

## Install

1. Download the latest release `.zip` (or clone this repo) and unzip it.
2. Double-click **`Somfy-RS485.indigoPlugin`** — Indigo loads and enables it.
3. Follow the **[Installation & Operation Guide](Somfy-SDN-Plugin-Guide.md)** to create the bus
   device, discover motors, and add shade and group devices.

## Documentation

Full setup and operation: **[Somfy-SDN-Plugin-Guide.md](Somfy-SDN-Plugin-Guide.md)**.

## License

MIT — free to use, modify, and redistribute; please retain the copyright/attribution. See
[LICENSE](LICENSE). © 2026 Brian Lloyd (lloyd.aero).
