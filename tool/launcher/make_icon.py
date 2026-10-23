"""从项目自带的 server/static/logo.svg 生成应用图标（PNG / ICO / 托盘图）。

★ 说明：logo.svg 就是这个项目**已有的品牌图标**，前端 index.html 也拿它当 favicon
  （`<link rel="icon" href="static/logo.svg">`）。所以启动器、app.exe、agent.exe
  都应当复用它，而不是另画一个。

SVG 是矢量、且本项目没有 SVG 渲染库，所以光栅化那一步交给 Chrome
（见 tools/launcher/render_logo.mjs）：先把 logo 渲染成若干尺寸的 PNG，
本脚本再给它们加一层圆角底并合成 ICO。

单独一个鲸鱼图形直接当应用图标并不好：任务栏/标题栏底色不可控，
深色背景下蓝色鲸鱼会糊成一团。所以这里做成「圆角方块 + 居中白鲸」的
标准应用图标样式；鲸鱼轮廓从渲染出来的 PNG 取白色蒙版。
"""
import io
import os
import sys

from PIL import Image

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))

# 与前端 logo 渐变一致（.logo{background:linear-gradient(135deg,#2563eb,#7c3aed)}）
C_A = (37, 99, 235)     # #2563eb
C_B = (124, 58, 237)    # #7c3aed


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def diagonal_gradient(size):
    """135° 对角渐变（左上→右下），与前端 logo 一致。"""
    img = Image.new('RGBA', (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1)) if size > 1 else 0
            px[x, y] = lerp(C_A, C_B, t) + (255,)
    return img


def whale_mask(size, src_png):
    """把渲染出的鲸鱼 PNG 转成白色蒙版（按亮度取 alpha）。"""
    src = Image.open(src_png).convert('RGBA').resize((size, size), Image.LANCZOS)
    mask = Image.new('L', (size, size), 0)
    sp, mp = src.load(), mask.load()
    for y in range(size):
        for x in range(size):
            r, g, b, a = sp[x, y]
            if a == 0:
                continue
            # 图形是蓝色鲸鱼画在透明底上：不透明度即形状
            mp[x, y] = a
    return mask


def build(size, src_png):
    """圆角渐变底 + 居中白鲸。"""
    S = size * 4                                   # 超采样，边缘更平滑
    base = diagonal_gradient(S)

    # 圆角遮罩
    from PIL import ImageDraw
    rmask = Image.new('L', (S, S), 0)
    ImageDraw.Draw(rmask).rounded_rectangle([0, 0, S - 1, S - 1], radius=round(S * 0.22), fill=255)
    base.putalpha(rmask)

    # 鲸鱼：占中间约 62%
    inner = round(S * 0.62)
    off = (S - inner) // 2
    m = whale_mask(inner, src_png)
    white = Image.new('RGBA', (inner, inner), (255, 255, 255, 255))
    base.paste(white, (off, off), m)

    return base.resize((size, size), Image.LANCZOS)


def main():
    src = os.path.join(HERE, '_svg_1024.png')
    if not os.path.isfile(src):
        # 退回最大的那张渲染图
        cands = sorted([f for f in os.listdir(HERE) if f.startswith('_svg_') and f.endswith('.png')],
                       key=lambda f: -int(f.split('_')[2].split('.')[0]))
        if not cands:
            print('  ❌ 找不到 logo 渲染图，请先运行 render_logo.mjs')
            return 1
        src = os.path.join(HERE, cands[0])
    print(f'  源图形: {os.path.basename(src)}')

    big = build(1024 if os.path.basename(src) == '_svg_1024.png' else 256, src)

    png = os.path.join(HERE, 'icon.png')
    big.resize((256, 256), Image.LANCZOS).save(png)
    print(f'  → {png}')

    ico = os.path.join(HERE, 'icon.ico')
    big.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f'  → {ico}')

    tray = os.path.join(HERE, 'tray.png')
    big.resize((64, 64), Image.LANCZOS).save(tray)
    print(f'  → {tray}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
