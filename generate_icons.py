# -*- coding: utf-8 -*-
"""生成开始菜单工具所需的自绘扁平图标（超采样抗锯齿，透明底 PNG）。

风格：Lucide/Feather 式线性图标，圆头端点，与 customtkinter 深色主题统一。
"""
import os
from PIL import Image, ImageDraw

S = 256          # 超采样画布
W = 22           # 线宽（对应 24px 显示尺寸约 2px）
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
os.makedirs(OUT, exist_ok=True)


def canvas():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def line(d, pts, color, w=W, joint="curve"):
    d.line(pts, fill=color, width=w, joint=joint)
    # 圆头端点（Pillow line 不直接圆头，这里在首尾补画小圆）
    r = w // 2
    for p in pts:
        d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=color)


def save(img, name):
    img.save(os.path.join(OUT, name))
    print("生成", name)


# ---------------------------------------------------------------------------
# 1. app —— 开始菜单方格（四宫格，两实两空）
# ---------------------------------------------------------------------------
def make_app():
    img, d = canvas()
    c = (79, 143, 247, 255)      # 主蓝
    c_soft = (143, 176, 238, 210)  # 空心格柔化蓝
    m = 40; gap = 22; cell = (S - 2 * m - gap) // 2
    x0, y0 = m, m
    def rect(cx, cy, fill_c, width=0):
        x = x0 + cx * (cell + gap)
        y = y0 + cy * (cell + gap)
        d.rounded_rectangle([x, y, x + cell, y + cell], radius=26,
                            fill=fill_c, width=width, outline=(fill_c if width else None))
    rect(0, 0, c)          # 左上实心
    rect(1, 1, c)          # 右下实心
    rect(1, 0, None)       # 右上空心（描线）
    rect(0, 1, None)
    # 空心格用描线重画
    def outline(cx, cy):
        x = x0 + cx * (cell + gap)
        y = y0 + cy * (cell + gap)
        d.rounded_rectangle([x, y, x + cell, y + cell], radius=26,
                            outline=c_soft, width=W)
    outline(1, 0)
    outline(0, 1)
    save(img, "app.png")


# ---------------------------------------------------------------------------
# 2. backup —— 向下箭头 + 托盘
# ---------------------------------------------------------------------------
def make_backup():
    img, d = canvas()
    c = (255, 255, 255, 255)
    line(d, [(128, 44), (128, 150)], c)          # 竖线
    line(d, [(128, 150), (78, 104)], c)          # 左斜
    line(d, [(128, 150), (178, 104)], c)         # 右斜
    line(d, [(64, 192), (192, 192)], c)          # 托盘
    save(img, "backup.png")


# ---------------------------------------------------------------------------
# 3. restore —— 逆时针回旋箭头
# ---------------------------------------------------------------------------
def make_restore():
    img, d = canvas()
    c = (255, 255, 255, 255)
    bbox = [46, 46, 210, 210]
    d.arc(bbox, 0, 270, fill=c, width=W)         # 从右侧起，逆时针 3/4 圆
    # 箭头尖（顶端，指向左）
    line(d, [(128, 46), (86, 46)], c, w=W)       # 切线
    line(d, [(128, 46), (94, 22)], c)            # 上边
    line(d, [(128, 46), (94, 70)], c)            # 下边
    save(img, "restore.png")


# ---------------------------------------------------------------------------
# 4. folder —— 文件夹（描边轮廓）
# ---------------------------------------------------------------------------
def make_folder():
    img, d = canvas()
    c = (232, 236, 245, 255)
    pts = [
        (200, 84),   # 主体右上角附近（暂留 tab 空间）
        (200, 188),  # 右下角
        (56, 188),   # 左下角
        (56, 116),   # 左侧上端
        (56, 84),    # tab 上端
        (120, 84),   # tab 右端
        (164, 112),  # 斜边
        (200, 112),  # 主体顶边右端
        (200, 84),   # 回到起点（右上 tab 口）
    ]
    line(d, pts, c, w=W, joint="curve")
    save(img, "folder.png")


# ---------------------------------------------------------------------------
# 5. missing —— 放大镜（检查缺失）
# ---------------------------------------------------------------------------
def make_missing():
    img, d = canvas()
    c = (255, 255, 255, 255)
    cx, cy, r = 108, 108, 58
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=c, width=W)
    line(d, [(150, 150), (208, 208)], c, w=W)   # 手柄
    save(img, "missing.png")


if __name__ == "__main__":
    make_app()
    make_backup()
    make_restore()
    make_folder()
    make_missing()
    print("全部图标生成完成 ->", OUT)