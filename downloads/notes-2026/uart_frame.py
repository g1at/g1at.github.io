"""Decode synthetic UART bit cells. No serial hardware is required."""
def decode_8n1(cells):
    if len(cells) != 10 or any(bit not in (0, 1) for bit in cells):
        raise ValueError('expected ten binary cells')
    if cells[0] != 0:
        raise ValueError('invalid start bit')
    if cells[9] != 1:
        raise ValueError('framing error: stop bit is low')
    return sum(bit << i for i, bit in enumerate(cells[1:9]))

if __name__ == '__main__':
    cells = [0, 1, 1, 0, 0, 1, 0, 1, 0, 1]
    value = decode_8n1(cells)
    assert value == 0x53
    print(f'8N1 cells: {cells}')
    print(f'decoded: 0x{value:02X} ({chr(value)})')
    print(f'115200 baud: {1e6/115200:.4f} us/bit, {115200/10:.0f} bytes/s')
    try:
        decode_8n1(cells[:-1] + [0])
    except ValueError as exc:
        print(exc)
    else:
        raise AssertionError('invalid stop bit was accepted')
