"""아이폰 홈 화면 아이콘 생성 — 외부 라이브러리 없이.

  python scripts/make_icons.py

Pillow 를 쓰지 않는다. 아이콘은 **한 번 만들어 저장소에 커밋하는 정적 파일**이라
그걸 위해 배포 의존성을 늘릴 이유가 없다. zlib 로 PNG 를 직접 쓴다.

도안
----
HOME RADAR = 집(HOME)에서 퍼져 나가는 레이더(RADAR).
가운데 흰 집, 거기서 번지는 동심원, 훑고 지나가는 부채꼴, 초록 블립 하나.

경계는 SDF(부호 있는 거리)로 그려 1픽셀 안티에일리어싱을 넣는다 —
180px 로 줄여도 테두리가 깨지지 않는다.

만드는 파일 (app/web/icons/)
  apple-touch-icon.png  180  iOS 홈 화면 (투명도·여백 없이 꽉 채운다)
  icon-192.png          192  manifest any
  icon-512.png          512  manifest any
  icon-maskable-512.png 512  manifest maskable (내용을 안쪽 78% 에 모은다)
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "app" / "web" / "icons"

# ---------------------------------------------------------------- 색
BG_IN = (0x1a, 0x36, 0x66)      # 배경 가운데 (남색)
BG_OUT = (0x06, 0x0c, 0x18)     # 배경 바깥 (거의 검정)
RING = (0x47, 0x8c, 0xe0)       # 동심원
SWEEP = (0x6f, 0xb4, 0xff)      # 훑고 지나가는 부채꼴
CROSS = (0x3d, 0x6f, 0xb0)      # 십자선
HOUSE = (0xff, 0xff, 0xff)      # 집
DOOR = (0x16, 0x30, 0x5c)       # 문 (배경색 계열로 파낸다)
BLIP = (0x5f, 0xe8, 0xa6)       # 블립


# ---------------------------------------------------------------- PNG 쓰기
def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def write_png(path: Path, size: int, rows: list[bytearray]) -> None:
    """truecolor(RGB, 8bit) PNG. 알파를 쓰지 않는다 —
    iOS 홈 화면 아이콘은 투명이 검게 나와서 불투명으로 꽉 채우는 게 맞다."""
    raw = b"".join(b"\x00" + bytes(r) for r in rows)      # 필터 0 (None)
    png = (b"\x89PNG\r\n\x1a\n"
           + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + _chunk(b"IDAT", zlib.compress(raw, 9))
           + _chunk(b"IEND", b""))
    path.write_bytes(png)


# ---------------------------------------------------------------- 보조
def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def lerp3(a: tuple, b: tuple, t: float) -> tuple:
    return (a[0] + (b[0] - a[0]) * t,
            a[1] + (b[1] - a[1]) * t,
            a[2] + (b[2] - a[2]) * t)


def over(dst: tuple, src: tuple, alpha: float) -> tuple:
    """일반적인 알파 합성 (src 를 dst 위에)."""
    if alpha <= 0:
        return dst
    if alpha >= 1:
        return src
    return lerp3(dst, src, alpha)


def cov(dist: float, px: float) -> float:
    """SDF 거리를 픽셀 커버리지로. dist<0 이 도형 안쪽."""
    return clamp(0.5 - dist / px)


def sd_box(x: float, y: float, hw: float, hh: float) -> float:
    dx, dy = abs(x) - hw, abs(y) - hh
    outside = math.hypot(max(dx, 0.0), max(dy, 0.0))
    return outside + min(max(dx, dy), 0.0)


def sd_segment(x: float, y: float, ax: float, ay: float,
               bx: float, by: float) -> float:
    """점에서 선분까지의 거리 (두께는 호출한 쪽에서 뺀다)."""
    pax, pay = x - ax, y - ay
    bax, bay = bx - ax, by - ay
    denom = bax * bax + bay * bay
    h = 0.0 if denom == 0 else clamp((pax * bax + pay * bay) / denom)
    return math.hypot(pax - bax * h, pay - bay * h)


def sd_triangle(x: float, y: float, p: list[tuple[float, float]]) -> float:
    """삼각형 SDF (내부는 음수). 지붕에 쓴다."""
    d = min(sd_segment(x, y, p[i][0], p[i][1],
                       p[(i + 1) % 3][0], p[(i + 1) % 3][1])
            for i in range(3))
    # 내부 판정 — 외적 부호가 모두 같으면 안쪽
    signs = []
    for i in range(3):
        ax, ay = p[i]
        bx, by = p[(i + 1) % 3]
        signs.append((bx - ax) * (y - ay) - (by - ay) * (x - ax))
    inside = all(s >= 0 for s in signs) or all(s <= 0 for s in signs)
    return -d if inside else d


# ---------------------------------------------------------------- 도안
def shade(u: float, v: float, px: float) -> tuple:
    """정규화 좌표(-1..1)에서의 색. px 는 한 픽셀의 길이 (안티에일리어싱용)."""
    r = math.hypot(u, v)

    # 배경 — 가운데가 밝은 방사형 그라데이션
    col = lerp3(BG_IN, BG_OUT, clamp(r / 1.35))

    # 훑고 지나가는 부채꼴: 12시 방향에서 시계방향 75°, 꼬리로 갈수록 흐려진다
    if r <= 0.92:
        ang = math.degrees(math.atan2(u, -v)) % 360      # 12시=0, 시계방향
        if ang <= 78:
            tail = 1.0 - ang / 78.0
            a = 0.46 * tail * tail * clamp((0.92 - r) / 0.10)
            col = over(col, SWEEP, a)

    # 십자선
    for d in (abs(u), abs(v)):
        if r <= 0.88:
            col = over(col, CROSS,
                       0.34 * cov(d - 0.0065, px) * clamp((0.88 - r) / 0.06))

    # 동심원 — 바깥쪽을 조금 더 또렷하게
    for rad, w, a in ((0.40, 0.013, 0.60), (0.62, 0.013, 0.70),
                      (0.85, 0.020, 0.92)):
        col = over(col, RING, a * cov(abs(r - rad) - w, px))

    # 블립 — 1시 방향, 은은한 번짐 + 또렷한 점
    bx, by = 0.62 * math.sin(math.radians(52)), -0.62 * math.cos(math.radians(52))
    bd = math.hypot(u - bx, v - by)
    col = over(col, BLIP, 0.30 * clamp((0.085 - bd) / 0.085))
    col = over(col, BLIP, cov(bd - 0.042, px))

    # 집 — 지붕(삼각형) + 몸통(사각형) + 문
    roof = sd_triangle(u, v, [(-0.265, -0.035), (0.265, -0.035), (0.0, -0.285)])
    body = sd_box(u, v - 0.115, 0.185, 0.150)
    house = min(roof, body)
    col = over(col, HOUSE, cov(house, px))
    door = sd_box(u, v - 0.175, 0.055, 0.090)
    col = over(col, DOOR, cov(door, px) * cov(house, px))

    return col


def render(size: int, content_scale: float = 1.0) -> list[bytearray]:
    """아이콘 한 장. content_scale<1 이면 도안을 안쪽으로 모은다(maskable)."""
    px = 2.0 / size / content_scale
    rows: list[bytearray] = []
    for y in range(size):
        v = ((y + 0.5) / size * 2.0 - 1.0) / content_scale
        row = bytearray()
        for x in range(size):
            u = ((x + 0.5) / size * 2.0 - 1.0) / content_scale
            r, g, b = shade(u, v, px)
            row += bytes((int(clamp(r, 0, 255) + 0.5),
                          int(clamp(g, 0, 255) + 0.5),
                          int(clamp(b, 0, 255) + 0.5)))
        rows.append(row)
    return rows


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = [
        ("apple-touch-icon.png", 180, 1.0),
        ("icon-192.png", 192, 1.0),
        ("icon-512.png", 512, 1.0),
        # maskable 은 바깥을 잘라낼 수 있으니 안전영역(안쪽 ~80%)에 내용을 모은다
        ("icon-maskable-512.png", 512, 0.76),
    ]
    for name, size, scale in jobs:
        write_png(OUT / name, size, render(size, scale))
        kb = (OUT / name).stat().st_size / 1024
        print(f"  {name:24s} {size}x{size}  {kb:6.1f} KB")
    print(f"\n{len(jobs)}개 생성 → {OUT}")


if __name__ == "__main__":
    main()
