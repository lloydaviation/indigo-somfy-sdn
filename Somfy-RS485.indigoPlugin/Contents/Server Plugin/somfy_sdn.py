"""
somfy_sdn.py — Somfy SDN (RS485) frame codec.

Pure-stdlib Python 3. Encodes/decodes Somfy Digital Network frames. The FRAME format follows
the official Somfy SDN Integration Guide; the MESSAGE SET below is the one **validated by
wiretapping the Autelis SC100SDN driving Brian's Ø30 DC motors** (2026-06-16) — which differs
from the Guide's documented control messages. See ../../../somfy-sdn-protocol.md §11.

Runs/ self-tests standalone:   python3 "somfy_sdn.py"

Wire rules (confirmed against captured frames):
  1. Every byte is bit-inverted (0xFF - b) on the wire; de-invert on receive.
  2. NodeIDs/GroupIDs are LSBF (byte-reversed) relative to label order.
  3. Checksum = 16-bit sum of the *inverted* (wire) bytes 1..n-2, appended big-endian, NOT inverted.

VALIDATED message set (Brian's motors):
  * CTRL_MOTOR 0x54 — universal control; DATA[0] = function:
        FN_POSITION 0x10 -> DATA = [0x10, pos(0..255), 0x00]   (0 = open, 255 = closed)
        FN_STOP     0x03 -> DATA = [0x03, 0x00, 0x00]
  * GET_POSITION 0x44 (no DATA) -> POST_POSITION 0x64, DATA = [pulse_lo, pulse_hi (u16 LSBF),
        position] where position is a 0-255 byte (0=open, 255=closed) on the SAME scale as the
        CTRL_MOTOR command -- NOT a 0-100 percent. Motor reports ONLY when polled (no unsolicited).
  Addressing: point-to-point (DEST = NodeID); group (SRC = GroupID, DEST = 00:00:00);
  broadcast (DEST = FF:FF:FF).
"""

# ---- message IDs ----
CTRL_MOTOR    = 0x54   # universal motor control (function in DATA[0])
GET_POSITION  = 0x44   # poll position
POST_POSITION = 0x64   # position report: DATA = [pulses16 LSBF, percent8]
GET_NODE_ADDR  = 0x40  # broadcast discovery (untested on these motors; NodeIDs known from wiretap)
POST_NODE_ADDR = 0x60
GET_NODE_LABEL = 0x45
POST_NODE_LABEL = 0x65
ACK  = 0x7F
NACK = 0x6F

# ---- CTRL_MOTOR (0x54) function bytes ----
FN_POSITION = 0x10     # DATA[1] = position 0..255 (0 = open, 255 = closed)
FN_STOP     = 0x03     # DATA[1] = 0

# ---- special addresses ----
ADDR_BROADCAST  = (0xFF, 0xFF, 0xFF)   # DEST for "all shades"
ADDR_GROUP_DEST = (0x00, 0x00, 0x00)   # DEST when commanding a group (GroupID goes in SRC)
MASTER_DEFAULT  = (0x7F, 0x7F, 0x7F)   # our controller source addr (Autelis used FF:FF:EE)


def parse_node_id(node):
    """Accept 'AA:BB:CC' / 'AABBCC' / (a,b,c) -> tuple(a,b,c) in label order."""
    if isinstance(node, (tuple, list)):
        if len(node) != 3:
            raise ValueError("NodeID must be 3 bytes")
        return tuple(int(x) & 0xFF for x in node)
    if isinstance(node, str):
        s = node.replace(":", "").replace(" ", "").replace("-", "")
        if len(s) != 6:
            raise ValueError("NodeID hex string must be 6 hex digits")
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    raise TypeError("unsupported NodeID type: %r" % (node,))


def _addr_bytes_lsbf(node):
    a, b, c = parse_node_id(node)
    return [c, b, a]


def encode_frame(msg, src, dest, data=b"", ack=False):
    """Build a complete on-wire SDN frame (bytes): header+data inverted, checksum appended BE."""
    data = bytes(data)
    if len(data) > 21:
        raise ValueError("DATA too long (max 21)")
    total_len = 9 + len(data) + 2
    ack_len = (0x80 if ack else 0x00) | (total_len & 0x1F)
    logical = [msg & 0xFF, ack_len, 0x00]              # node_type 0x00 (master src, any dest)
    logical += _addr_bytes_lsbf(src)
    logical += _addr_bytes_lsbf(dest)
    logical += list(data)
    wire = [(0xFF - b) & 0xFF for b in logical]
    cksum = sum(wire) & 0xFFFF
    wire += [(cksum >> 8) & 0xFF, cksum & 0xFF]
    return bytes(wire)


def decode_frame(wire):
    """Parse an on-wire frame -> dict. Raises ValueError on length/checksum error."""
    wire = bytes(wire)
    if len(wire) < 11:
        raise ValueError("frame too short (%d < 11)" % len(wire))
    body = wire[:-2]
    rx_cksum = (wire[-2] << 8) | wire[-1]
    if (sum(body) & 0xFFFF) != rx_cksum:
        raise ValueError("checksum mismatch: got 0x%04X, computed 0x%04X"
                         % (rx_cksum, sum(body) & 0xFFFF))
    logical = [(0xFF - b) & 0xFF for b in body]
    ack_len = logical[1]
    return {
        "msg": logical[0],
        "ack_requested": bool(ack_len & 0x80),
        "length": ack_len & 0x1F,
        "node_type": logical[2],
        "src": tuple(reversed(logical[3:6])),
        "dest": tuple(reversed(logical[6:9])),
        "data": bytes(logical[9:]),
    }


# ---- VALIDATED control builders (CTRL_MOTOR 0x54). For a group: src=GroupID, dest=ADDR_GROUP_DEST.
#      For all shades: dest=ADDR_BROADCAST. The Autelis did NOT request ACK on these. ----

def set_position(src, dest, pos, ack=False):
    """Move to absolute position 0..255 (0 = open, 255 = closed)."""
    return encode_frame(CTRL_MOTOR, src, dest, bytes([FN_POSITION, pos & 0xFF, 0x00]), ack=ack)

def open_shade(src, dest, ack=False):
    return set_position(src, dest, 0, ack=ack)

def close_shade(src, dest, ack=False):
    return set_position(src, dest, 255, ack=ack)

def stop(src, dest, ack=False):
    """Stop in place — CTRL_MOTOR function 0x03 (validated)."""
    return encode_frame(CTRL_MOTOR, src, dest, bytes([FN_STOP, 0x00, 0x00]), ack=ack)

def query_position(src, dest):
    """Poll position: GET 0x44 (no data) -> motor replies POST 0x64."""
    return encode_frame(GET_POSITION, src, dest, b"")

def discover_nodes(src):
    """Broadcast GET_NODE_ADDR (untested on these motors; NodeIDs known from the wiretap)."""
    return encode_frame(GET_NODE_ADDR, src, ADDR_BROADCAST, b"")

def get_node_label(src, dest):
    return encode_frame(GET_NODE_LABEL, src, dest, b"")


def parse_position_report(data):
    """POST_POSITION (0x64) DATA -> {pulses, position}. position is a 0-255 byte on the SAME scale
    as the CTRL_MOTOR position command (0 = open, 255 = closed) -- NOT a 0-100 percent (confirmed
    on the wire: it climbs to 0xFF at full close). pulses is the raw u16 LSBF encoder count."""
    if len(data) < 3:
        raise ValueError("POST_POSITION needs >=3 data bytes")
    return {"pulses": data[0] | (data[1] << 8), "position": data[2]}


def parse_node_label(data):
    return bytes(data).decode("ascii", "ignore").replace("\x00", " ").rstrip()


def node_id_str(node):
    return ":".join("%02X" % b for b in node)


def hexs(b):
    return " ".join("%02X" % x for x in b)


# --------------------------------------------------------------------------- self-test
if __name__ == "__main__":
    import sys
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append("%s:\n  got  %r\n  want %r" % (name, got, want))

    # set_position 50%-ish (pos 128) round-trips with the validated payload
    d = decode_frame(set_position(MASTER_DEFAULT, "12:34:56", 128))
    check("setpos msg", d["msg"], CTRL_MOTOR)
    check("setpos dest", d["dest"], (0x12, 0x34, 0x56))
    check("setpos data", hexs(d["data"]), "10 80 00")
    check("setpos len", d["length"], 14)

    check("open data", hexs(decode_frame(open_shade(MASTER_DEFAULT, "12:34:56"))["data"]), "10 00 00")
    check("close data", hexs(decode_frame(close_shade(MASTER_DEFAULT, "12:34:56"))["data"]), "10 FF 00")
    check("stop data", hexs(decode_frame(stop(MASTER_DEFAULT, "12:34:56"))["data"]), "03 00 00")
    check("query msg", decode_frame(query_position(MASTER_DEFAULT, "12:34:56"))["msg"], GET_POSITION)

    # group addressing: GroupID in SRC, 00:00:00 in DEST (matches captured #G1 frame shape)
    g = decode_frame(set_position("00:FF:FF", ADDR_GROUP_DEST, 30))
    check("group src", g["src"], (0x00, 0xFF, 0xFF))
    check("group dest", g["dest"], (0x00, 0x00, 0x00))
    check("group data", hexs(g["data"]), "10 1E 00")
    # broadcast
    check("bcast dest", decode_frame(set_position(MASTER_DEFAULT, ADDR_BROADCAST, 0))["dest"],
          (0xFF, 0xFF, 0xFF))

    # POST_POSITION parse: DATA = [pulse_lo, pulse_hi, position(0-255; 0=open,255=closed)]
    p = parse_position_report(bytes([0x74, 0x01, 0x20]))
    check("report pulses", p["pulses"], 372)
    check("report position", p["position"], 0x20)

    check("node_id_str", node_id_str((0x06, 0x64, 0xAB)), "06:64:AB")
    check("node label", parse_node_label(b"Office          "), "Office")

    if fails:
        print("SELF-TEST FAILED (%d):" % len(fails))
        for x in fails:
            print(x)
        sys.exit(1)
    print("somfy_sdn self-test: all checks passed")
