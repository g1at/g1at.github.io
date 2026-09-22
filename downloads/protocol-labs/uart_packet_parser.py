#!/usr/bin/env python3
"""Bounded incremental UART byte-stream framing demo (standard library only).

Wire format after unescaping:
  version:u8 | type:u8 | payload_len:u16be | sequence:u16be | payload | crc:u16be

0x7e starts a frame. Within a frame, 0x7e and 0x7d are escaped as
0x7d followed by (byte XOR 0x20). The CRC covers header and payload.
"""

from __future__ import annotations

from dataclasses import dataclass


SOF = 0x7E
ESC = 0x7D
ESC_XOR = 0x20
HEADER_LEN = 6
CRC_LEN = 2
MAX_PAYLOAD = 256
MAX_BODY = HEADER_LEN + MAX_PAYLOAD + CRC_LEN


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly=0x1021 init=0xffff refin=false refout=false xorout=0."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def escape(data: bytes) -> bytes:
    out = bytearray()
    for byte in data:
        if byte in (SOF, ESC):
            out.extend((ESC, byte ^ ESC_XOR))
        else:
            out.append(byte)
    return bytes(out)


def encode(message_type: int, sequence: int, payload: bytes, version: int = 1) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload exceeds MAX_PAYLOAD")
    if not 0 <= message_type <= 0xFF or not 0 <= sequence <= 0xFFFF:
        raise ValueError("field outside wire width")
    header = bytes((version, message_type)) + len(payload).to_bytes(2, "big") + sequence.to_bytes(2, "big")
    body = header + payload
    body += crc16_ccitt_false(body).to_bytes(2, "big")
    return bytes((SOF,)) + escape(body)


@dataclass(frozen=True)
class Frame:
    version: int
    message_type: int
    sequence: int
    payload: bytes


class Parser:
    def __init__(self) -> None:
        self.body = bytearray()
        self.in_frame = False
        self.escaped = False
        self.expected: int | None = None
        self.frames = 0
        self.crc_errors = 0
        self.length_errors = 0
        self.restarts = 0

    def reset(self) -> None:
        self.body.clear()
        self.in_frame = False
        self.escaped = False
        self.expected = None

    def _start(self) -> None:
        if self.in_frame and self.body:
            self.restarts += 1
        self.body.clear()
        self.in_frame = True
        self.escaped = False
        self.expected = None

    def feed(self, chunk: bytes) -> list[Frame]:
        accepted: list[Frame] = []
        for wire_byte in chunk:
            if wire_byte == SOF:
                self._start()
                continue
            if not self.in_frame:
                continue
            if self.escaped:
                byte = wire_byte ^ ESC_XOR
                self.escaped = False
            elif wire_byte == ESC:
                self.escaped = True
                continue
            else:
                byte = wire_byte

            self.body.append(byte)
            if len(self.body) == 4:
                payload_len = int.from_bytes(self.body[2:4], "big")
                if payload_len > MAX_PAYLOAD:
                    self.length_errors += 1
                    self.reset()
                    continue
                self.expected = HEADER_LEN + payload_len + CRC_LEN

            if len(self.body) > MAX_BODY:
                self.length_errors += 1
                self.reset()
                continue

            if self.expected is not None and len(self.body) == self.expected:
                content, received_crc = self.body[:-2], int.from_bytes(self.body[-2:], "big")
                calculated_crc = crc16_ccitt_false(content)
                if calculated_crc == received_crc:
                    accepted.append(
                        Frame(
                            version=content[0],
                            message_type=content[1],
                            sequence=int.from_bytes(content[4:6], "big"),
                            payload=bytes(content[6:]),
                        )
                    )
                    self.frames += 1
                else:
                    self.crc_errors += 1
                self.reset()
        return accepted


def demonstrate() -> None:
    assert crc16_ccitt_false(b"123456789") == 0x29B1

    first = encode(0x10, 7, b"A~B}C")
    damaged = bytearray(encode(0x20, 8, b"bad crc"))
    damaged[-1] ^= 0x01
    oversized_header = bytes((SOF, 1, 0x30, 0x01, 0x01, 0, 9))
    truncated = bytes((SOF, 1, 0x40, 0, 5, 0, 9, ord("x")))
    second = encode(0x11, 10, b"recovered")
    wire = b"noise" + first + bytes(damaged) + oversized_header + truncated + second

    parser = Parser()
    accepted: list[Frame] = []
    sizes = (1, 2, 5, 3, 8, 13)
    offset = 0
    turn = 0
    while offset < len(wire):
        size = sizes[turn % len(sizes)]
        accepted.extend(parser.feed(wire[offset : offset + size]))
        offset += size
        turn += 1

    assert [frame.sequence for frame in accepted] == [7, 10]
    assert accepted[0].payload == b"A~B}C"
    assert parser.crc_errors == 1
    assert parser.length_errors == 1
    assert parser.restarts == 1

    print(f"CRC check vector: 123456789 -> 0x{crc16_ccitt_false(b'123456789'):04X}")
    print(f"escaped frame seq=7: {first.hex(' ')}")
    for frame in accepted:
        print(f"accepted: type=0x{frame.message_type:02X} seq={frame.sequence} payload={frame.payload!r}")
    print(
        "counters: "
        f"accepted={parser.frames} crc_errors={parser.crc_errors} "
        f"length_errors={parser.length_errors} restarts={parser.restarts}"
    )


if __name__ == "__main__":
    demonstrate()
