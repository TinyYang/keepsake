"""诊断工具：判断一张截图里详情弹窗是否打开。

    python scripts/match_one.py probe/xxx.png
"""
import os
import sys

import numpy as np
from PIL import Image

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
TPL_DIR = os.path.join(ROOT, "templates")
CAMERA_BOX = (820, 1765, 982, 1977)


def ncc_at(win, tpl):
    t = tpl - tpl.mean()
    w = win - win.mean()
    denom = (np.sqrt((w * w).sum()) + 1e-6) * (np.sqrt((t * t).sum()) + 1e-6)
    return float((w * t).sum() / denom)


def main(path):
    g = np.asarray(Image.open(path).convert("L"), dtype=np.float32)
    tpl = np.asarray(Image.open(os.path.join(TPL_DIR, "camera_icon.png")).convert("L"), dtype=np.float32)
    th, tw = tpl.shape
    x0, y0, x1, y1 = CAMERA_BOX
    win = g[y0:y0 + th, x0:x0 + tw]
    print(f"{os.path.basename(path)}: popup_score={ncc_at(win, tpl):.4f}  (>=0.90 判定弹窗已开)")
    # 顺带报一下整屏平均亮度，用于发现黑屏/休眠
    print(f"  mean_brightness={g.mean():.1f}")


if __name__ == "__main__":
    main(sys.argv[1])
