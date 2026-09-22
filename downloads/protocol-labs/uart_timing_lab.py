#!/usr/bin/env python3
"""Idealized UART timing lab using only the Python standard library.

The receiver model uses 16x oversampling and majority votes at ticks 7/8/9
of each bit cell. It intentionally omits analog slew, noise, jitter,
metastability, input filtering, and implementation-specific resynchronization.
Use the result to understand error accumulation, never as a chip limit.
"""

from __future__ import annotations

import argparse
import html
from dataclasses import dataclass
from pathlib import Path


OVERSAMPLE = 16


def make_8n1(byte: int) -> list[int]:
    if not 0 <= byte <= 0xFF:
        raise ValueError("byte must be in 0..255")
    return [0] + [(byte >> bit) & 1 for bit in range(8)] + [1]


def line_level(frame: list[int], time_in_tx_bits: float) -> int:
    if time_in_tx_bits < 0:
        return 1
    index = int(time_in_tx_bits)
    if index >= len(frame):
        return 1
    return frame[index]


@dataclass(frozen=True)
class DecodeResult:
    accepted: bool
    value: int
    voted_bits: tuple[int, ...]
    sample_times: tuple[float, ...]


def decode_16x(byte: int, rx_rate_error: float, detect_phase: float) -> DecodeResult:
    """Decode one ideal 8N1 frame.

    rx_rate_error is (B_rx - B_tx) / B_tx. A positive value makes receiver
    sample intervals shorter. detect_phase is the start-edge detection delay
    expressed as a fraction in [0, 1) of one receiver oversample tick.
    """
    if not -0.2 < rx_rate_error < 0.2:
        raise ValueError("rx_rate_error outside model range")
    if not 0.0 <= detect_phase < 1.0:
        raise ValueError("detect_phase must be in [0, 1)")

    frame = make_8n1(byte)
    rx_tick = 1.0 / (OVERSAMPLE * (1.0 + rx_rate_error))
    edge_detected_at = detect_phase * rx_tick
    voted: list[int] = []
    centers: list[float] = []

    for cell in range(len(frame)):
        center_tick = 8 + cell * OVERSAMPLE
        times = [edge_detected_at + (center_tick + offset) * rx_tick for offset in (-1, 0, 1)]
        samples = [line_level(frame, point) for point in times]
        voted.append(1 if sum(samples) >= 2 else 0)
        centers.append(times[1])

    value = sum(voted[1 + bit] << bit for bit in range(8))
    accepted = voted[0] == 0 and voted[-1] == 1 and value == byte
    return DecodeResult(accepted, value, tuple(voted), tuple(centers))


def all_phase_interval(byte: int, step_percent: float = 0.05) -> tuple[float, float]:
    phases = [index / 40 for index in range(40)]
    errors = [(-8.0 + index * step_percent) / 100.0 for index in range(int(16.0 / step_percent) + 1)]
    passing = [error for error in errors if all(decode_16x(byte, error, phase).accepted for phase in phases)]
    if not passing:
        raise RuntimeError("sweep found no passing region")
    return min(passing), max(passing)


def baud_examples(clock_hz: int = 48_000_000, target: int = 115_200) -> dict[str, float]:
    ideal_divisor = clock_hz / (OVERSAMPLE * target)
    integer_divisor = round(ideal_divisor)
    integer_actual = clock_hz / (OVERSAMPLE * integer_divisor)
    fractional_register = round(ideal_divisor * 64)
    fractional_actual = 64 * clock_hz / (OVERSAMPLE * fractional_register)
    return {
        "ideal_divisor": ideal_divisor,
        "integer_divisor": integer_divisor,
        "integer_actual": integer_actual,
        "integer_ppm": (integer_actual - target) / target * 1_000_000,
        "fractional_register": fractional_register,
        "fractional_divisor": fractional_register / 64,
        "fractional_actual": fractional_actual,
        "fractional_ppm": (fractional_actual - target) / target * 1_000_000,
    }


def svg_text(x: float, y: float, value: str, *, size: int = 16, fill: str = "#172033", anchor: str = "start", weight: int = 500) -> str:
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" text-anchor="{anchor}" fill="{fill}" '
        f'font-family="Inter,Noto Sans SC,Microsoft YaHei,sans-serif" font-size="{size}" '
        f'font-weight="{weight}">{html.escape(value)}</text>'
    )


def write_svg(path: Path, byte: int = 0x55) -> None:
    width, height = 960, 760
    ink, muted = "#172033", "#5f6b7a"
    blue, green, red, amber = "#2563eb", "#16845b", "#c2413c", "#b96800"
    grid, panel = "#cbd5e1", "#f8fafc"
    body: list[str] = []
    body.append(svg_text(48, 52, "16× 过采样的理想 UART 时序实验", size=30, weight=700))
    body.append(svg_text(48, 80, "上：0x55 的采样漂移；下：起始检测相位与 RX 波特率误差扫描", fill=muted))

    # Waveform panel.
    body.append(f'<rect x="45" y="105" width="870" height="270" rx="14" fill="{panel}" stroke="{grid}"/>')
    frame = make_8n1(byte)
    labels = ["Start"] + [f"D{i}" for i in range(8)] + ["Stop"]
    x0, cell, high_y, low_y = 90.0, 78.0, 175.0, 265.0
    previous = 1
    body.append(f'<line x1="70" y1="{high_y}" x2="{x0}" y2="{high_y}" stroke="{blue}" stroke-width="4"/>')
    for index, bit in enumerate(frame):
        x = x0 + index * cell
        y = high_y if bit else low_y
        previous_y = high_y if previous else low_y
        if bit != previous:
            body.append(f'<line x1="{x}" y1="{previous_y}" x2="{x}" y2="{y}" stroke="{blue}" stroke-width="4"/>')
        body.append(f'<line x1="{x}" y1="{y}" x2="{x + cell}" y2="{y}" stroke="{blue}" stroke-width="4"/>')
        body.append(f'<line x1="{x}" y1="145" x2="{x}" y2="292" stroke="{grid}" stroke-dasharray="3 5"/>')
        body.append(svg_text(x + cell / 2, 132, labels[index], size=13, fill=muted, anchor="middle"))
        previous = bit
    body.append(f'<line x1="{x0 + len(frame) * cell}" y1="145" x2="{x0 + len(frame) * cell}" y2="292" stroke="{grid}"/>')

    nominal = decode_16x(byte, 0.0, 0.0)
    slow = decode_16x(byte, -0.045, 0.9)
    for point in nominal.sample_times:
        body.append(f'<circle cx="{x0 + point * cell}" cy="307" r="5" fill="{green}"/>')
    for point in slow.sample_times:
        body.append(f'<path d="M {x0 + point * cell - 5:.2f} 322 l 10 10 m -10 0 l 10 -10" stroke="{red}" stroke-width="3"/>')
    body.append(f'<circle cx="105" cy="357" r="5" fill="{green}"/>')
    body.append(svg_text(120, 362, "标称 RX 中心", size=14, fill=green))
    body.append('<path d="M 315 352 l 10 10 m -10 0 l 10 -10" stroke="#c2413c" stroke-width="3"/>')
    body.append(svg_text(335, 362, "RX 慢 4.5%，检测延迟 0.9 tick", size=14, fill=red))

    # Heatmap panel.
    body.append(f'<rect x="45" y="400" width="870" height="310" rx="14" fill="#ffffff" stroke="{grid}"/>')
    body.append(svg_text(65, 432, "理想模型解码区域（0x55，三点多数表决）", size=19, weight=700))
    errors = [-8.0 + i * 0.5 for i in range(33)]
    phases = [i / 16 for i in range(16)]
    hx, hy, cw, ch = 165.0, 458.0, 21.5, 12.0
    for row, phase in enumerate(phases):
        for column, percent in enumerate(errors):
            ok = decode_16x(byte, percent / 100.0, phase).accepted
            color = "#d1fae5" if ok else "#ffe4e6"
            body.append(f'<rect x="{hx + column*cw:.2f}" y="{hy + row*ch:.2f}" width="{cw + .2:.2f}" height="{ch + .2:.2f}" fill="{color}"/>')
    for percent in (-8, -4, 0, 4, 8):
        column = int((percent + 8) / 0.5)
        x = hx + column * cw + cw / 2
        body.append(svg_text(x, 672, f"{percent:+d}%", size=13, fill=muted, anchor="middle"))
    for phase in (0.0, 0.25, 0.5, 0.75):
        row = int(phase * 16)
        body.append(svg_text(150, hy + row*ch + 10, f"{phase:.2f}", size=12, fill=muted, anchor="end"))
    body.append(svg_text(520, 696, "RX 波特率误差 (B_rx − B_tx) / B_tx", size=14, fill=ink, anchor="middle"))
    body.append(svg_text(75, 540, "检测相位", size=13, fill=ink, anchor="middle"))
    body.append(svg_text(75, 558, "/ tick", size=13, fill=ink, anchor="middle"))
    body.append(f'<rect x="735" y="420" width="16" height="12" fill="#d1fae5"/><rect x="810" y="420" width="16" height="12" fill="#ffe4e6"/>')
    body.append(svg_text(757, 431, "通过", size=12, fill=green))
    body.append(svg_text(832, 431, "失败", size=12, fill=red))
    body.append(svg_text(480, 738, "仅描述无噪声、瞬时边沿、固定频差的教学模型；实际边界以器件数据手册为准。", size=14, fill=amber, anchor="middle", weight=650))

    document = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">
  <title id="title">理想 UART 16 倍过采样时序与误差区域</title>
  <desc id="desc">0x55 波形上叠加标称与偏慢接收时钟的采样点，并扫描接收波特率误差和起始检测相位。</desc>
  <rect width="960" height="760" fill="#ffffff"/>
  {''.join(body)}
</svg>
'''
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--svg", type=Path, help="write the synthetic timing plot to this SVG")
    args = parser.parse_args()

    assert make_8n1(0x53) == [0, 1, 1, 0, 0, 1, 0, 1, 0, 1]
    assert decode_16x(0x55, 0.0, 0.0).accepted
    assert not decode_16x(0x55, 0.08, 0.0).accepted
    low, high = all_phase_interval(0x55)
    div = baud_examples()
    byte_time = 10 / 115_200

    print("model: ideal edges, fixed frequency error, 16x vote at ticks 7/8/9")
    print(f"frame 0x55: {make_8n1(0x55)}")
    print(f"ideal all-phase pass interval: {low*100:+.2f}% .. {high*100:+.2f}% RX rate error")
    print(f"integer divider: N={div['integer_divisor']}, actual={div['integer_actual']:.6f}, error={div['integer_ppm']:+.2f} ppm")
    print(
        "1/64 fractional divider: "
        f"REG={div['fractional_register']} (N={div['fractional_divisor']:.6f}), "
        f"actual={div['fractional_actual']:.6f}, error={div['fractional_ppm']:+.2f} ppm"
    )
    print(f"115200 8N1 byte time: {byte_time*1e6:.4f} us")
    print(f"16-byte FIFO no-service window: {16*byte_time*1e3:.4f} ms")
    print(f"256-byte ring no-consumer window: {256*byte_time*1e3:.4f} ms")
    if args.svg:
        write_svg(args.svg)
        print(f"wrote synthetic SVG: {args.svg}")


if __name__ == "__main__":
    main()
