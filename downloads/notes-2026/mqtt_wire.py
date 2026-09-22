"""MQTT 3.1.1 loopback lab. Python 3 standard library only.
Start an isolated Mosquitto broker on 127.0.0.1:18884 first.
The lab publishes and clears retained data only under lab/temp.
"""
import socket

def string(value):
    value = value.encode('utf-8')
    return len(value).to_bytes(2, 'big') + value

def packet(first, body):
    length = len(body)
    encoded = bytearray()
    while True:
        digit = length % 128
        length //= 128
        encoded.append(digit | (128 if length else 0))
        if not length:
            return bytes([first]) + encoded + body

def read_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        block = sock.recv(size - len(data))
        if not block:
            raise EOFError('broker closed the connection')
        data.extend(block)
    return bytes(data)

def receive(sock):
    first = read_exact(sock, 1)[0]
    size, multiplier = 0, 1
    for _ in range(4):
        digit = read_exact(sock, 1)[0]
        size += (digit & 127) * multiplier
        if not digit & 128:
            return first, read_exact(sock, size)
        multiplier *= 128
    raise ValueError('invalid Remaining Length')

def connect(client_id, clean=True):
    sock = socket.create_connection(('127.0.0.1', 18884), timeout=2)
    body = string('MQTT') + bytes([4, 2 if clean else 0, 0, 30]) + string(client_id)
    sock.sendall(packet(0x10, body))
    first, ack = receive(sock)
    assert first == 0x20 and len(ack) == 2 and ack[1] == 0, (first, ack)
    return sock, ack[0] & 1

def publish(sock, payload, retain=False, mid=1):
    wire = packet(0x32 | int(retain), string('lab/temp') + mid.to_bytes(2, 'big') + payload)
    sock.sendall(wire)
    assert receive(sock) == (0x40, mid.to_bytes(2, 'big'))
    return wire

def subscribe(sock):
    sock.sendall(packet(0x82, b'\x00\x01' + string('lab/temp') + b'\x01'))
    first, body = receive(sock)
    assert (first, body) == (0x90, b'\x00\x01\x01'), (first, body)

def message(sock):
    first, body = receive(sock)
    assert first >> 4 == 3
    length = int.from_bytes(body[:2], 'big')
    topic = body[2:2+length].decode()
    offset = 2 + length
    if (first >> 1) & 3 == 1:
        sock.sendall(packet(0x40, body[offset:offset+2]))
        offset += 2
    return topic, body[offset:], bool(first & 1)

def close(sock):
    sock.sendall(b'\xe0\x00')
    sock.close()

if __name__ == '__main__':
    publisher, _ = connect('notes-publisher')
    try:
        wire = publish(publisher, b'23.5', retain=True)
        print('retained PUBLISH:', wire.hex(' '))
        subscriber, _ = connect('notes-retained')
        subscribe(subscriber)
        item = message(subscriber)
        assert item == ('lab/temp', b'23.5', True), item
        print('new subscriber:', item)
        close(subscriber)
        publish(publisher, b'', retain=True, mid=2)

        # Create a persistent subscription before taking it offline.
        persistent, _ = connect('notes-session', clean=False)
        subscribe(persistent)
        close(persistent)
        publish(publisher, b'offline', mid=3)
        persistent, present = connect('notes-session', clean=False)
        item = message(persistent)
        assert present == 1 and item == ('lab/temp', b'offline', False), (present, item)
        print('resumed session:', present, item)
        close(persistent)
        reset, _ = connect('notes-session', clean=True)
        close(reset)
    finally:
        close(publisher)
