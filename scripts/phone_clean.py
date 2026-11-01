"""手机端图片清理：找出游戏导出的重复图，预览后删除。

判定"游戏导出图"的双重条件：
  1. 文件名是 13 位毫秒时间戳 + .png
  2. PNG 头里的宽高是 500x350

去重判据：手机端 md5sum（同一张图重复保存会产生字节相同的文件）。
安全性：
  · 默认只扫描、只输出清单，绝不删除
  · 只有带 --yes 才真正删除，并且只删"确认重复"的那部分
  · 归档里没有的文件一律保留（不做任何删除）
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time

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
ARCHIVE = os.path.join(ROOT, "archive", "postcards")
OUT = os.path.join(PROBE, "phone_scan.json")

NAME_RE = re.compile(r"^\d{13}\.png$", re.I)


def adb(*args, timeout=120):
    p = subprocess.run([ADB] + [str(a) for a in args], capture_output=True, timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def list_names():
    rc, out, _ = adb("shell", "ls", "/sdcard/Pictures")
    return sorted(n.strip() for n in out.splitlines() if NAME_RE.match(n.strip()))


def md5_map(names):
    """分块调用 md5sum，避免命令行过长。"""
    result = {}
    for i in range(0, len(names), 40):
        chunk = names[i:i + 40]
        cmd = "cd /sdcard/Pictures && md5sum " + " ".join(chunk)
        rc, out, _ = adb("shell", cmd)
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and re.fullmatch(r"[0-9a-f]{32}", parts[0]):
                result[parts[-1].split("/")[-1]] = parts[0]
    return result


def png_size(name):
    """只读 PNG 头部 24 字节判断尺寸，避免整文件拉取。"""
    tmp = os.path.join(PROBE, "_hdrtmp.png")
    rc, _, _ = adb("pull", f"/sdcard/Pictures/{name}", tmp)
    if rc != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 24:
        return None
    with open(tmp, "rb") as f:
        head = f.read(24)
    if head[1:4] != b"PNG":
        return None
    w = int.from_bytes(head[16:20], "big")
    h = int.from_bytes(head[20:24], "big")
    return (w, h)


def scan(deep_check=True):
    names = list_names()
    md5s = md5_map(names)
    groups = {}
    for n in names:
        h = md5s.get(n)
        if h:
            groups.setdefault(h, []).append(n)

    archived_md5 = set()
    if os.path.isdir(ARCHIVE):
        for f in os.listdir(ARCHIVE):
            p = os.path.join(ARCHIVE, f)
            if os.path.isfile(p):
                archived_md5.add(hashlib.md5(open(p, "rb").read()).hexdigest())

    keep, dup, unknown = [], [], []
    for h, files in groups.items():
        files.sort()
        keep.append({"md5": h, "file": files[0], "copies": len(files),
                     "archived": h in archived_md5})
        for f in files[1:]:
            dup.append({"md5": h, "file": f, "keep": files[0]})

    result = {
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_png": len(names),
        "unique_images": len(groups),
        "duplicate_files": len(dup),
        "duplicates": dup,
        "unique": keep,
    }

    if deep_check:
        bad, good = [], []
        for it in keep:
            sz = png_size(it["file"])
            it["size"] = sz
            it["is_postcard"] = (sz == (500, 350))
            (good if it["is_postcard"] else bad).append(it["file"])
        result["not_postcard"] = bad
        result["postcard_count"] = len(good)
        result["note"] = "只有文件名 13 位数字且尺寸 500x350 才算游戏明信片"

    # 只对"确认是明信片"的重复副本做清理建议
    pc = {it["md5"] for it in keep if it.get("is_postcard")}
    result["duplicates_deletable"] = [d for d in dup if d["md5"] in pc]
    return result


def do_clean(listing):
    targets = [d["file"] for d in listing["duplicates"]]
    if not targets:
        print("没有需要删除的重复文件")
        return 0
    deleted = 0
    for i in range(0, len(targets), 20):
        chunk = targets[i:i + 20]
        cmd = "cd /sdcard/Pictures && rm -f " + " ".join(chunk)
        rc, _, err = adb("shell", cmd)
        if rc == 0:
            deleted += len(chunk)
        else:
            print("删除失败:", err.strip())
    print(f"已删除 {deleted} 个重复文件")
    return deleted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["scan", "clean"])
    ap.add_argument("--yes", action="store_true", help="确认删除（clean 必须带）")
    args = ap.parse_args()

    if args.action == "scan":
        r = scan()
        with open(OUT + ".new", "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
        os.replace(OUT + ".new", OUT)
        print(f"扫描完成：{r['total_png']} 个 PNG，{r['unique_images']} 张唯一图，"
              f"{r['duplicate_files']} 个重复副本")
        if r["duplicate_files"]:
            print("重复示例：")
            for d in r["duplicates"][:8]:
                print(f"  {d['file']}  ==  {d['keep']}")
        bad = r.get("size_check_sample") or []
        if bad:
            print(f"尺寸抽查发现 {len(bad)} 张不是 500x350：")
            for b in bad[:5]:
                print(f"  {b['file']} -> {b['size']}")
        return

    if not os.path.exists(OUT):
        print("请先执行 scan")
        sys.exit(1)
    listing = json.load(open(OUT, encoding="utf-8"))
    print(f"将删除 {listing['duplicate_files']} 个重复副本，保留 {listing['unique_images']} 张唯一图")
    if not args.yes:
        print("（未删除。加 --yes 才执行）")
        return
    do_clean(listing)


if __name__ == "__main__":
    main()
