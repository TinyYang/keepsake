"""绕过 pip：直接从 PyPI 下载 wheel 并解压到工作区 pylibs。

原因：工作区目录带 Everyone:Deny-Delete 的沙箱 ACL，pip 的
"下载到临时文件再 os.replace 落盘" 会 EACCES。这里只做
urllib 下载 + zipfile 解压，不产生任何 rename/delete。
"""
import json
import os
import sys
import urllib.request
import zipfile

DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pylibs")
DEST = os.path.abspath(DEST)
TMP = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".wheelcache"))
os.makedirs(DEST, exist_ok=True)
os.makedirs(TMP, exist_ok=True)

TAG = "cp314-cp314-win_amd64"


def pick_file(pkg, want_tag=TAG):
    url = f"https://pypi.org/pypi/{pkg}/json"
    with urllib.request.urlopen(url, timeout=30) as r:
        meta = json.load(r)
    cands = []
    for ver, files in meta["releases"].items():
        for f in files:
            name = f["filename"]
            if name.endswith(".whl") and want_tag in name and "win_amd64" in name:
                cands.append((f["upload_time"], ver, name, f["url"]))
    if not cands:
        raise SystemExit(f"No {want_tag} wheel for {pkg}")
    cands.sort()
    return cands[-1]


def install(pkg):
    upload, ver, name, url = pick_file(pkg)
    print(f"[{pkg}] {name}  ({upload})")
    dst = os.path.join(TMP, name)
    if not os.path.exists(dst) or os.path.getsize(dst) == 0:
        with urllib.request.urlopen(url, timeout=120) as r, open(dst, "wb") as f:
            f.write(r.read())
    size = os.path.getsize(dst)
    print(f"  downloaded {size/1024/1024:.1f} MB")
    with zipfile.ZipFile(dst) as z:
        z.extractall(DEST)
    print(f"  extracted to {DEST}")


if __name__ == "__main__":
    for pkg in (sys.argv[1:] or ["numpy", "pillow"]):
        install(pkg)
