"""量出相册网格页的几何：6 个卡片包围盒（基于局部方差而不是白边）。

用于 L2 坐标兜底，以及 L1 里限定模板搜索区域（不必全屏搜索）。
"""
import sys
import numpy as np
from PIL import Image


def bands_from(profile, thresh, min_len=30):
    on = profile > thresh
    out, start = [], None
    for i, v in enumerate(on):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= min_len:
                out.append((start, i - 1))
            start = None
    if start is not None and len(on) - start >= min_len:
        out.append((start, len(on) - 1))
    return out


def main(path):
    img = Image.open(path).convert("L")
    a = np.asarray(img).astype(np.float32)
    h, w = a.shape
    print(f"image: {w}x{h}")

    # 卡片区：避开顶部标题栏与底部按钮条
    y0, y1 = int(h * 0.15), int(h * 0.72)
    reg = a[y0:y1, :]

    # 高频能量：相邻像素差的绝对值，背景是平坦米色 -> 接近 0
    gx = np.abs(np.diff(reg, axis=1))
    gy = np.abs(np.diff(reg, axis=0))

    col_prof = gx.mean(axis=0)   # 每列的纵向边缘能量
    row_prof = gy.mean(axis=1)   # 每行的横向边缘能量

    print(f"col_prof: min={col_prof.min():.2f} max={col_prof.max():.2f} mean={col_prof.mean():.2f}")
    print(f"row_prof: min={row_prof.min():.2f} max={row_prof.max():.2f} mean={row_prof.mean():.2f}")

    ct = col_prof.mean() + (col_prof.max() - col_prof.mean()) * 0.35
    rt = row_prof.mean() + (row_prof.max() - row_prof.mean()) * 0.35

    cb = bands_from(col_prof, ct, min_len=60)
    rb = bands_from(row_prof, rt, min_len=60)

    print("\ncolumn bands (x, absolute):")
    for b in cb:
        print(f"  {b[0]} .. {b[1]}  width={b[1]-b[0]+1}  center={(b[0]+b[1])//2}")

    print("\nrow bands (y, absolute):")
    for b in rb:
        print(f"  {b[0]+y0} .. {b[1]+y0}  height={b[1]-b[0]+1}  center={(b[0]+b[1])//2+y0}")

    xs = [(b[0] + b[1]) // 2 for b in cb]
    ys = [(b[0] + b[1]) // 2 + y0 for b in rb]
    print(f"\ngrid: {len(xs)} cols x {len(ys)} rows")
    n = 1
    for y in ys:
        for x in xs:
            print(f"  slot {n:2d}: ({x}, {y})")
            n += 1


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "probe/now.png")
