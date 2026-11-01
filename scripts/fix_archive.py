"""修复归档序号：按采集顺序重命名为 0001..NNNN，并重建 manifest.json。

背景：调试期清空过 p1_state.json，导致序号回绕、出现两个 0001_ 前缀。
本脚本以"文件最后修改时间"为采集顺序依据，重排文件名与清单，幂等可重复跑。
"""
import hashlib
import json
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
POSTCARDS = os.path.join(ROOT, "archive", "postcards")
MANIFEST = os.path.join(ROOT, "archive", "manifest.json")
STATE = os.path.join(ROOT, "probe", "p1_state.json")

PAT = re.compile(r"^(\d{4})_(.+)$")


def main():
    files = [f for f in os.listdir(POSTCARDS) if f.lower().endswith(".png")]
    # 按 (原序号, 修改时间) 排序，尽量保留历史顺序
    def key(f):
        m = PAT.match(f)
        seq = int(m.group(1)) if m else 9999
        return (seq, os.path.getmtime(os.path.join(POSTCARDS, f)))

    files.sort(key=key)
    items = []
    plan = []
    for i, f in enumerate(files, 1):
        m = PAT.match(f)
        orig = m.group(2) if m else f
        new = f"{i:04d}_{orig}"
        if new != f:
            plan.append((f, new))
        full = os.path.join(POSTCARDS, f)
        h = hashlib.sha256(open(full, "rb").read()).hexdigest()
        items.append({
            "seq": i,
            "file": f"postcards/{new}",
            "original_name": orig,
            "source": "in-game save-to-album",
            "device": None,
            "package": "com.Aligames.lxqw.huawei",
            "captured_at": None,
            "bytes": os.path.getsize(full),
            "sha256": h,
            "width": 500,
            "height": 350,
            "location": None,
            "note": "",
            "favorite": False,
            "hidden": False,
        })

    print(f"发现 {len(files)} 个文件，需要改名 {len(plan)} 个")
    # 两步改名，避免 0001->0001 冲突
    tmpmap = []
    for old, new in plan:
        tmp = os.path.join(POSTCARDS, "_tmp_" + new)
        os.replace(os.path.join(POSTCARDS, old), tmp)
        tmpmap.append((tmp, new))
    for tmp, new in tmpmap:
        os.replace(tmp, os.path.join(POSTCARDS, new))

    # 用原 manifest 里已有的 captured_at 补回去（按 original_name 匹配）
    old_time = {}
    if os.path.exists(MANIFEST):
        try:
            d = json.load(open(MANIFEST, encoding="utf-8"))
            for it in d.get("items", []):
                if it.get("original_name"):
                    old_time[it["original_name"]] = it.get("captured_at")
        except Exception as e:
            print("读旧 manifest 失败:", e)
    for it in items:
        it["captured_at"] = old_time.get(it["original_name"]) or it["captured_at"]

    data = {
        "schema": 1,
        "created": "2026-10-18T19:00:00",
        "source": "旅行青蛙·中国之旅 / com.Aligames.lxqw.huawei",
        "device": None,
        "note": "postcards/ 下为游戏内「保存到相册」导出的原图，500x350 PNG，无裁剪无压缩",
        "count": len(items),
        "items": items,
    }
    tmp = MANIFEST + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MANIFEST)

    # 同步状态文件
    if os.path.exists(STATE):
        st = json.load(open(STATE, encoding="utf-8"))
    else:
        st = {"done": {}, "cards_saved": 0, "file_hashes": {}, "popup_keys": {}}
    st["cards_saved"] = len(items)
    st["file_hashes"] = {it["sha256"]: os.path.basename(it["file"]) for it in items}
    st.setdefault("popup_keys", {})
    st.setdefault("done", {})
    with open(STATE + ".new", "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(STATE + ".new", STATE)

    print(f"完成：{len(items)} 条，manifest 已重建")
    for it in items:
        print(f"  {it['seq']:04d}  {os.path.basename(it['file'])}  {it['bytes']} bytes  {it['sha256'][:12]}")


if __name__ == "__main__":
    main()
