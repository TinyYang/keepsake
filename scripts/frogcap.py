"""P1 明信片批量采集器 v3（PC + adb，L1 图像匹配）。

实测结论（P0 勘察 + v1/v2 试跑）
--------------------------------
1. `input tap` 按压时间太短，Unity 引擎会当成无效手势吞掉。
   -> 全部改用 `input swipe x y x y 160`（160ms 按住）。
2. 保存后弹窗不会自动关闭，必须点右上角 X，并且要验证关掉了。
3. 同一张卡片可以被重复保存，产生字节完全相同的 PNG。
   -> 先对详情页的明信片画面做哈希，已归档过的直接跳过，不点相机、不产生垃圾。
4. 误触垃圾桶会弹"确认删除图片吗？"，此时相机图标仍可见、位置不变。
   -> 检测浅黄确认面板，命中立即停手，绝不点"是"。
5. 任何一步与预期不符（打不开卡片 / 关不掉弹窗 / 危险框）→ 立刻停，不乱点。
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
STATE_PATH = os.path.join(PROBE, "p1_state.json")
LOG_PATH = os.path.join(PROBE, "p1_log.txt")
SHOT_PATH = os.path.join(PROBE, "_shot.png")

# ---- 设备常量（P0 实测，屏幕 1316x2832）----
SLOTS = [(388, 955), (980, 955), (388, 1370), (980, 1370), (388, 1790), (980, 1790)]
CAMERA_BOX = (820, 1765, 982, 1977)
CAMERA_PT = (900, 1871)
CLOSE_PT = (1202, 912)
NEXT_PT = (1199, 1409)                    # 网格页右侧翻页箭头
PAGE_CROP = (850, 2650, 1250, 2800)       # 右下角 "n / 83" 页码区
POSTCARD_CROP = (170, 1010, 1140, 1690)   # 详情页里的明信片画面区域
HOLD_MS = 160

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


def hold(x, y, ms=HOLD_MS):
    rc, _, err = adb("shell", "input", "swipe", x, y, x, y, ms)
    if rc != 0:
        fatal(f"input swipe ({x},{y}) 失败: {err.strip()}")


def screencap(path=SHOT_PATH):
    cmd = [ADB] + (["-s", SERIAL] if SERIAL else []) + ["exec-out", "screencap", "-p"]
    with open(path, "wb") as f:
        p = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, timeout=60)
    if p.returncode != 0 or os.path.getsize(path) < 10000:
        fatal("截屏失败（文件过小），可能屏幕已休眠")
    return path


def as_gray(path):
    return np.asarray(Image.open(path).convert("L"), dtype=np.float32)


def as_rgb(path):
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)


def pictures():
    rc, out, err = adb("shell", "ls", "/sdcard/Pictures")
    if rc != 0:
        fatal(f"列目录失败: {err.strip()}")
    return [n.strip() for n in out.splitlines() if n.strip()]


def ncc(win, tpl):
    t = tpl - tpl.mean()
    w = win - win.mean()
    d = (np.sqrt((w * w).sum()) + 1e-6) * (np.sqrt((t * t).sum()) + 1e-6)
    return float((w * t).sum() / d)


def camera_score(g):
    th, tw = CAM_T.shape
    x0, y0 = CAMERA_BOX[0], CAMERA_BOX[1]
    return ncc(g[y0:y0 + th, x0:x0 + tw], CAM_T)


def danger_panel(rgb):
    """“确认删除图片吗？”面板是近乎纯色的浅黄，正常详情页该区域是彩色插画。"""
    reg = rgb[1330:1470, 480:860]
    m = reg.mean(axis=(0, 1))
    std = float(reg.std())
    return (m[0] > 235 and m[1] > 220 and m[2] < 215 and std < 18), std


def postcard_key(path):
    """详情页里明信片画面的像素哈希，用来判断"是不是同一张卡"。"""
    im = Image.open(path).convert("RGB").crop(POSTCARD_CROP)
    return hashlib.sha256(im.tobytes()).hexdigest()


def build_template():
    os.makedirs(TPL_DIR, exist_ok=True)
    p = os.path.join(TPL_DIR, "camera_icon.png")
    if not os.path.exists(p):
        src = os.path.join(PROBE, "detail.png")
        if not os.path.exists(src):
            fatal("缺少 probe/detail.png")
        Image.open(src).convert("L").crop(CAMERA_BOX).save(p)
        log(f"生成模板 {p}")
    return np.asarray(Image.open(p).convert("L"), dtype=np.float32)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"done": {}, "cards_saved": 0, "file_hashes": {}, "popup_keys": {}}


def save_state(st):
    tmp = STATE_PATH + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def append_manifest(entry):
    os.makedirs(POSTCARDS, exist_ok=True)
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"schema": 1, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "source": "旅行青蛙·中国之旅 / com.Aligames.lxqw.huawei", "items": []}
    data["items"].append(entry)
    tmp = MANIFEST + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MANIFEST)


def ensure_ready():
    rc, out, _ = adb("devices")
    if SERIAL not in out:
        fatal("设备不在线")
    rc, out, _ = adb("shell", "dumpsys", "power")
    if "mWakefulness=Awake" not in out:
        fatal("屏幕休眠，请点亮并解锁手机")
    rc, out, _ = adb("shell", "dumpsys", "window")
    if "com.Aligames.lxqw.huawei" not in out:
        fatal("游戏不在前台")


def probe_state(path=None):
    """返回 (kind, info)。kind ∈ grid / detail / danger / black"""
    p = path or screencap()
    g = as_gray(p)
    if g.mean() < 30:
        return "black", {"score": 0.0}
    rgb = as_rgb(p)
    d, std = danger_panel(rgb)
    if d:
        return "danger", {"std": round(std, 1)}
    cs = camera_score(g)
    if cs > 0.85:
        return "detail", {"score": round(cs, 3), "key": postcard_key(p)}
    return "grid", {"score": round(cs, 3)}


def stable_detail(cam_tpl, tries=3, gap=0.7):
    """等明信片画面渲染稳定：连续两次截屏哈希一致才算数。

    弹窗相框和相机图标是立刻出现的，但画面本身有淡入，
    过早截图会拿到上一张的残影 —— 会导致误判重复。
    """
    last = None
    for i in range(tries):
        p = screencap()
        g = as_gray(p)
        cs = camera_score(g)
        if cs <= 0.85:
            return None
        key = postcard_key(p)
        if last is not None and key == last:
            return {"score": round(cs, 3), "key": key, "stable_after": i}
        last = key
        time.sleep(gap)
    return {"score": 1.0, "key": last, "stable_after": tries}


def open_card(slot_pos, label, prev_key=None):
    """打开一张卡片，并保证它是「新的一张」。

    返回 (ok, info)。ok=False 表示这一格反复拿不到新卡片（可能点按失灵，
    也可能真的是同一张画面），调用方应记为 uncertain 后继续。
    """
    x, y = slot_pos
    for attempt in (1, 2, 3):
        hold(x, y, 250)          # 第一击：选中
        time.sleep(0.9)
        hold(x, y, 160)          # 第二击：打开
        time.sleep(1.2)

        kind, info = probe_state()
        if kind == "danger":
            fatal("出现危险确认框，停止（需人工确认界面）")

        if kind != "detail":
            log(f"  开卡 {label} #{attempt} -> {kind}（没打开）")
            time.sleep(0.6)
            continue

        st = stable_detail(None)
        if not st:
            log(f"  开卡 {label} #{attempt} -> 弹窗不稳定")
            close_detail()
            continue

        if prev_key and st["key"] == prev_key:
            log(f"  开卡 {label} #{attempt} -> 又是上一张，关掉重试")
            close_detail()
            time.sleep(0.5)
            continue

        log(f"  开卡 {label} #{attempt} -> OK key={st['key'][:12]}")
        return True, st

    return False, {}


def close_detail(wait_first=0.0):
    """点 X 关闭详情弹窗。

    实测：保存动作完成后游戏会阻塞输入约 2~3 秒（PNG 编码 + 媒体库扫描），
    这段时间点 X 无效。所以保存后先等 wait_first 秒再点。
    """
    if wait_first:
        time.sleep(wait_first)
    for attempt in (1, 2):
        hold(*CLOSE_PT)
        time.sleep(1.2)
        kind, info = probe_state()
        log(f"  关窗 #{attempt} -> {kind} {info}")
        if kind == "grid":
            return True
        if kind == "danger":
            fatal("关窗时出现危险确认框，停止")
        time.sleep(1.8)
    return False


def page_hash(path=None):
    p = path or screencap()
    im = Image.open(p).convert("RGB").crop(PAGE_CROP)
    return hashlib.sha256(im.tobytes()).hexdigest()[:16]


def next_page(cur_hash):
    """点右侧箭头翻页，用页码区像素变化确认真的翻了。"""
    hold(*NEXT_PT)
    time.sleep(1.5)
    p = screencap()
    h = page_hash(p)
    if h == cur_hash:
        return False, cur_hash
    kind, info = probe_state(p)
    if kind != "grid":
        fatal(f"翻页后界面不是网格页（{kind}），停止")
    return True, h


def wait_new_file(before, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        now = pictures()
        new = [n for n in now if n not in before]
        if new:
            return new
        time.sleep(0.3)
    return []


def main():
    global SERIAL, CAM_T
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default=None)
    ap.add_argument("--cards", type=int, default=1, help="本次处理多少张卡片")
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--max-page", type=int, default=83, help="最多翻到第几页")
    ap.add_argument("--start-page", type=int, default=1, help="游戏当前停在第几页")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for d in (PROBE, ARCHIVE, POSTCARDS, TPL_DIR):
        os.makedirs(d, exist_ok=True)

    if args.serial:
        SERIAL = args.serial
    else:
        rc, out, _ = adb("devices")
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                SERIAL = parts[0]
                break
        if not SERIAL:
            fatal("没有 device 状态的设备")

    log("=" * 64)
    log(f"v3 开始 serial={SERIAL} cards={args.cards} start={args.start} dry={args.dry_run}")
    ensure_ready()
    CAM_T = build_template()

    st = load_state()
    if os.path.isdir(POSTCARDS):
        for fn in os.listdir(POSTCARDS):
            p = os.path.join(POSTCARDS, fn)
            if os.path.isfile(p):
                st["file_hashes"][hashlib.sha256(open(p, "rb").read()).hexdigest()] = fn

    kind, info = probe_state()
    log(f"起始状态 {kind} {info}")
    if kind == "detail":
        if not close_detail():
            fatal("无法从详情页退出")
    elif kind == "danger":
        fatal("起始时就有危险确认框，请人工处理")

    processed = 0
    idx = args.start
    last_key = None
    same_key_streak = 0
    empty_streak = 0
    cur_hash = page_hash()
    cur_page = args.start_page
    while processed < args.cards:
        slot = (idx - 1) % 6
        page = (idx - 1) // 6 + 1

        if page > args.max_page:
            log(f"已到 --max-page={args.max_page}，结束本轮")
            break

        while cur_page < page:
            log(f"--- 翻到第 {cur_page + 1} 页 ---")
            ok, cur_hash = next_page(cur_hash)
            if not ok:
                log("页码没有变化，判定已到相册末尾，正常结束")
                log(f"本轮处理 {processed} 张，累计归档 {st['cards_saved']} 张")
                return
            cur_page += 1
            log(f"--- 第 {cur_page} 页 ---")

        if str(idx) in st["done"]:
            idx += 1
            continue

        ok, info = open_card(SLOTS[slot], f"slot{slot + 1}", prev_key=last_key)
        if not ok:
            empty_streak += 1
            log(f"  该格子拿不到新卡片（连续 {empty_streak} 次）")
            st["done"][str(idx)] = {"file": None, "seq": None, "skipped": "uncertain"}
            save_state(st)
            if empty_streak >= 6:
                fatal("连续 6 个格子都拿不到新卡片，判定点按全面失灵，停止")
            processed += 1
            idx += 1
            continue
        empty_streak = 0

        key = info.get("key")
        last_key = key

        if args.dry_run:
            log("  [dry-run] 不点相机")
            close_detail()
            st["done"][str(idx)] = {"file": None, "seq": None, "skipped": "dry_run"}
            save_state(st)
            processed += 1
            idx += 1
            continue

        before = set(pictures())
        hold(*CAMERA_PT)
        new = wait_new_file(before)
        if not new:
            log("  相机未产生文件，用 250ms 长按重试")
            hold(CAMERA_PT[0], CAMERA_PT[1], 250)
            new = wait_new_file(before)
        if not new:
            fatal("点了相机但 8 秒内没有新文件，停止")

        name = sorted(new)[0]
        tmp = os.path.join(PROBE, f"_new_{name}")
        rc, _, err = adb("pull", f"/sdcard/Pictures/{name}", tmp)
        if rc != 0:
            fatal(f"pull 失败: {err.strip()}")
        fh = hashlib.sha256(open(tmp, "rb").read()).hexdigest()
        if fh in st["file_hashes"]:
            log(f"  文件哈希重复（{fh[:12]}），丢弃并跳过")
            try:
                os.remove(tmp)
            except OSError:
                pass
            st["done"][str(idx)] = {"file": name, "seq": st["file_hashes"][fh], "skipped": "dup_file"}
            if key:
                st["popup_keys"][key] = st["file_hashes"][fh]
            save_state(st)
            if not close_detail(wait_first=2.6):
                fatal("关不掉详情页，停止")
            processed += 1
            idx += 1
            continue

        seq = st["cards_saved"] + 1
        local = os.path.join(POSTCARDS, f"{seq:04d}_{name}")
        os.replace(tmp, local)
        append_manifest({
            "seq": seq,
            "file": f"postcards/{seq:04d}_{name}",
            "original_name": name,
            "source": "in-game save-to-album",
            "device": device_model(),
            "package": "com.Aligames.lxqw.huawei",
            "page": page,
            "slot": slot + 1,
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "bytes": os.path.getsize(local),
            "sha256": fh,
            "width": 500,
            "height": 350,
            "location": None,
            "note": "",
            "favorite": False,
            "hidden": False,
        })
        st["cards_saved"] = seq
        st["file_hashes"][fh] = f"{seq:04d}_{name}"
        if key:
            st["popup_keys"][key] = seq
        st["done"][str(idx)] = {"file": name, "seq": seq}
        save_state(st)
        log(f"  OK seq={seq} {name} sha={fh[:12]}")
        last_key = key
        same_key_streak = 1

        if not close_detail(wait_first=2.6):
            fatal("关不掉详情页，停止")
        processed += 1
        idx += 1

    log(f"本轮处理 {processed} 张，累计归档 {st['cards_saved']} 张")
    log("=" * 64)


if __name__ == "__main__":
    main()
