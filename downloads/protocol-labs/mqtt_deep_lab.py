#!/usr/bin/env python3
"""Raw MQTT 3.1.1 loopback lab (Python standard library only).

The broker must listen on 127.0.0.1:18891.  The script uses only topics under
lab/deep/ and fixed client IDs beginning with mqtt-depth-.  Run it against an
isolated broker; it deliberately opens an idle connection to trigger a Will.
"""

from __future__ import annotations

import argparse
import socket
import time
from dataclasses import dataclass


def mqtt_utf8(value: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > 65_535 or "\x00" in value:
        raise ValueError("invalid MQTT UTF-8 field for this lab")
    return len(encoded).to_bytes(2, "big") + encoded


def encode_remaining_length(value: int) -> bytes:
    if not 0 <= value <= 268_435_455:
        raise ValueError("Remaining Length out of range")
    out = bytearray()
    while True:
        digit = value % 128
        value //= 128
        out.append(digit | (0x80 if value else 0))
        if not value:
            return bytes(out)


def packet(first_byte: int, body: bytes = b"") -> bytes:
    return bytes([first_byte]) + encode_remaining_length(len(body)) + body


class StreamParser:
    """Incremental MQTT packet parser used by the synthetic framing checks."""

    def __init__(self, maximum_packet_size: int = 1024 * 1024):
        self.buffer = bytearray()
        self.maximum_packet_size = maximum_packet_size

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buffer.extend(chunk)
        result = []
        while True:
            if len(self.buffer) < 2:
                return result
            value = 0
            multiplier = 1
            end = None
            for index in range(1, min(5, len(self.buffer))):
                digit = self.buffer[index]
                value += (digit & 0x7F) * multiplier
                if not digit & 0x80:
                    end = index + 1
                    break
                multiplier *= 128
            if end is None:
                if len(self.buffer) >= 5:
                    raise ValueError("malformed Remaining Length")
                return result
            total = end + value
            if total > self.maximum_packet_size:
                raise ValueError("packet exceeds local size limit")
            if len(self.buffer) < total:
                return result
            result.append(bytes(self.buffer[:total]))
            del self.buffer[:total]


def read_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        block = sock.recv(size - len(data))
        if not block:
            raise EOFError("broker closed the connection")
        data.extend(block)
    return bytes(data)


def receive_packet(sock: socket.socket) -> tuple[int, bytes, bytes]:
    first = read_exact(sock, 1)[0]
    encoded = bytearray()
    value = 0
    multiplier = 1
    for _ in range(4):
        digit = read_exact(sock, 1)[0]
        encoded.append(digit)
        value += (digit & 0x7F) * multiplier
        if not digit & 0x80:
            body = read_exact(sock, value)
            return first, body, bytes([first]) + encoded + body
        multiplier *= 128
    raise ValueError("malformed Remaining Length")


def connect_client(
    host: str,
    port: int,
    client_id: str,
    *,
    clean: bool = True,
    keepalive: int = 30,
    will: tuple[str, bytes, int, bool] | None = None,
) -> tuple[socket.socket, int]:
    flags = 0x02 if clean else 0
    payload = bytearray(mqtt_utf8(client_id))
    if will is not None:
        topic, message, qos, retain = will
        if qos not in (0, 1, 2):
            raise ValueError("invalid Will QoS")
        flags |= 0x04 | (qos << 3) | (0x20 if retain else 0)
        payload += mqtt_utf8(topic)
        payload += len(message).to_bytes(2, "big") + message
    variable = mqtt_utf8("MQTT") + bytes([4, flags]) + keepalive.to_bytes(2, "big")
    sock = socket.create_connection((host, port), timeout=3)
    sock.settimeout(3)
    sock.sendall(packet(0x10, variable + payload))
    first, body, _ = receive_packet(sock)
    if first != 0x20 or len(body) != 2 or body[1] != 0:
        raise AssertionError((first, body))
    return sock, body[0] & 1


def disconnect(sock: socket.socket) -> None:
    try:
        sock.sendall(b"\xe0\x00")
    finally:
        sock.close()


def subscribe(sock: socket.socket, topic_filter: str, qos: int, packet_id: int) -> int:
    body = packet_id.to_bytes(2, "big") + mqtt_utf8(topic_filter) + bytes([qos])
    sock.sendall(packet(0x82, body))
    first, reply, _ = receive_packet(sock)
    if first != 0x90 or reply[:2] != packet_id.to_bytes(2, "big") or len(reply) != 3:
        raise AssertionError((first, reply))
    if reply[2] == 0x80:
        raise AssertionError("subscription rejected")
    return reply[2]


def publish_qos1(
    sock: socket.socket,
    topic: str,
    payload: bytes,
    packet_id: int,
    *,
    retain: bool = False,
) -> bytes:
    wire = packet(
        0x32 | int(retain),
        mqtt_utf8(topic) + packet_id.to_bytes(2, "big") + payload,
    )
    sock.sendall(wire)
    first, body, _ = receive_packet(sock)
    if (first, body) != (0x40, packet_id.to_bytes(2, "big")):
        raise AssertionError((first, body))
    return wire


def publish_qos2(
    sock: socket.socket,
    topic: str,
    payload: bytes,
    packet_id: int,
    *,
    repeat_publish: bool,
) -> tuple[bytes, bytes | None]:
    body = mqtt_utf8(topic) + packet_id.to_bytes(2, "big") + payload
    first_wire = packet(0x34, body)
    duplicate_wire = packet(0x3C, body) if repeat_publish else None
    sock.sendall(first_wire)
    first, reply, _ = receive_packet(sock)
    if (first, reply) != (0x50, packet_id.to_bytes(2, "big")):
        raise AssertionError((first, reply))
    if duplicate_wire is not None:
        sock.sendall(duplicate_wire)
        first, reply, _ = receive_packet(sock)
        if (first, reply) != (0x50, packet_id.to_bytes(2, "big")):
            raise AssertionError((first, reply))
    sock.sendall(packet(0x62, packet_id.to_bytes(2, "big")))
    first, reply, _ = receive_packet(sock)
    if (first, reply) != (0x70, packet_id.to_bytes(2, "big")):
        raise AssertionError((first, reply))
    return first_wire, duplicate_wire


@dataclass
class PublishedMessage:
    topic: str
    payload: bytes
    qos: int
    duplicate: bool
    retain: bool
    packet_id: int | None
    first_byte: int


def receive_publish(sock: socket.socket) -> PublishedMessage:
    first, body, _ = receive_packet(sock)
    if first >> 4 != 3:
        raise AssertionError((first, body))
    qos = (first >> 1) & 3
    topic_length = int.from_bytes(body[:2], "big")
    topic_end = 2 + topic_length
    topic = body[2:topic_end].decode("utf-8")
    packet_id = None
    payload_at = topic_end
    if qos:
        packet_id = int.from_bytes(body[payload_at : payload_at + 2], "big")
        payload_at += 2
    message = PublishedMessage(
        topic=topic,
        payload=body[payload_at:],
        qos=qos,
        duplicate=bool(first & 0x08),
        retain=bool(first & 0x01),
        packet_id=packet_id,
        first_byte=first,
    )
    if qos == 1:
        sock.sendall(packet(0x40, packet_id.to_bytes(2, "big")))
    elif qos == 2:
        sock.sendall(packet(0x50, packet_id.to_bytes(2, "big")))
        rel_first, rel_body, _ = receive_packet(sock)
        if rel_first != 0x62 or rel_body != packet_id.to_bytes(2, "big"):
            raise AssertionError((rel_first, rel_body))
        sock.sendall(packet(0x70, packet_id.to_bytes(2, "big")))
    return message


def expect_no_packet(sock: socket.socket, timeout: float) -> None:
    previous = sock.gettimeout()
    sock.settimeout(timeout)
    try:
        try:
            receive_packet(sock)
        except socket.timeout:
            return
        raise AssertionError("unexpected second packet")
    finally:
        sock.settimeout(previous)


def framing_checks() -> None:
    assert encode_remaining_length(127) == b"\x7f"
    assert encode_remaining_length(128) == b"\x80\x01"
    assert encode_remaining_length(16_383) == b"\xff\x7f"
    assert encode_remaining_length(16_384) == b"\x80\x80\x01"

    large_publish = packet(0x30, b"x" * 128)
    parser = StreamParser()
    assert parser.feed(large_publish[:2]) == []
    assert parser.feed(large_publish[2:17]) == []
    assert parser.feed(large_publish[17:]) == [large_publish]

    parser = StreamParser()
    combined = b"\xc0\x00\xe0\x00"
    assert parser.feed(combined) == [b"\xc0\x00", b"\xe0\x00"]
    print("framing: RL(128)=80 01; split=2/15/114 bytes -> 1 packet; coalesced -> 2 packets")

    # Synthetic MQTT 5 PUBLISH used in the article.  It contains Message
    # Expiry Interval=60 and Content Type="text".  This validates the byte
    # counts only; the network tests below deliberately negotiate MQTT 3.1.1.
    mqtt5_publish = bytes.fromhex(
        "32 19 00 06 6c 61 62 2f 76 35 00 10 0c "
        "02 00 00 00 3c 03 00 04 74 65 78 74 6f 6b"
    )
    assert len(mqtt5_publish) == 27 and mqtt5_publish[1] == len(mqtt5_publish) - 2
    assert mqtt5_publish[12] == 0x0C
    print("mqtt5 fixture: remaining=25 properties=12 total=27 bytes")


def broker_checks(host: str, port: int) -> None:
    publisher, _ = connect_client(host, port, "mqtt-depth-publisher")
    established, _ = connect_client(host, port, "mqtt-depth-established")
    retained_sub = None
    persistent = None
    qos2_sub = None
    will_observer = None
    idle = None
    try:
        # RETAIN belongs to a sending hop.  An established subscription sees
        # RETAIN=0; a subscription created later receives the stored copy with 1.
        subscribe(established, "lab/deep/retain", 1, 1)
        retained_wire = publish_qos1(
            publisher, "lab/deep/retain", b"23.5", 1, retain=True
        )
        live = receive_publish(established)
        retained_sub, _ = connect_client(host, port, "mqtt-depth-retained")
        subscribe(retained_sub, "lab/deep/retain", 1, 1)
        stored = receive_publish(retained_sub)
        assert (live.payload, live.retain) == (b"23.5", False)
        assert (stored.payload, stored.retain) == (b"23.5", True)
        print("retained wire:", retained_wire.hex(" "))
        print("retain scope: established=0 new-subscription=1")
        publish_qos1(publisher, "lab/deep/retain", b"", 2, retain=True)

        # MQTT 3.1.1 persistent session: subscription and queued QoS 1 message.
        persistent, present = connect_client(
            host, port, "mqtt-depth-session", clean=False
        )
        assert present == 0
        subscribe(persistent, "lab/deep/offline", 1, 1)
        disconnect(persistent)
        persistent = None
        publish_qos1(publisher, "lab/deep/offline", b"queued", 3)
        persistent, present = connect_client(
            host, port, "mqtt-depth-session", clean=False
        )
        queued = receive_publish(persistent)
        assert present == 1 and queued.payload == b"queued"
        print(f"session restore: present={present} payload={queued.payload.decode()}")
        disconnect(persistent)
        persistent = None
        reset, _ = connect_client(host, port, "mqtt-depth-session", clean=True)
        disconnect(reset)

        # Duplicate QoS 2 PUBLISH before PUBREL is one protocol transaction.
        qos2_sub, _ = connect_client(host, port, "mqtt-depth-qos2-sub")
        assert subscribe(qos2_sub, "lab/deep/qos2", 2, 1) == 2
        first_wire, duplicate_wire = publish_qos2(
            publisher,
            "lab/deep/qos2",
            b"one-transaction",
            0x1234,
            repeat_publish=True,
        )
        one = receive_publish(qos2_sub)
        assert one.payload == b"one-transaction" and one.qos == 2
        expect_no_packet(qos2_sub, 0.5)
        print(
            "qos2 duplicate: publish=%02x duplicate=%02x subscriber-deliveries=1"
            % (first_wire[0], duplicate_wire[0])
        )

        # Once PUBCOMP completes the exchange, the same Packet Identifier can
        # identify a new transaction.
        publish_qos2(
            publisher,
            "lab/deep/qos2",
            b"after-reuse",
            0x1234,
            repeat_publish=False,
        )
        two = receive_publish(qos2_sub)
        assert two.payload == b"after-reuse"
        print("packet-id reuse after PUBCOMP: payload=after-reuse")

        # Keep Alive failure: leave the connection silent and observe its Will.
        will_observer, _ = connect_client(host, port, "mqtt-depth-will-observer")
        subscribe(will_observer, "lab/deep/will", 1, 1)
        will_observer.settimeout(8)
        started = time.monotonic()
        idle, _ = connect_client(
            host,
            port,
            "mqtt-depth-idle",
            keepalive=2,
            will=("lab/deep/will", b"timeout", 1, False),
        )
        will_message = receive_publish(will_observer)
        elapsed = time.monotonic() - started
        assert will_message.payload == b"timeout"
        print(f"keepalive Will: payload=timeout elapsed={elapsed:.3f}s")
        idle.close()
        idle = None

        # A graceful DISCONNECT suppresses the Will.
        graceful, _ = connect_client(
            host,
            port,
            "mqtt-depth-graceful",
            keepalive=2,
            will=("lab/deep/will", b"must-not-fire", 1, False),
        )
        disconnect(graceful)
        expect_no_packet(will_observer, 2.0)
        print("graceful DISCONNECT: Will packets=0 during 2.0s observation")
    finally:
        for sock in (retained_sub, established, persistent, qos2_sub, will_observer):
            if sock is not None:
                try:
                    disconnect(sock)
                except (OSError, EOFError):
                    sock.close()
        if idle is not None:
            idle.close()
        try:
            disconnect(publisher)
        except (OSError, EOFError):
            publisher.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18891)
    args = parser.parse_args()
    framing_checks()
    broker_checks(args.host, args.port)
    print("all checks passed")


if __name__ == "__main__":
    main()
