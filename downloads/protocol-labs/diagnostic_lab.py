#!/usr/bin/env python3
"""Offline OBD/UDS examples. Python 3.10+, standard library only.

No CAN device is opened. Synthetic timestamps are end-of-frame microseconds.
Classic CAN, 8-byte frames, normal addressing, 12-bit ISO-TP FF length only.
The strict complete-trace checker is NOT an ISO-TP transport implementation.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import unittest


def supported_pids(base: int, data: bytes) -> list[int]:
    if base % 32 or not 0 <= base <= 0xC0 or len(data) != 4:
        raise ValueError("invalid PID bitmap")
    bits = int.from_bytes(data, "big")
    return [base + i for i in range(1, 33) if bits & (1 << (32 - i))]


# (payload byte count, unit, conversion). Formula is specific to each PID.
PID = {
    0x04: (1, "%", lambda b: b[0] * 100 / 255),
    0x05: (1, "degC", lambda b: b[0] - 40),
    0x06: (1, "%", lambda b: (b[0] - 128) * 100 / 128),
    0x07: (1, "%", lambda b: (b[0] - 128) * 100 / 128),
    0x0C: (2, "rpm", lambda b: int.from_bytes(b, "big") / 4),
    0x0D: (1, "km/h", lambda b: b[0]),
    0x10: (2, "g/s", lambda b: int.from_bytes(b, "big") / 100),
    0x11: (1, "%", lambda b: b[0] * 100 / 255),
}


def obd_value(pdu: bytes, pid: int, frame: int | None = None) -> tuple[float, str]:
    header = bytes([0x41, pid]) if frame is None else bytes([0x42, pid, frame])
    length, unit, convert = PID[pid]
    if len(pdu) != len(header) + length or not pdu.startswith(header):
        raise ValueError("OBD SID/PID/frame/length mismatch")
    return convert(pdu[len(header):]), unit


def obd_dtc(pair: bytes) -> str:
    if len(pair) != 2:
        raise ValueError("a DTC needs two bytes")
    a, b = pair
    return f"{'PCBU'[a >> 6]}{(a >> 4) & 3}{a & 15:X}{b:02X}"


def obd_dtcs(pdu: bytes, response_sid: int = 0x43) -> list[str]:
    if response_sid not in (0x43, 0x47, 0x4A):
        raise ValueError("unsupported DTC service")
    if not pdu or pdu[0] != response_sid or (len(pdu) - 1) % 2:
        raise ValueError("invalid DTC response")
    return [obd_dtc(pdu[i:i+2]) for i in range(1, len(pdu), 2)
            if pdu[i:i+2] != b"\x00\x00"]


def decode_dids(pdu: bytes, lengths: dict[int, int]) -> dict[int, bytes]:
    if not pdu or pdu[0] != 0x62:
        raise ValueError("not an RDBI positive response")
    pos, out = 1, {}
    while pos < len(pdu):
        if pos + 2 > len(pdu):
            raise ValueError("truncated DID")
        did = int.from_bytes(pdu[pos:pos+2], "big")
        if did not in lengths or lengths[did] <= 0 or did in out:
            raise ValueError("unknown/duplicate DID or invalid codec")
        end = pos + 2 + lengths[did]
        if end > len(pdu):
            raise ValueError("truncated DID data")
        out[did] = pdu[pos+2:end]
        pos = end
    return out


def stmin_us(code: int) -> int:
    if 0 <= code <= 0x7F:
        return code * 1000
    if 0xF1 <= code <= 0xF9:
        return (code - 0xF0) * 100
    raise ValueError("reserved STmin")


@dataclass
class Transfer:
    length: int
    data: bytearray
    sn: int = 1
    waiting_fc: bool = True
    block_size: int = 0
    block_used: int = 0
    separation: int = 0
    last_cf_end: int | None = None
    last_event: int = 0
    waits: int = 0


class TraceChecker:
    """One independent transfer per explicit link + data direction.

    A link is supplied by capture configuration, e.g. can0/11/7e0/7e8.
    FC in direction X controls data travelling in the OTHER direction.
    All frames including FC must be present; timestamps monotonically ordered.
    EOF gaps can only disprove STmin, not prove SOF-after-EOF compliance.
    """
    def __init__(self, n_bs_us=1_000_000, n_cr_us=1_000_000, max_wait=3):
        self.active: dict[tuple[str, str], Transfer] = {}
        self.n_bs, self.n_cr, self.max_wait = n_bs_us, n_cr_us, max_wait
        self.clock = -1

    def feed(self, event: dict) -> bytes | None:
        now, link, direction = event["t_us"], event["link"], event["direction"]
        raw = bytes.fromhex(event["data"])
        if direction not in ("tx", "rx") or len(raw) != 8 or now < self.clock:
            raise ValueError("invalid direction, frame size or timestamp")
        self.clock = now
        for transfer in self.active.values():
            budget = self.n_bs if transfer.waiting_fc else self.n_cr
            if now - transfer.last_event > budget:
                raise ValueError("N_Bs timeout" if transfer.waiting_fc else "N_Cr timeout")
        key, kind = (link, direction), raw[0] >> 4
        if kind == 3:
            other = (link, "rx" if direction == "tx" else "tx")
            transfer = self.active.get(other)
            if transfer is None or not transfer.waiting_fc:
                raise ValueError("unexpected FC")
            status = raw[0] & 15
            if status == 2:
                del self.active[other]
                raise ValueError("receiver overflow")
            if status == 1:
                transfer.waits += 1
                if transfer.waits > self.max_wait:
                    raise ValueError("WAIT budget exceeded")
                transfer.last_event = now
                return None
            if status != 0:
                raise ValueError("reserved flow status")
            transfer.separation = stmin_us(raw[2])
            transfer.block_size, transfer.block_used = raw[1], 0
            transfer.waiting_fc, transfer.last_event, transfer.waits = False, now, 0
            return None
        if kind in (0, 1) and key in self.active:
            raise ValueError("new message interrupts unfinished transfer")
        if kind == 0:
            length = raw[0] & 15
            if not 1 <= length <= 7:
                raise ValueError("invalid SF length")
            return raw[1:1+length]
        if kind == 1:
            length = ((raw[0] & 15) << 8) | raw[1]
            if not 8 <= length <= 4095:
                raise ValueError("unsupported FF length")
            self.active[key] = Transfer(length, bytearray(raw[2:]), last_event=now)
            return None
        if kind != 2 or key not in self.active:
            raise ValueError("orphan/unknown frame")
        transfer = self.active[key]
        if transfer.waiting_fc:
            raise ValueError("CF before CTS or beyond block size")
        if raw[0] & 15 != transfer.sn:
            raise ValueError("CF sequence mismatch")
        if transfer.last_cf_end is not None and now - transfer.last_cf_end < transfer.separation:
            raise ValueError("CF EOF gap smaller than STmin")
        transfer.data.extend(raw[1:1+min(7, transfer.length-len(transfer.data))])
        transfer.sn = (transfer.sn + 1) & 15
        transfer.last_cf_end = transfer.last_event = now
        transfer.block_used += 1
        if len(transfer.data) == transfer.length:
            del self.active[key]
            return bytes(transfer.data)
        if transfer.block_size and transfer.block_used == transfer.block_size:
            transfer.waiting_fc = True
        return None

    def finish(self):
        if self.active:
            raise ValueError("incomplete capture or unfinished transfer")


def synthetic_trace(payload: bytes, link="can0/11/7e0/7e8", start=0, bs=2) -> list[dict]:
    if not 1 <= len(payload) <= 4095:
        raise ValueError("fixture size out of range")
    events = []
    def add(t, direction, raw):
        events.append(dict(t_us=t, link=link, direction=direction,
                           data=raw.ljust(8, b"\x55").hex(" ")))
    if len(payload) <= 7:
        add(start, "rx", bytes([len(payload)]) + payload)
        return events
    add(start, "rx", bytes([0x10 | len(payload) >> 8, len(payload) & 255]) + payload[:6])
    clock = start + 1000
    add(clock, "tx", bytes([0x30, bs, 5]))
    count, sn = 0, 1
    for pos in range(6, len(payload), 7):
        clock += 6000  # Synthetic gap, NOT measured bus timing.
        add(clock, "rx", bytes([0x20 | sn]) + payload[pos:pos+7])
        sn = (sn + 1) & 15
        count += 1
        if bs and count % bs == 0 and pos + 7 < len(payload):
            clock += 1000
            add(clock, "tx", bytes([0x30, bs, 5]))
    return events


@dataclass
class PendingRequest:
    sid: int
    started_ms: int = 0
    p2_ms: int = 80
    p2_star_ms: int = 5000
    overall_ms: int = 6000
    deadline_ms: int = field(init=False)
    last_ms: int = field(init=False)
    done: bool = False

    def __post_init__(self):
        self.deadline_ms = self.started_ms + min(self.p2_ms, self.overall_ms)
        self.last_ms = self.started_ms

    def receive(self, now_ms: int, pdu: bytes) -> str:
        if self.done or now_ms < self.last_ms:
            raise ValueError("completed request or reversed clock")
        self.last_ms = now_ms
        if now_ms > self.deadline_ms:
            raise TimeoutError("request deadline exceeded")
        if len(pdu) == 3 and pdu[0] == 0x7F:
            if pdu[1] != self.sid:
                raise ValueError("negative response SID mismatch")
            if pdu[2] == 0x78:
                self.deadline_ms = min(now_ms + self.p2_star_ms,
                                       self.started_ms + self.overall_ms)
                return "pending"
            self.done = True
            return f"NRC {pdu[2]:02X}"
        if not pdu or pdu[0] != self.sid + 0x40:
            raise ValueError("positive response SID mismatch")
        # Caller still must check DID / subfunction / block-counter echo.
        self.done = True
        return "positive SID; service-specific validation still required"


class DownloadModel:
    """In-memory toy receiver: retries are accepted only for the LAST block.

    No flash writes, seed/key implementation, real transport or OEM routine.
    """
    def __init__(self, size=4096, max_block=258):
        self.size, self.max_block = size, max_block
        self.data, self.expected, self.previous = bytearray(), 1, None

    def block(self, request: bytes) -> bytes:
        if len(request) < 3 or request[0] != 0x36 or len(request) > self.max_block:
            raise ValueError("TransferData size or SID")
        counter = request[1]
        if self.previous and counter == self.previous[1]:
            if request != self.previous:
                raise ValueError("same counter with changed payload")
            return bytes([0x76, counter])
        if counter != self.expected:
            raise ValueError("wrong block counter")
        if len(self.data) + len(request) - 2 > self.size:
            raise ValueError("declared memory size exceeded")
        self.data.extend(request[2:])
        self.previous = request
        self.expected = (self.expected + 1) & 255
        return bytes([0x76, counter])

    def exit(self) -> bytes:
        if len(self.data) != self.size:
            raise ValueError("download not complete")
        return b"\x77"


class Tests(unittest.TestCase):
    def test_values(self):
        self.assertEqual(obd_value(bytes.fromhex("41 0c 1a f8"), 12), (1726, "rpm"))
        self.assertEqual(obd_value(bytes.fromhex("42 0c 00 1a f8"), 12, 0), (1726, "rpm"))
        self.assertEqual(obd_value(bytes.fromhex("41 06 80"), 6), (0, "%"))
        self.assertEqual(obd_value(bytes.fromhex("41 06 ff"), 6), (99.21875, "%"))
        self.assertEqual(obd_value(bytes.fromhex("41 05 00"), 5), (-40, "degC"))
        for bad in ("41 0d 1a f8", "41 0c 1a", "42 0c 00 1a f8"):
            with self.assertRaises(ValueError): obd_value(bytes.fromhex(bad), 12)

    def test_bitmap_dtc_dids(self):
        self.assertEqual(supported_pids(0, bytes.fromhex("00 10 00 01")), [12, 32])
        self.assertEqual(supported_pids(32, bytes.fromhex("80 00 00 00")), [33])
        self.assertEqual(obd_dtcs(bytes.fromhex("43 01 33 c1 23 00 00")), ["P0133", "U0123"])
        pdu = bytes.fromhex("62 f1 90") + b"LTEST123456789012" + bytes.fromhex("f1 8c f1 90 00 01")
        result = decode_dids(pdu, {0xF190: 17, 0xF18C: 4})
        self.assertEqual(result[0xF18C], bytes.fromhex("f1 90 00 01"))
        with self.assertRaises(ValueError): decode_dids(pdu, {0xF190: 17})
        with self.assertRaises(ValueError): decode_dids(pdu[:-1], {0xF190: 17, 0xF18C: 4})

    def test_all_lengths_and_mux(self):
        for length in (1, 7, 8, 20, 30, 112, 125, 258, 4095):
            payload = bytes(i % 256 for i in range(length))
            checker = TraceChecker()
            result = [p for e in synthetic_trace(payload) if (p := checker.feed(e)) is not None]
            checker.finish()
            self.assertEqual(result, [payload])
        events = synthetic_trace(b"A" * 30, "ecu-A") + synthetic_trace(b"B" * 20, "ecu-B", 100)
        checker = TraceChecker()
        outputs = [p for e in sorted(events, key=lambda e:e["t_us"]) if (p:=checker.feed(e)) is not None]
        checker.finish()
        self.assertCountEqual(outputs, [b"A"*30, b"B"*20])

    def test_article_timelines_and_counter_domains(self):
        # ECU A is a 20-byte PDU: FF at 0 ms, physical FC at 1 ms,
        # then CF1/CF2 at 7/13 ms. ECU B's SF at 8 ms is independent.
        a_payload = bytes(range(20))
        a = synthetic_trace(a_payload, "can0/11/7e0/7e8", start=0, bs=0)
        b = synthetic_trace(bytes.fromhex("41 0c 1a f8"),
                            "can0/11/7e1/7e9", start=8000)
        self.assertEqual(
            [(e["t_us"], e["direction"], int(e["data"][:2], 16) >> 4)
             for e in a],
            [(0, "rx", 1), (1000, "tx", 3),
             (7000, "rx", 2), (13000, "rx", 2)],
        )
        checker = TraceChecker()
        outputs = []
        for event in sorted(a + b, key=lambda e: e["t_us"]):
            pdu = checker.feed(event)
            if pdu is not None:
                outputs.append((event["link"], pdu))
        checker.finish()
        self.assertCountEqual(
            outputs,
            [("can0/11/7e0/7e8", a_payload),
             ("can0/11/7e1/7e9", bytes.fromhex("41 0c 1a f8"))],
        )

        # A 30-byte PDU with BS=2 has 6 bytes in FF and 7+7+7+3 in
        # four CFs. Two FCs are present; the CF sequence is 1,2,3,4.
        thirty = synthetic_trace(bytes(range(30)), bs=2)
        kinds = [int(e["data"][:2], 16) >> 4 for e in thirty]
        self.assertEqual(kinds, [1, 3, 2, 2, 3, 2, 2])
        self.assertEqual(
            [int(e["data"][:2], 16) & 15 for e in thirty if int(e["data"][:2], 16) >> 4 == 2],
            [1, 2, 3, 4],
        )

        # maxNumberOfBlockLength=0x0102 means 258 request bytes here:
        # SID + BSC + 256 data. That UDS PDU needs FF(6) + 36 CF(7).
        max_block_length, data_per_block, image_size = 0x0102, 256, 4096
        self.assertEqual(max_block_length, 2 + data_per_block)
        self.assertEqual(image_size // data_per_block, 16)
        self.assertEqual((max_block_length - 6 + 6) // 7, 36)
        cf_sn = [index & 15 for index in range(1, 37)]
        self.assertEqual(cf_sn, list(range(1, 16)) + [0] + list(range(1, 16)) + [0, 1, 2, 3, 4])

    def test_transport_errors(self):
        good = synthetic_trace(bytes(range(30)))
        mutations = {}
        def copy(): return [dict(e) for e in good]
        x=copy(); x[2]["data"]="22 " + x[2]["data"][3:]; mutations["sequence"]=x
        x=copy(); del x[1]; mutations["missing initial FC"]=x
        x=copy(); del x[4]; mutations["missing block FC"]=x
        x=copy(); x[3]["t_us"]=x[2]["t_us"]+1; mutations["gap"]=x
        x=copy(); x[1]["data"]="30 02 80 55 55 55 55 55"; mutations["reserved STmin"]=x
        x=copy(); x[1]["data"]="32 00 00 55 55 55 55 55"; mutations["overflow"]=x
        x=copy(); x[2]["t_us"]+=2_000_000; mutations["N_Cr"]=x
        x=copy(); x[1]["t_us"]+=2_000_000; mutations["N_Bs"]=x
        x=copy(); x.pop(); mutations["truncated"]=x
        for name, events in mutations.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                checker = TraceChecker()
                for e in events: checker.feed(e)
                checker.finish()
        self.assertEqual(stmin_us(0xF3), 300)

    def test_pending(self):
        request = PendingRequest(0x31)
        self.assertEqual(request.receive(40, bytes.fromhex("7f 31 78")), "pending")
        self.assertEqual(request.deadline_ms, 5040)
        request.receive(4040, bytes.fromhex("7f 31 78"))
        self.assertEqual(request.deadline_ms, 6000)
        with self.assertRaises(TimeoutError): request.receive(7000, bytes.fromhex("71 01 f0 01"))
        with self.assertRaises(ValueError): PendingRequest(0x31).receive(30, bytes.fromhex("7f 22 78"))

    def test_blocks(self):
        model = DownloadModel()
        for counter in range(1, 17):
            block = bytes([0x36, counter]) + bytes([counter])*256
            self.assertEqual(model.block(block), bytes([0x76, counter]))
            self.assertEqual(model.block(block), bytes([0x76, counter]))
        self.assertEqual(len(model.data), 4096)
        self.assertEqual(model.exit(), b"\x77")
        with self.assertRaises(ValueError): model.block(b"\x36\x10"+b"X"*256)
        with self.assertRaises(ValueError): DownloadModel().block(b"\x36\x01"+b"X"*257)
        with self.assertRaises(ValueError): DownloadModel().block(b"\x36\x02X")
        with self.assertRaises(ValueError): DownloadModel().exit()
        rollover = DownloadModel(size=256, max_block=3)
        for i in range(1, 257): rollover.block(bytes([0x36, i & 255, 0]))
        self.assertEqual(rollover.exit(), b"\x77")


def demo():
    print("PID bitmap BE3EB813:", " ".join(f"{p:02X}" for p in supported_pids(0, bytes.fromhex("be3eb813"))))
    print("RPM:", *obd_value(bytes.fromhex("41 0c 1a f8"), 0x0C))
    print("DTC:", ", ".join(obd_dtcs(bytes.fromhex("43 01 33 c1 23 00 00"))))
    pdu = bytes.fromhex("49 02 01") + b"LTEST123456789012"
    checker = TraceChecker()
    for event in synthetic_trace(pdu):
        result = checker.feed(event)
        if result is not None: print("OBD VIN:", result.hex(" "), "->", result[3:].decode())
    checker.finish()
    request = PendingRequest(0x31)
    for now in (40, 4040):
        request.receive(now, bytes.fromhex("7f 31 78"))
        print(f"pending at {now} ms -> deadline {request.deadline_ms} ms")
    model = DownloadModel()
    for i in range(1, 17):
        block = bytes([0x36, i])+bytes([i])*256
        model.block(block); model.block(block)
    print(f"download: {len(model.data)} bytes, 16 blocks, 16 duplicate acknowledgements, exit={model.exit().hex()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--trace", type=Path, help="read synthetic/compatible JSONL offline")
    parser.add_argument("--write-fixture", type=Path)
    args = parser.parse_args()
    if args.test:
        unittest.main(argv=[__file__], verbosity=2)
    elif args.write_fixture:
        events = synthetic_trace(b"\x49\x02\x01LTEST123456789012", "can0/11/7e0/7e8")
        events += synthetic_trace(b"\x41\x0c\x1a\xf8", "can0/11/7e1/7e9", 8000)
        args.write_fixture.write_text("\n".join(json.dumps(e) for e in sorted(events,key=lambda e:e["t_us"]))+"\n", encoding="utf8")
    elif args.trace:
        checker=TraceChecker()
        for line in args.trace.read_text(encoding="utf8").splitlines():
            event=json.loads(line); pdu=checker.feed(event)
            if pdu is not None: print(event["t_us"], event["link"], pdu.hex(" "))
        checker.finish()
    else:
        demo()
