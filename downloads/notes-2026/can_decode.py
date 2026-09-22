"""Offline Classic CAN / normal-addressing examples for the blog.
Accept one isolated ISO-TP response stream, not a mixed candump capture.
This is an educational reassembler, not an ISO-TP implementation.
"""
def reassemble(frames):
    if not frames or any(not 1 <= len(frame) <= 8 for frame in frames):
        raise ValueError('expected Classical CAN data fields of 1..8 bytes')
    first = frames[0]
    kind = first[0] >> 4
    if kind == 0:
        length = first[0] & 15
        if not 1 <= length <= 7 or length > len(first)-1 or len(frames) != 1:
            raise ValueError('invalid single frame')
        return first[1:1+length]
    if kind != 1 or len(first) != 8:
        raise ValueError('expected a normal first frame')
    length = ((first[0] & 15) << 8) | first[1]
    if length <= 7:
        raise ValueError('unsupported first-frame length')
    result = bytearray(first[2:])
    sequence = 1
    for frame in frames[1:]:
        if len(result) >= length:
            raise ValueError('extra frame after complete payload')
        if frame[0] >> 4 != 2 or frame[0] & 15 != sequence:
            raise ValueError('consecutive-frame sequence mismatch')
        if len(frame) < 2 or (length-len(result) > 7 and len(frame) != 8):
            raise ValueError('short consecutive frame')
        result.extend(frame[1:])
        sequence = (sequence + 1) & 15
    if len(result) < length:
        raise ValueError('incomplete payload')
    return bytes(result[:length])

def rpm(payload):
    if len(payload) != 4 or payload[:2] != bytes.fromhex('41 0C'):
        raise ValueError('expected Mode 01 PID 0C response')
    return int.from_bytes(payload[2:], 'big') / 4

if __name__ == '__main__':
    pdu = reassemble([bytes.fromhex('04 41 0C 1A F8 00 00 00')])
    assert rpm(pdu) == 1726.0
    print(f'OBD: {pdu.hex(" ")} -> {rpm(pdu):.0f} rpm')
    vin_frames = [bytes.fromhex(s) for s in [
        '10 14 62 F1 90 4C 54 45',
        '21 53 54 31 32 33 34 35',
        '22 36 37 38 39 30 31 32']]
    pdu = reassemble(vin_frames)
    assert pdu == bytes.fromhex('62 F1 90') + b'LTEST123456789012'
    print(f'UDS: length={len(pdu)}, DID={pdu[1:3].hex()}, data={pdu[3:].decode()}')
    for label, frames in [
        ('truncated', vin_frames[:-1]),
        ('wrong sequence', [vin_frames[0], bytes.fromhex('22 53 54 31 32 33 34 35')]),
        ('invalid SF length', [bytes.fromhex('08 7F 37 24 55 55 55 55')])]:
        try:
            reassemble(frames)
        except ValueError as exc:
            print(f'{label}: rejected ({exc})')
        else:
            raise AssertionError(label)
