"""受控实验 v2：同一张卡，截图 vs 游戏原生导出。

关键：截屏后立刻另存为独立文件，避免后续 probe_state 覆盖。
再用「对齐搜索」找到插画在屏幕上的精确矩形，缩放后与原生图比对。
"""
import os
import shutil
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import frogcap as fc  # noqa: E402

PROBE = fc.PROBE


def align(shot_img, native_img):
    """在截图里搜索与原生图最匹配的矩形（比例锁定）。"""
    n = np.asarray(native_img, dtype=np.float32)
    n0 = n - n.mean()
    nnorm = np.sqrt((n0 * n0).sum())
    ar = native_img.size[0] / native_img.size[1]
    sw, sh = shot_img.size

    def score(x0, y0, w):
        h = int(round(w / ar))
        if x0 < 0 or y0 < 0 or x0 + w > sw or y0 + h > sh:
            return -2
        c = np.asarray(shot_img.crop((x0, y0, x0 + w, y0 + h)).resize(native_img.size, Image.BILINEAR),
                       dtype=np.float32)
        c0 = c - c.mean()
        return float((c0 * n0).sum() / (np.sqrt((c0 * c0).sum()) * nnorm))

    best = (-2, None)
    for w in range(760, 1060, 12):
        for x0 in range(80, 330, 10):
            for y0 in range(900, 1130, 10):
                s = score(x0, y0, w)
                if s > best[0]:
                    best = (s, (x0, y0, w))
    s0, (bx, by, bw) = best
    best2 = (-2, None)
    for w in range(bw - 14, bw + 15, 2):
        for x0 in range(bx - 12, bx + 13, 2):
            for y0 in range(by - 12, by + 13, 2):
                s = score(x0, y0, w)
                if s > best2[0]:
                    best2 = (s, (x0, y0, w))
    return best2


def main():
    rc, out, _ = fc.adb("devices")
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            fc.SERIAL = parts[0]
            break
    fc.CAM_T = fc.build_template()

    if fc.probe_state()[0] == "detail":
        fc.close_detail()

    ok, info = fc.open_card((388, 955), "slot1")
    if not ok:
        print("打不开")
        return

    shot_path = os.path.join(PROBE, "exp_shot.png")
    shutil.copy(fc.screencap(), shot_path)
    print("截屏已存:", shot_path)

    before = set(fc.pictures())
    fc.hold(*fc.CAMERA_PT)
    new = fc.wait_new_file(before)
    if not new:
        fc.hold(fc.CAMERA_PT[0], fc.CAMERA_PT[1], 250)
        new = fc.wait_new_file(before)
    if not new:
        print("没存出来")
        return
    name = sorted(new)[0]
    native_path = os.path.join(PROBE, "exp_native.png")
    fc.adb("pull", f"/sdcard/Pictures/{name}", native_path)
    print("原生:", name)

    shot = Image.open(shot_path).convert("RGB")
    native = Image.open(native_path).convert("RGB")
    s, (x0, y0, w) = align(shot, native)
    h = int(round(w / (native.size[0] / native.size[1])))
    print("\n最佳对齐: corr=%.5f  box=(%d,%d,%d,%d)  显示尺寸 %dx%d  倍率 %.2fx"
          % (s, x0, y0, w, h, w, h, w / native.size[0]))

    crop = shot.crop((x0, y0, x0 + w, y0 + h))
    crop.save(os.path.join(PROBE, "exp_crop_aligned.png"))
    c = np.asarray(crop.resize(native.size, Image.LANCZOS), dtype=np.float32)
    n = np.asarray(native, dtype=np.float32)
    print("对齐后 MAE=%.2f/255   相关=%.5f" % (np.abs(c - n).mean(), s))

    def hf(a):
        g = a.mean(axis=2)
        return (np.abs(np.diff(g, axis=1)).mean() + np.abs(np.diff(g, axis=0)).mean()) / 2

    print("\n--- 锐度（越高越清晰）---")
    print("截图裁剪 %dx%d : %.3f" % (w, h, hf(np.asarray(crop, dtype=np.float32))))
    print("原生     500x350 : %.3f" % hf(n))
    print("原生放大到裁剪尺寸 : %.3f" % hf(np.asarray(native.resize((w, h), Image.LANCZOS), dtype=np.float32)))

    left = native.resize((1000, 700), Image.LANCZOS)
    right = crop.resize((1000, 700), Image.LANCZOS)
    canvas = Image.new("RGB", (2030, 700), (255, 255, 255))
    canvas.paste(left, (0, 0))
    canvas.paste(right, (1030, 0))
    canvas.save(os.path.join(PROBE, "compare_final.png"))
    print("\n对照图: probe/compare_final.png")

    fc.close_detail(wait_first=2.6)


if __name__ == "__main__":
    main()
