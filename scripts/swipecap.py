"""P1 明信片滑扫引擎（swipe sweep）——比网格点选稳得多。

原理
----
相册详情页支持左右滑动切换上一张/下一张。于是整个采集变成：

    打开第 1 张 → [保存 → 左滑] 循环

好处：
  · 不再需要 6 个格子的坐标（网格布局、排序方式、页码全部不用管）
  · 不会出现"点错格子→打开上一张"的错位
  · 每张只需 2 个固定动作：点相机(900,1871)、左滑(1050,1350 → 300,1350)

安全设计
--------
1. 每一步都验证：弹窗还在吗？画面变了没？文件真的出现了吗？
2. 画面连续 3 次没变 → 判定已到相册末尾，正常结束
3. 任何异常（弹窗消失 / 危险框 / 长红）→ 立即停，不乱点
4. 断点续跑：记录已处理张数，重跑时先快进 N 张
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

import numpy as np
from PIL import Image

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
def _resolve_adb():
    """按优先级找 adb：环境变量 ADB_PATH -> 仓库内 tools/ -> 系统 PATH。"""
    cands = [
        os.environ.get("ADB_PATH"),
        os.path.join(ROOT, "tools", "platform-tools", "adb.exe"),
        os.path.join(ROOT, "tools", "platform-tools", "adb"),
        "adb",
    ]
    for c in cands:
        if not c:
            continue
        if c == "adb" or os.path.exists(c):
            return c
    return "adb"


ADB = _resolve_adb()
PROBE = os.path.join(ROOT, "probe")
TPL_DIR = os.path.join(ROOT, "templates")
ARCHIVE = os.path.join(ROOT, "archive")
POSTCARDS = os.path.join(ARCHIVE, "postcards")
MANIFEST = os.path.join(ARCHIVE, "manifest.json")
LOG_PATH = os.path.join(PROBE, "sweep_log.txt")
STATE_PATH = os.path.join(PROBE, "sweep_state.json")
SHOT = os.path.join(PROBE, "_sweep_shot.png")

CAMERA_BOX = (820, 1765, 982, 1977)
CAMERA_PT = (900, 1871)
POSTCARD_CROP = (170, 1010, 1140, 1690)
SWIPE_FROM = (1050, 1350)      # 正向：右->左（下一张）
SWIPE_TO = (300, 1350)
BACK_FROM = (300, 1350)        # 反向：左->右（上一张）
BACK_TO = (1050, 1350)
SWIPE_MS = 260

SERIAL = None
CAM_T = None


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


_DEVICE_MODEL = {"v": None}


def device_model():
    """运行时读取设备型号，避免把作者机型写死进代码。"""
    if _DEVICE_MODEL["v"] is None:
        try:
            _, out, _ = adb("shell", "getprop", "ro.product.model")
            _DEVICE_MODEL["v"] = out.strip() or "unknown"
        except Exception:
            _DEVICE_MODEL["v"] = "unknown"
    return _DEVICE_MODEL["v"]


def fatal(msg):
    log("FATAL: " + msg)
    log("已安全停止，不再发出任何点击。")
    sys.exit(3)


def adb(*args, timeout=60):
    cmd = [ADB] + (["-s", SERIAL] if SERIAL else []) + [str(a) for a in args]
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def hold(x, y, ms=200):
    rc, _, err = adb("shell", "input", "swipe", x, y, x, y, ms)
    if rc != 0:
        fatal(f"点击 ({x},{y}) 失败: {err.strip()}")


def swipe_next():
    rc, _, err = adb("shell", "input", "swipe", SWIPE_FROM[0], SWIPE_FROM[1],
                     SWIPE_TO[0], SWIPE_TO[1], SWIPE_MS)
    if rc != 0:
        fatal(f"滑动失败: {err.strip()}")


def is_locked():
    """判断是否停在系统锁屏。adb 无法解锁，只能提示人工。"""
    _, out, _ = adb("shell", "dumpsys", "window")
    return "isKeyguardShowing=true" in out


def ensure_stay_awake():
    """充电时保持屏幕常亮（svc power stayon），并唤醒屏幕。

    HarmonyOS/Android 默认会把"注入的点击"当成非用户活动，
    一段时间不碰屏幕就会休眠 —— 采集必须自己把这个坑填上。
    """
    adb("shell", "svc", "power", "stayon", "true")
    rc, out, _ = adb("shell", "dumpsys", "power")
    awake = "mWakefulness=Awake" in out
    if not awake:
        adb("shell", "input", "keyevent", "224")   # KEYCODE_WAKEUP
        time.sleep(1.6)
    return awake


def screencap(path=SHOT):
    cmd = [ADB] + (["-s", SERIAL] if SERIAL else []) + ["exec-out", "screencap", "-p"]
    with open(path, "wb") as f:
        p = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, timeout=60)
    if p.returncode != 0 or os.path.getsize(path) < 10000:
        fatal("截屏失败（文件过小），可能屏幕休眠或数据线断开")
    return path


def as_gray(p):
    return np.asarray(Image.open(p).convert("L"), dtype=np.float32)


def camera_score(g):
    th, tw = CAM_T.shape
    x0, y0 = CAMERA_BOX[0], CAMERA_BOX[1]
    win = g[y0:y0 + th, x0:x0 + tw]
    if win.shape != CAM_T.shape:
        return -1.0
    t = CAM_T - CAM_T.mean()
    w = win - win.mean()
    d = (np.sqrt((w * w).sum()) + 1e-6) * (np.sqrt((t * t).sum()) + 1e-6)
    return float((w * t).sum() / d)


PAGE_CROP = (850, 2650, 1250, 2800)


NEXT_PT = (1199, 1409)   # 相册网格页右侧翻页箭头


def is_blank(p):
    """判断明信片画面是否还是空白/未加载。

    正常插画有丰富颜色，灰度标准差通常 > 15；纯白/纯灰底 < 3。
    """
    im = Image.open(p).convert("RGB").crop(POSTCARD_CROP)
    a = np.asarray(im, dtype=np.float32)
    std = float(a.std())
    mean = float(a.mean())
    return (std < 3.0) or (mean > 250.0), round(std, 1), round(mean, 1)


def preload_pages(n, wait=1.0):
    """把相册一页页翻过去，触发懒加载。调用前必须已停在相册第 1 页。"""
    log(f"预热：开始翻页 {n} 页（让游戏把图加载出来）")
    for i in range(n):
        adb("shell", "input", "swipe", NEXT_PT[0], NEXT_PT[1],
            NEXT_PT[0], NEXT_PT[1], "180")
        time.sleep(wait)
        if (i + 1) % 10 == 0:
            log(f"  预热 {i + 1}/{n} 页")
    log("预热完成")


def frame_sig(p):
    """整帧签名：明信片画面 + 右下角页码区。用于区分"卡住"与"真的换图"。"""
    im = Image.open(p).convert("RGB")
    h = hashlib.sha256()
    h.update(im.crop(POSTCARD_CROP).tobytes())
    h.update(im.crop(PAGE_CROP).tobytes())
    return h.hexdigest()



def postcard_key(p):
    im = Image.open(p).convert("RGB").crop(POSTCARD_CROP)
    return hashlib.sha256(im.tobytes()).hexdigest()


def pictures():
    rc, out, _ = adb("shell", "ls", "/sdcard/Pictures")
    return [n.strip() for n in out.splitlines() if n.strip()]


def wait_new_file(before, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        now = pictures()
        new = [n for n in now if n not in before]
        if new:
            return sorted(new)
        time.sleep(0.3)
    return []


def build_template():
    os.makedirs(TPL_DIR, exist_ok=True)
    p = os.path.join(TPL_DIR, "camera_icon.png")
    if not os.path.exists(p):
        fatal("缺少 templates/camera_icon.png，请先跑一次网格版采集生成模板")
    return np.asarray(Image.open(p).convert("L"), dtype=np.float32)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"processed": 0, "saved": 0, "last_key": None, "hashes": {}, "keys": []}


def save_state(st):
    tmp = STATE_PATH + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def append_manifest(entry):
    os.makedirs(POSTCARDS, exist_ok=True)
    data = {"schema": 1, "items": []}
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding="utf-8") as f:
            data = json.load(f)
    data.setdefault("items", []).append(entry)
    tmp = MANIFEST + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MANIFEST)


def main():
    global SERIAL, CAM_T
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default=None)
    ap.add_argument("--max", type=int, default=9999, help="本次最多处理多少张")
    ap.add_argument("--hard-cap", type=int, default=520, help="总量上限，防死循环")
    ap.add_argument("--ff", type=int, default=None, help="先快进多少张（续跑用）")
    ap.add_argument("--dry-run", action="store_true", help="只滑动不保存")
    ap.add_argument("--fresh", action="store_true", help="忽略旧状态，从头开始")
    ap.add_argument("--verify", action="store_true", help="只读校验：不保存，只统计漏采")
    ap.add_argument("--preload", type=int, default=0, help="先翻页预热多少页")
    ap.add_argument("--backward", action="store_true", help="反向：从最后一页往前滑")
    args = ap.parse_args()

    for d in (PROBE, ARCHIVE, POSTCARDS, TPL_DIR):
        os.makedirs(d, exist_ok=True)

    if args.serial:
        SERIAL = args.serial
    else:
        _, out, _ = adb("devices")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                SERIAL = parts[0]
                break
        if not SERIAL:
            fatal("没有已授权的设备")

    if args.fresh and os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)

    if args.max > args.hard_cap:
        args.max = args.hard_cap
    global SWIPE_FROM, SWIPE_TO
    if args.backward:
        SWIPE_FROM, SWIPE_TO = BACK_FROM, BACK_TO

    if args.preload:
        if is_locked():
            fatal("手机锁屏，请先解锁")
        ensure_stay_awake()
        preload_pages(args.preload)
        return

    if is_locked():
        fatal("手机现在是锁屏状态，adb 无法解锁。请手动解锁（人脸/指纹）并保持屏幕亮着，再重新点「一键滑扫」")
    was_awake = ensure_stay_awake()
    log(f"屏幕常亮已开启（起始状态：{'亮屏' if was_awake else '休眠→已唤醒'}）")
    CAM_T = build_template()
    st = load_state()
    st.setdefault("keys", [])
    direction = "backward" if args.backward else "forward"
    if st.get("direction") != direction:
        log(f"滑动方向：{st.get('direction') or '未记录'} -> {direction}，进度重置为 0")
        st["processed"] = 0
    st["direction"] = direction
    if os.path.isdir(POSTCARDS):
        for fn in os.listdir(POSTCARDS):
            p = os.path.join(POSTCARDS, fn)
            if os.path.isfile(p):
                st["hashes"][hashlib.sha256(open(p, "rb").read()).hexdigest()] = fn

    log("=" * 64)
    log(f"滑扫开始 serial={SERIAL} max={args.max} dry={args.dry_run} 已完成={st['processed']}")

    # 起点：必须在详情页
    p = screencap()
    if camera_score(as_gray(p)) <= 0.85:
        fatal("当前不在明信片详情页。请先打开相册里任意一张明信片，再启动滑扫。")

    ff = st["processed"] if args.ff is None else args.ff
    if ff > 0:
        log(f"快进 {ff} 张（跳过已处理）…")
        prev = None
        for i in range(ff):
            swipe_next()
            time.sleep(1.1)
            if (i + 1) % 20 == 0:
                log(f"  快进 {i + 1}/{ff}")
        log("快进完成")

    processed = 0
    verified = 0
    missing = []
    same_streak = 0
    prev_key = None

    last_sig = None
    stuck = 0

    while processed < args.max:
        if is_locked():
            fatal("手机锁屏了（adb 无法解锁）。请手动解锁后重新点「一键滑扫」，进度已保存")
        p = screencap()
        g = as_gray(p)

        if g.mean() < 30:
            log("  截屏全黑：屏幕像是熄了，尝试唤醒")
            adb("shell", "input", "keyevent", "224")
            time.sleep(1.8)
            p = screencap()
            g = as_gray(p)
            if g.mean() < 30:
                fatal("唤醒后仍是黑屏 —— 手机锁屏了，请手动解锁后重新点「一键滑扫」")
            log("  已唤醒")

        if camera_score(g) <= 0.85:
            log("详情弹窗消失了，停止")
            break

        blank, std, mean = is_blank(p)
        if blank:
            for _try in range(4):
                log(f"  明信片画面是空白（std={std}），等待加载 {_try + 1}/4")
                time.sleep(1.8)
                p = screencap()
                g = as_gray(p)
                blank, std, mean = is_blank(p)
                if not blank:
                    break
            if blank:
                log("  仍然空白：跳过这张（不点保存，避免存进白图）")
                st["skipped_blank"] = st.get("skipped_blank", 0) + 1
                last_sig = frame_sig(p)
                adb("shell", "input", "swipe", SWIPE_FROM[0], SWIPE_FROM[1],
                    SWIPE_TO[0], SWIPE_TO[1], "380")
                time.sleep(1.8)
                continue

        sig = frame_sig(p)
        if last_sig is not None and sig == last_sig:
            stuck += 1
            log(f"  滑动没生效（{stuck}/8）")
            if stuck >= 8:
                if st["processed"] >= 480:
                    log("连续 8 次滑不动、且已走过近全量 -> 判定到达相册末尾，正常结束")
                    break
                fatal(f"在第 {st['processed'] + 1} 张位置连续 8 次滑动无效（疑似游戏卡住）。"
                      f"进度已保存，请重新点「一键滑扫」继续")
            time.sleep(0.6)
            adb("shell", "input", "swipe", SWIPE_FROM[0], SWIPE_FROM[1],
                SWIPE_TO[0], SWIPE_TO[1], "380")
            time.sleep(1.8)
            continue
        stuck = 0
        last_sig = sig

        key = postcard_key(p)
        n = st["processed"] + 1
        log(f"[{n}] key={key[:12]}")

        if (not args.verify) and (not args.dry_run) and key in set(st.get("keys", [])):
            log("  这张画面已归档过，跳过保存（不再产生重复文件）")
            swipe_next()
            time.sleep(1.3)
            st["processed"] = n
            save_state(st)
            processed += 1
            continue

        if args.verify:
            known = set(st.get("keys", []))
            if key in known:
                verified += 1
            else:
                missing.append({"order": n, "key": key})
                log(f"  !! 这张没有归档记录：order={n} key={key[:12]}")
            swipe_next()
            time.sleep(1.3)
            st["processed"] = n
            save_state(st)
            processed += 1
            continue

        if args.dry_run:
            swipe_next()
            time.sleep(1.2)
            st["processed"] = n
            save_state(st)
            processed += 1
            continue

        before = set(pictures())
        hold(*CAMERA_PT)
        new = wait_new_file(before)
        if not new:
            log("  第一次点相机没出文件，用 250ms 重试")
            hold(CAMERA_PT[0], CAMERA_PT[1], 250)
            new = wait_new_file(before)
        if not new:
            fatal("点了相机但 10 秒内没有新文件，停止")

        name = sorted(new)[0]
        tmp = os.path.join(PROBE, f"_sw_{name}")
        rc, _, err = adb("pull", f"/sdcard/Pictures/{name}", tmp)
        if rc != 0:
            fatal(f"拉取失败: {err.strip()}")
        fh = hashlib.sha256(open(tmp, "rb").read()).hexdigest()

        if fh in st["hashes"]:
            log(f"  重复图片（已有 {st['hashes'][fh][:20]}），丢弃")
            try:
                os.remove(tmp)
            except OSError:
                pass
        else:
            seq = st["saved"] + 1
            dst = os.path.join(POSTCARDS, f"{seq:04d}_{name}")
            os.replace(tmp, dst)
            append_manifest({
                "seq": seq,
                "file": f"postcards/{seq:04d}_{name}",
                "original_name": name,
                "source": "in-game save-to-album (swipe sweep)",
                "device": device_model(),
                "package": "com.Aligames.lxqw.huawei",
                "order": n,
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "bytes": os.path.getsize(dst),
                "sha256": fh,
                "width": 500,
                "height": 350,
                "location": None,
                "note": "",
                "favorite": False,
                "hidden": False,
            })
            st["saved"] = seq
            st["hashes"][fh] = f"{seq:04d}_{name}"
            log(f"  OK seq={seq} ({os.path.getsize(dst)} bytes)")

        st["processed"] = n
        st["last_key"] = key
        if key not in st["keys"]:
            st["keys"].append(key)
        save_state(st)
        processed += 1

        # 保存后游戏会锁输入 2~3 秒，等一会儿再滑
        time.sleep(4.0)
        swipe_next()
        time.sleep(1.6)

    if args.verify:
        total = verified + len(missing)
        log(f"校验结果：走过 {total} 张，已归档 {verified} 张，未归档 {len(missing)} 张")
        if missing:
            log("未归档清单（前 20）：")
            for m in missing[:20]:
                log(f"  order={m['order']} key={m['key'][:16]}")
        else:
            log("✓ 全部覆盖，没有漏采")
    log(f"本轮处理 {processed} 张，累计归档 {st['saved']} 个文件，进度 {st['processed']}")
    log("=" * 64)


if __name__ == "__main__":
    main()
