# -*- coding: utf-8 -*-
r"""生成 DeepChat 全套图标（原创设计，不使用任何第三方品牌标志）。

设计语言
--------
· 底：圆角方形（squircle），深靛 → 紫的对角渐变，右下角一团柔光，避免"廉价纯渐变"。
· 主体：**一个白色对话气泡 + 向上长出的两根枝条 + 两个实心节点** ——
  "一句提问分出两条对话"，正好是树状对话的最小表达；
  比单纯画一个 Y 更像产品，也不会被误看成字母。
· 主体带一层柔和投影，让它像"浮"在底上，而不是贴纸。
· 小尺寸（16/24/32）自动加粗线条、放大节点，保证任务栏里不糊。

用法： python tool/make_icons.py
输出：
  server/static/icon.ico   (16/24/32/48/64/128/256)
  server/static/icon.png   (256)
  server/static/tray.png   (64)
  client/static/icon.ico
  client/static/tray.png
  tool/launcher/icon.ico
"""
import io
import pathlib
import struct

from PIL import Image, ImageDraw, ImageFilter

ROOT = pathlib.Path(__file__).resolve().parents[1]
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

BG = (79, 70, 229)           # #4F46E5 纯色底（干净，不用渐变）
WHITE = (255, 255, 255, 255)

BASE = 512.0                 # 设计基准（所有坐标按此写，再按目标尺寸缩放）


def solid_bg(size):
    return Image.new('RGB', (size, size), BG)


def rounded_mask(size, radius):
    m = Image.new('L', (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def draw_mark(canvas, small):
    """返回 (窗口 mask, 气泡 mask)。

    语义：窗口 = 操作模式（工作台 / 终端），气泡 = 对话模式；
    气泡压在窗口右下角，中间留一道底色缝隙，两个元素各念各的又连成一体。
    """
    k = canvas / BASE

    def S(v):
        return v * k

    win = Image.new('L', (canvas, canvas), 0)
    wd = ImageDraw.Draw(win)
    # 窗口（圆角矩形）
    wd.rounded_rectangle([S(112), S(112), S(390), S(302)], radius=S(34), fill=255)

    # 窗口里是终端提示符「> _」——操作模式的语义（负形挖空）
    if small:
        # 小尺寸只留一道粗横杠，别糊
        wd.rounded_rectangle([S(158), S(190), S(346), S(228)], radius=S(16), fill=0)
    else:
        lw = int(S(26))
        wd.line([(S(168), S(160)), (S(208), S(196)), (S(168), S(232))],
                fill=0, width=lw, joint='curve')
        wd.rounded_rectangle([S(224), S(210), S(282), S(232)], radius=S(10), fill=0)

    bub = Image.new('L', (canvas, canvas), 0)
    bd = ImageDraw.Draw(bub)
    bd.rounded_rectangle([S(258), S(262), S(404), S(372)], radius=S(34), fill=255)
    bd.polygon([(S(292), S(360)), (S(324), S(360)), (S(278), S(406))], fill=255)

    return win, bub


def draw_icon(size, *, small=False):
    ss = 4 if size <= 128 else 2
    canvas = size * ss

    bg = solid_bg(canvas).convert('RGBA')
    bg.putalpha(rounded_mask(canvas, int(canvas * 0.235)))

    win, bub = draw_mark(canvas, small)

    layer = Image.new('RGBA', (canvas, canvas), (0, 0, 0, 0))

    # ① 窗口
    w = Image.new('RGBA', (canvas, canvas), WHITE)
    w.putalpha(win)
    layer.alpha_composite(w)

    # ② 气泡外围一圈底色缝隙（把两个白块分开，否则会粘成一块）
    gap = canvas * (0.030 if small else 0.026)
    grown = bub.filter(ImageFilter.GaussianBlur(gap * 0.55)).point(lambda v: 255 if v > 70 else 0)
    g = Image.new('RGBA', (canvas, canvas), (BG[0], BG[1], BG[2], 255))
    g.putalpha(grown)
    layer.alpha_composite(g)

    # ③ 气泡本体
    b = Image.new('RGBA', (canvas, canvas), WHITE)
    b.putalpha(bub)
    layer.alpha_composite(b)

    out = Image.alpha_composite(bg, layer)
    return out.resize((size, size), Image.LANCZOS)


def write_ico(path, frames):
    """手工写 ICO：每帧用各自尺寸的专用绘制结果（Pillow 默认会把大图缩小）。"""
    sizes = sorted(frames.keys())
    blobs = []
    for s in sizes:
        buf = io.BytesIO()
        frames[s].save(buf, format='PNG')
        blobs.append((s, buf.getvalue()))

    header = struct.pack('<HHH', 0, 1, len(blobs))
    entries = b''
    offset = 6 + 16 * len(blobs)
    data = b''
    for s, blob in blobs:
        b = 0 if s >= 256 else s
        entries += struct.pack('<BBBBHHII', b, b, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        data += blob
    path.write_bytes(header + entries + data)


def main():
    frames = {sz: draw_icon(sz, small=sz <= 32) for sz in ICO_SIZES}

    ico_path = ROOT / 'server' / 'static' / 'icon.ico'
    write_ico(ico_path, frames)

    png = ROOT / 'server' / 'static' / 'icon.png'
    frames[256].save(png, format='PNG')

    tray = ROOT / 'server' / 'static' / 'tray.png'
    draw_icon(64).save(tray, format='PNG')

    for dst in (ROOT / 'client' / 'static' / 'icon.ico', ROOT / 'tool' / 'launcher' / 'icon.ico'):
        dst.write_bytes(ico_path.read_bytes())

    (ROOT / 'client' / 'static' / 'tray.png').write_bytes(tray.read_bytes())
    # ★ 2026-11-12：client/control.py 会读 client/static/icon.png 当窗口/托盘图标，
    #   之前漏了这个文件，客户端一直显示的是旧的 DeepSeek 标志。
    (ROOT / 'client' / 'static' / 'icon.png').write_bytes(png.read_bytes())

    for t in (ico_path, png, tray, ROOT / 'client' / 'static' / 'icon.ico',
              ROOT / 'client' / 'static' / 'icon.png',
              ROOT / 'tool' / 'launcher' / 'icon.ico', ROOT / 'client' / 'static' / 'tray.png'):
        print('  ✅', t.relative_to(ROOT), t.stat().st_size, 'bytes')


if __name__ == '__main__':
    main()
