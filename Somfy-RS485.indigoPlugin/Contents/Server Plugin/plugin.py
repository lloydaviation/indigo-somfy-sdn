"""
Somfy SDN (RS485) Indigo plugin.

Bus / sub-device architecture (per Brian's design + radiora2 prior art):
  - "Somfy Bus" (custom device) owns ONE USB-RS485 dongle; multiple buses allowed. It opens
    the serial port and runs a reader + writer thread. All bus I/O is serialized and PACED.
  - "Roller Shade" (dimmer device) = one motor (NodeID); references its bus via pluginProps.
  - "Shade Group" (dimmer device) = one SDN hardware group (GroupID); one frame moves all.

Design rationale (Brian's field experience — see project.md "Reliability"):
  - Software fan-out dropped commands; HARDWARE GROUPS are reliable -> group device = 1 frame.
  - No inter-command handshake -> async fire-and-forget through a queue.
  - Bus needs pacing -> writer enforces INTER_CMD (>= SDN Treq 25ms).
  - Motors report status -> reader routes reports to the child device by NodeID.
  - Dropped commands -> point-to-point commands request ACK and retry on no-ACK.

The SDN frame codec is somfy_sdn.py (unit-tested standalone). Indigo bundles pyserial; we use
it directly with PARITY_ODD (Somfy SDN = 4800 8-O-1), which avoids the openSerial parity
ambiguity. Not yet run under Indigo/hardware — bring-up TODOs are flagged.
"""

import threading
import queue
import time

import indigo
import serial                      # bundled with Indigo
import somfy_sdn as sdn            # local codec


class Bus(object):
    """Owns one dongle: serial port + reader/writer threads + paced, retrying TX queue."""

    INTER_CMD = 0.030              # >= SDN Treq (25ms); spacing between transactions on the wire
    REPLY_WINDOW = 0.35            # max wait for a unicast poll reply (SDN Trep <= 255ms) + margin
    DISCOVER_WINDOW = 0.8          # longer window to collect several colliding broadcast replies
    FRAME_GAP = 0.004              # idle gap that delimits an inbound frame (Tfree<3ms)

    def __init__(self, plugin, dev):
        self.plugin = plugin
        self.devId = dev.id
        self.name = dev.name
        props = dev.pluginProps
        # The 'serialport' ConfigUI field stores composite keys (serialPort_serialConnType,
        # serialPort_serialPortLocal, ...), NOT a plain 'serialPort'. Use Indigo's helper to
        # assemble the URL (confirmed against the bundled miniSerial reference plugin).
        self.port = plugin.getSerialPortUrl(props, "serialPort")
        self.baud = int(props.get("baud", "4800") or "4800")
        mval = (props.get("masterAddress") or "").strip()
        try:
            self.master = sdn.parse_node_id(mval) if mval else sdn.MASTER_DEFAULT
        except Exception:
            self.master = sdn.MASTER_DEFAULT
        self.ser = None
        self.outq = queue.Queue()
        self.children = {}         # NodeID tuple -> indigo device id (rollerShade inbound routing)
        self._pending = None       # (legacy, unused) ACK-wait slot; kept so _handle stays inert
        self._last_tx = None       # bytes of our most recent TX, for half-duplex echo/collision check
        self._disc = None          # discovery collector {'addrs':set,'labels':dict} while scanning
        self._stop = threading.Event()

    # ---- open / close ----
    def open(self):
        if not self.port:
            raise ValueError("no serial port selected — edit the Somfy Bus device and choose the "
                             "PL2303 dongle in the 'USB RS485 dongle' field")
        self.ser = serial.Serial(self.port, self.baud, bytesize=serial.EIGHTBITS,
                                 parity=serial.PARITY_ODD, stopbits=serial.STOPBITS_ONE,
                                 timeout=0.05)
        self._stop.clear()
        threading.Thread(target=self._io_loop, name="somfy-io-%d" % self.devId,
                         daemon=True).start()

    def close(self):
        self._stop.set()
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    def register_child(self, node_id, devId):
        self.children[node_id] = devId

    def unregister_child(self, devId):
        self.children = {n: i for n, i in self.children.items() if i != devId}

    def discover(self, rounds=6, window=0.8):
        """Broadcast GET_NODE_ADDR for a few rounds, collect replying NodeIDs, then fetch each
        label. Returns {(a,b,c): label}. Call from a menu/action thread (it sleeps); frames go
        through the paced writer, replies are gathered by the reader thread. Multiple rounds
        because simultaneous broadcast replies can collide (spec 6.1.1)."""
        self._disc = {"addrs": set(), "labels": {}}
        try:
            for _ in range(rounds):
                self.send(sdn.discover_nodes(self.master), expect_reply=True, multi=True)
                time.sleep(window)
            addrs = set(self._disc["addrs"])
            for node in addrs:
                self.send(sdn.get_node_label(self.master, node), expect_reply=True)
            time.sleep(0.1 * len(addrs) + 0.6)          # let labels arrive (writer is paced)
            return {n: self._disc["labels"].get(n, "") for n in addrs}
        finally:
            self._disc = None

    # ---- half-duplex request/response I/O: send one frame; if it expects a reply (GET poll /
    #      discovery), read until the reply decodes BEFORE sending the next. These motors answer
    #      only when polled, so overlapping GETs would make their replies collide (the bug behind
    #      "refresh not working" with 7 shades) — serializing costs nothing and fixes it. ----
    def send(self, frame, expect_reply=False, multi=False):
        """Queue a frame. expect_reply=True (GET/discovery) -> read the reply before the next send.
        multi=True -> read the whole window for several replies (broadcast discovery); else return
        on the first complete reply."""
        self.outq.put((frame, expect_reply, multi))

    def _io_loop(self):
        while not self._stop.is_set():
            try:
                frame, expect_reply, multi = self.outq.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if self.plugin.debug:
                    self.plugin.logger.debug("%s TX %s" % (self.name, sdn.hexs(frame)))
                try:
                    self.ser.reset_input_buffer()          # drop stale bytes before the request
                except Exception:
                    pass
                self.ser.write(frame)
                self.ser.flush()                           # block until TX is on the wire (turnaround)
                if expect_reply:
                    self._read_reply(self.DISCOVER_WINDOW if multi else self.REPLY_WINDOW,
                                     until_first=not multi)
            except Exception:
                self.plugin.logger.exception("%s: I/O error" % self.name)
            finally:
                self.outq.task_done()
            self._stop.wait(self.INTER_CMD)                # pace the bus (>= Treq 25ms)

    def _read_reply(self, window, until_first=True):
        """Read for up to `window` s, dispatching each complete frame (split on the idle gap).
        With until_first, return as soon as one frame is dispatched (a single poll reply)."""
        buf = bytearray()
        last = time.monotonic()
        deadline = last + window
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                chunk = self.ser.read(64)
            except Exception:
                self.plugin.logger.exception("%s: RX error" % self.name)
                return
            now = time.monotonic()
            if chunk:
                buf.extend(chunk)
                last = now
            elif buf and (now - last) > self.FRAME_GAP:
                self._handle(bytes(buf))
                buf = bytearray()
                if until_first:
                    return                                 # got the reply -> done
        if buf:
            self._handle(bytes(buf))

    def _handle(self, raw):
        if self._last_tx is not None and raw == self._last_tx:    # heard our own transmission
            self._last_tx = None
            if self.plugin.debug:
                self.plugin.logger.debug("%s: TX echo confirmed (%d B) — collision-free"
                                         % (self.name, len(raw)))
            return                                                # don't process our own frame
        try:
            f = sdn.decode_frame(raw)
        except ValueError:
            if self.plugin.debug:
                self.plugin.logger.debug("%s RX undecodable %s" % (self.name, sdn.hexs(raw)))
            return
        if self.plugin.debug:
            self.plugin.logger.debug("%s RX %s -> msg=0x%02X src=%s"
                                     % (self.name, sdn.hexs(raw), f["msg"], f["src"]))
        msg = f["msg"]
        if msg in (sdn.ACK, sdn.NACK):
            p = self._pending
            if p is not None:
                p["ok"] = (msg == sdn.ACK)
                p["nack"] = f["data"][0] if (msg == sdn.NACK and f["data"]) else None
                p["evt"].set()
            return
        if self._disc is not None and msg in (sdn.POST_NODE_ADDR, sdn.POST_NODE_LABEL):
            if msg == sdn.POST_NODE_ADDR:
                self._disc["addrs"].add(f["src"])           # NodeID is the reply's src
            else:
                self._disc["labels"][f["src"]] = sdn.parse_node_label(f["data"])
            return
        devId = self.children.get(f["src"])
        if devId is None:
            return
        dev = indigo.devices[devId]
        if msg == sdn.POST_POSITION:
            self.plugin.update_position(dev, f["data"])   # [pulses16, percent8]


class Plugin(indigo.PluginBase):

    POS_TOL = 6                # tolerance in 0-255 position units (~2.4%) for "reached target"
    ACTIVE_POLL = 0.5          # while a command is outstanding, poll ~2Hz to stream position --
                               # these motors report ONLY when polled (confirmed on the wire; the
                               # "2Hz stream" under the Autelis was IT polling, not unsolicited).
    STALL_TIME = 2.5           # if the encoder (pulses) hasn't moved for this long, motion has stopped
    MAX_REISSUE = 3            # reissue a command that produced NO motion this many times, then block
    IDLE_RESYNC = 30.0         # s between idle position re-syncs (startup + external-move catch)

    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.buses = {}            # busDevId -> Bus
        self.orphans = {}          # busDevId -> set(childDevId) started before their bus
        self.discovered = {}       # busDevId -> {nodeStr: label} from the last scan
        self._target = {}          # devId -> target percent (0=open,100=closed); set = command outstanding
        self._reported = {}        # devId -> last VALID reported percent (0..100; for UI)
        self._pulses = {}          # devId -> last reported encoder pulses (reliable motion signal)
        self._moved = {}           # devId -> True once the encoder has moved since the command
        self._progress_t = {}      # devId -> monotonic of last encoder change (motion-stopped detection)
        self._report_t = {}        # devId -> monotonic of last report
        self._cmd_t = {}           # devId -> monotonic of last (re)issue toward the target
        self._reissue = {}         # devId -> count of no-motion reissues toward the current target
        self._lastpoll = {}        # devId -> monotonic of last poll (fast while moving, slow when idle)
        self.debug = pluginPrefs.get("showDebugInfo", False)

    # ---- prefs ----
    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        if not userCancelled:
            self.debug = valuesDict.get("showDebugInfo", False)

    # ---- ConfigUI dynamic list: the parent-bus picker for sub-devices ----
    def gatewayList(self, filter="", valuesDict=None, typeId="", targetId=0):
        buses = [(str(d.id), d.name) for d in indigo.devices.iter("self")
                 if d.deviceTypeId == "somfyBus"]
        buses.sort(key=lambda x: x[1].lower())
        return buses

    def discoveredMotorList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Motors found by 'Discover Somfy Motors'. Show the chosen bus's motors; if no bus is
        picked yet (fresh device), show ALL discovered (one bus in typical setups) so the list
        isn't empty before the Bus field is touched."""
        try:
            busId = int((valuesDict or {}).get("busId", "") or 0)
        except (TypeError, ValueError):
            busId = 0
        found = dict(self.discovered.get(busId, {}))
        if not found:                                    # no bus picked yet, or none for that bus
            for d in self.discovered.values():
                found.update(d)
        out = [(n, "%s  %s" % (n, lbl or "(no label)")) for n, lbl in sorted(found.items())]
        out.append(("__manual__", "— enter NodeID manually —"))
        return out

    # ---- Plugins menu: discover motors on the bus ----
    def discoverMotors(self):
        if not self.buses:
            self.logger.warning("Discover: no connected Somfy Bus devices to scan.")
            return
        for busId, bus in list(self.buses.items()):
            name = indigo.devices[busId].name
            self.logger.info("Discover: scanning %s ..." % name)
            try:
                found = bus.discover()
            except Exception:
                self.logger.exception("Discover: error scanning %s" % name)
                continue
            d = self.discovered.setdefault(busId, {})    # ACCUMULATE across scans (don't overwrite)
            for n, lbl in found.items():
                key = sdn.node_id_str(n)
                if lbl or key not in d:                  # keep a real label; never clobber with blank
                    d[key] = lbl
            if found:
                self.logger.info("%s: %d motor(s) this scan; %d known total:"
                                 % (name, len(found), len(d)))
                for key in sorted(d):
                    self.logger.info("   %s  %s" % (key, d[key] or "(no label)"))
                self.logger.info("Re-run discovery until the total stops growing — broadcast "
                                 "replies collide, so each scan sees a random subset.")
            else:
                self.logger.warning("%s: no motors replied this scan (known total: %d)."
                                    % (name, len(d)))

    def getDeviceConfigUiValues(self, pluginProps, typeId, devId):
        """Pre-fill the bus picker so it needn't be chosen each time: use the remembered default
        bus (last created/used), else the sole bus when there's only one."""
        values = pluginProps
        if typeId in ("rollerShade", "shadeGroup") and not values.get("busId"):
            buses = [d.id for d in indigo.devices.iter("self") if d.deviceTypeId == "somfyBus"]
            default = self.pluginPrefs.get("defaultBusId", "")
            try:
                have_default = bool(default) and int(default) in buses
            except ValueError:
                have_default = False
            if have_default:
                values["busId"] = default
            elif len(buses) == 1:
                values["busId"] = str(buses[0])
        return (values, indigo.Dict())

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        if typeId == "rollerShade":
            pick = valuesDict.get("motorPick", "")
            if pick and pick != "__manual__":            # chose a discovered motor
                valuesDict["motorAddress"] = pick
            if not valuesDict.get("busId") or not valuesDict.get("motorAddress"):
                errs = indigo.Dict(); errs["motorAddress"] = "Pick or enter a motor NodeID."
                return (False, valuesDict, errs)
            try:
                sdn.parse_node_id(valuesDict["motorAddress"])
            except Exception:
                errs = indigo.Dict(); errs["motorAddress"] = "Bad NodeID (use AA:BB:CC)."
                return (False, valuesDict, errs)
            valuesDict["address"] = valuesDict["motorAddress"]
        elif typeId == "shadeGroup":
            if not valuesDict.get("busId") or not valuesDict.get("groupAddress"):
                errs = indigo.Dict(); errs["groupAddress"] = "Select a bus and enter a GroupID."
                return (False, valuesDict, errs)
            try:
                sdn.parse_node_id(valuesDict["groupAddress"])
            except Exception:
                errs = indigo.Dict(); errs["groupAddress"] = "Bad GroupID (use AA:BB:CC)."
                return (False, valuesDict, errs)
            valuesDict["address"] = valuesDict["groupAddress"]
        if valuesDict.get("busId"):
            self.pluginPrefs["defaultBusId"] = valuesDict["busId"]   # remember the operator's bus
        return (True, valuesDict)

    def _autoname_bus(self, dev):
        """A freshly-created bus is named after its type; give it a friendly default (SomfySDN1,
        SomfySDN2, …) so the operator needn't invent one. Any operator-chosen name is left alone."""
        if dev.name != "Somfy Bus (RS485)" and not dev.name.startswith("new device"):
            return                                            # operator named it -> keep it
        existing = {d.name for d in indigo.devices}
        n = 1
        while ("SomfySDN%d" % n) in existing:
            n += 1
        try:
            dev.name = "SomfySDN%d" % n
            dev.replaceOnServer()
            self.logger.info("named new Somfy bus '%s'" % dev.name)
        except Exception:
            self.logger.exception("could not auto-name bus device")

    # ---- device lifecycle ----
    def deviceStartComm(self, dev):
        if dev.deviceTypeId == "somfyBus":
            self._autoname_bus(dev)                           # friendly default name for a fresh bus
            self.pluginPrefs["defaultBusId"] = str(dev.id)    # remember it for new shade/group devices
            try:
                bus = Bus(self, dev)
                bus.open()
                self.buses[dev.id] = bus
                dev.updateStateOnServer("status", "connected")
                self.logger.info("%s: SDN bus open on %s @ 4800 8-O-1"
                                 % (dev.name, bus.port))
                for childId in self.orphans.pop(dev.id, set()):   # attach early-started children
                    c = indigo.devices[childId]
                    if c.enabled:
                        self._attach_child(c)
            except Exception:
                self.logger.exception("%s: failed to open bus" % dev.name)
                dev.setErrorStateOnServer("open failed")
        elif dev.deviceTypeId in ("rollerShade", "shadeGroup"):
            self._attach_child(dev)

    def deviceStopComm(self, dev):
        if dev.deviceTypeId == "somfyBus":
            bus = self.buses.pop(dev.id, None)
            if bus:
                bus.close()
            dev.updateStateOnServer("status", "disconnected")
        elif dev.deviceTypeId == "rollerShade":
            bus = self.buses.get(self._bus_id(dev))
            if bus:
                bus.unregister_child(dev.id)

    def _bus_id(self, dev):
        try:
            return int(dev.pluginProps.get("busId", "0"))
        except (TypeError, ValueError):
            return 0

    def _attach_child(self, dev):
        busId = self._bus_id(dev)
        bus = self.buses.get(busId)
        if bus is None:                               # bus not up yet -> defer
            self.orphans.setdefault(busId, set()).add(dev.id)
            dev.setErrorStateOnServer("waiting for bus")
            return
        if dev.deviceTypeId == "rollerShade":         # register for inbound report routing
            try:
                bus.register_child(sdn.parse_node_id(dev.pluginProps["motorAddress"]), dev.id)
            except Exception:
                self.logger.error("%s: bad motor NodeID" % dev.name)
        dev.setErrorStateOnServer("")                 # clear "waiting for bus" once attached

    def _bus_for(self, dev):
        bus = self.buses.get(self._bus_id(dev))
        if bus is None:
            self.logger.error("%s: its Somfy Bus is not connected" % dev.name)
        return bus

    def _endpoints(self, dev):
        """(src, dest, expect_ack). Group: GroupID in SOURCE@, 00:00:00 dest. The Autelis did
        NOT request ACK on the 0x54 control frames, so we don't either (expect_ack=False)."""
        if dev.deviceTypeId == "shadeGroup":
            return sdn.parse_node_id(dev.pluginProps["groupAddress"]), sdn.ADDR_GROUP_DEST, False
        bus = self.buses.get(self._bus_id(dev))
        master = bus.master if bus else sdn.MASTER_DEFAULT
        return master, sdn.parse_node_id(dev.pluginProps["motorAddress"]), False

    # ---- named action callbacks (shades + groups) ----
    def actionOpen(self, action, dev):
        self._command(dev, 100)                         # brightness 100 = fully open

    def actionClose(self, action, dev):
        self._command(dev, 0)                           # brightness 0 = fully closed

    def actionStop(self, action, dev):
        if self._send_cmd(dev, sdn.stop):
            self._target.pop(dev.id, None)              # stop => abandon any convergence goal
            self._poll(dev)                             # read where it actually stopped

    def actionSetPosition(self, action, dev):
        self._command(dev, int(action.props.get("percent", 50)))

    def actionQueryPosition(self, action, dev):
        self._poll(dev)

    def _send_cmd(self, dev, builder):
        bus = self._bus_for(dev)
        if not bus:
            return False
        src, dst, _ = self._endpoints(dev)
        bus.send(builder(src, dst))
        return True

    def _command(self, dev, brightness):
        """Issue a position command (brightness 100=open) and, for a shade, arm closed-loop
        convergence: the target is tracked and _reconcile reissues it if it stalls short. Groups
        are addressed via _endpoints and fire-and-forget (no single-shade position to track)."""
        bus = self._bus_for(dev)
        if not bus:
            return
        brightness = max(0, min(100, int(brightness)))
        pos = round((100 - brightness) * 255 / 100)     # 0-255: brightness 100->0 open, 0->255 closed
        src, dst, _ = self._endpoints(dev)
        bus.send(sdn.set_position(src, dst, pos))
        self._reflect(dev, brightness, moving=True)
        if dev.deviceTypeId == "rollerShade":
            now = time.monotonic()
            self._target[dev.id] = pos                  # target in 0-255 position units (matches report)
            self._cmd_t[dev.id] = now
            self._progress_t[dev.id] = now              # stall timer starts at the command
            self._moved[dev.id] = False                 # has the encoder moved since this command?
            self._reissue[dev.id] = 0

    def _reflect(self, dev, brightness, moving=False):
        """Immediate UI feedback on command. A GROUP has no position report, so its brightness is
        command-only and set here. A SHADE is driven by live polling (update_position), so we do
        NOT set its brightness/position optimistically — doing that fought the real reports and made
        the on/off toggle and % snap when the shade hadn't physically moved yet. Just flag moving."""
        brightness = max(0, min(100, int(brightness)))
        if dev.deviceTypeId == "shadeGroup":
            dev.updateStateOnServer("brightnessLevel", brightness, uiValue="%d%%" % brightness)
        elif dev.deviceTypeId == "rollerShade" and moving:
            dev.updateStateOnServer("windowState", "moving")

    def _poll(self, dev):
        """Ask one shade for its position (GET 0x44 -> motor POST 0x64 -> update_position)."""
        bus = self.buses.get(self._bus_id(dev))
        if not bus or dev.deviceTypeId != "rollerShade":
            return
        try:
            dst = sdn.parse_node_id(dev.pluginProps["motorAddress"])
        except Exception:
            return
        bus.send(sdn.query_position(bus.master, dst), expect_reply=True)

    # ---- closed-loop reconcile: status arrives async from the report stream; here we verify each
    #      outstanding command reached its target and reissue if it stalled short (Brian's design) ----
    def runConcurrentThread(self):
        try:
            while True:
                now = time.monotonic()
                for dev in indigo.devices.iter("self"):
                    if dev.deviceTypeId != "rollerShade" or not dev.enabled:
                        continue
                    if not self.buses.get(self._bus_id(dev)):
                        continue
                    self._reconcile(dev, now)
                self.sleep(0.25)
        except self.StopThread:
            pass

    def _reconcile(self, dev, now):
        target = self._target.get(dev.id)
        if target is None:                               # idle: slow re-sync (startup + external moves)
            if now - self._lastpoll.get(dev.id, 0.0) >= self.IDLE_RESYNC:
                self._poll(dev)
                self._lastpoll[dev.id] = now
            return
        # command outstanding -> poll FAST to stream position (motor reports only when polled)
        if now - self._lastpoll.get(dev.id, 0.0) >= self.ACTIVE_POLL:
            self._poll(dev)
            self._lastpoll[dev.id] = now
        reported = self._reported.get(dev.id)
        if reported is not None and abs(reported - target) <= self.POS_TOL:
            self._finish(dev)                            # reached commanded position
            return
        if now - self._progress_t.get(dev.id, now) < self.STALL_TIME:
            return                                       # encoder still advancing (or start grace)
        # motion has stopped:
        if self._moved.get(dev.id):                      # it DID move -> command took effect; stop here
            self._finish(dev)
            return
        # encoder never moved -> command was not acted on (lost) -> reissue
        n = self._reissue.get(dev.id, 0)
        if n >= self.MAX_REISSUE:
            self.logger.error("%s: no motion after %d reissues (want %d%%) — giving up"
                              % (dev.name, n, target))
            dev.updateStateOnServer("windowState", "blocked")
            self._finish(dev)
            return
        self._reissue[dev.id] = n + 1
        self._cmd_t[dev.id] = now
        self._progress_t[dev.id] = now
        self.logger.warning("%s: no motion, reissuing move to %d%% (%d/%d)"
                            % (dev.name, target, n + 1, self.MAX_REISSUE))
        bus = self.buses.get(self._bus_id(dev))
        src, dst, _ = self._endpoints(dev)
        bus.send(sdn.set_position(src, dst, round(target * 255 / 100)))

    def _finish(self, dev):
        for d in (self._target, self._reissue, self._moved):
            d.pop(dev.id, None)

    # Indigo dimmer dispatch (slider, Set Brightness, Home/voice, On/Off) for both types
    def actionControlDimmerRelay(self, action, dev):
        A = indigo.kDimmerRelayAction
        if action.deviceAction == A.SetBrightness:
            self._command(dev, int(action.actionValue))
        elif action.deviceAction == A.TurnOn:
            self.actionOpen(action, dev)
        elif action.deviceAction == A.TurnOff:
            self.actionClose(action, dev)
        elif action.deviceAction == A.Toggle:           # on/off toggle to the left of the slider
            if dev.states.get("brightnessLevel", 0) > 0:
                self.actionClose(action, dev)           # any opening -> close
            else:
                self.actionOpen(action, dev)            # fully closed -> open
        elif action.deviceAction in (A.BrightenBy, A.DimBy):
            cur = dev.states.get("brightnessLevel", 0)
            delta = int(action.actionValue)
            self._command(dev, cur + delta if action.deviceAction == A.BrightenBy else cur - delta)

    # ---- inbound report handler (called by Bus reader thread) ----
    def update_position(self, dev, data):
        """POST_POSITION (0x64): data = [pulses16 LSBF, position8]. position is 0-255 (0=open,
        255=closed), the same scale as the command. Motion is detected from the encoder pulses."""
        try:
            p = sdn.parse_position_report(data)
        except ValueError:
            return
        now = time.monotonic()
        pulses, pos = p["pulses"], p["position"]
        self._report_t[dev.id] = now
        prev = self._pulses.get(dev.id)
        self._pulses[dev.id] = pulses
        if prev is not None and pulses != prev:          # encoder moved => motion (reliable signal)
            self._progress_t[dev.id] = now
            self._moved[dev.id] = True
        self._reported[dev.id] = pos                     # 0-255 position -> feeds convergence
        brightness = round(100 - pos * 100 / 255)        # Indigo brightness: 100 = open
        dev.updateStateOnServer("brightnessLevel", brightness, uiValue="%d%%" % brightness)
        dev.updateStateOnServer("positionPercent", round(pos * 100 / 255))
        target = self._target.get(dev.id)
        if target is not None and abs(pos - target) > self.POS_TOL:
            win = "moving"                               # command outstanding, not there yet
        else:
            win = "open" if brightness >= 99 else "closed" if brightness <= 1 else "partial"
        dev.updateStateOnServer("windowState", win)
