"""《旅行青蛙》明信片抢救工具 —— 本地服务端。

只监听 127.0.0.1，不联网。启动后自动打开浏览器。
页面上可以：开始/停止采集、看手机实时画面、看进度与日志、翻已归档的图。

用法（由 start.cmd 调用）：
    python scripts/server.py
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "pylibs"))
PYLIB = os.path.join(ROOT, "pylibs")
SCRIPTS = os.path.join(ROOT, "scripts")
PROBE = os.path.join(ROOT, "probe")
ARCHIVE = os.path.join(ROOT, "archive")
POSTCARDS = os.path.join(ARCHIVE, "postcards")
UI = os.path.join(ROOT, "ui", "index.html")
STATE = os.path.join(PROBE, "p1_state.json")
MANIFEST = os.path.join(ARCHIVE, "manifest.json")
LOG = os.path.join(PROBE, "p1_log.txt")
NAV = os.path.join(PROBE, "nav.json")
SWEEP_LOG = os.path.join(PROBE, "sweep_log.txt")

# 内置默认路径（归一化比例，与分辨率无关）：庭院大门 -> 屋内门 -> 菜单相册
# 来自 1316x2832 实测录制；别的机型按比例换算，仍需「相册页」校验兜底。
PRESET_NORM = [(0.617, 0.428), (0.385, 0.503), (0.500, 0.367)]
PHONE_CACHE = os.path.join(PROBE, "phone_cache")
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
_MODEL = {"v": None}
_LAST = {"state": "unknown", "score": 0.0, "ts": 0.0}
_SHOT_LOCK = threading.Lock()
_SHOT_TS = {"t": 0.0}

# adb 启动守护进程时要写 %TEMP%\adb.log；某些受限环境下 %TEMP% 不可写，
# 会导致 "cannot open ...adb.log: Permission denied" 而永远连不上设备。
# 统一把临时目录指到工作区内的可写目录。
_ADB_TMP = os.path.join(ROOT, "tmp")
os.makedirs(_ADB_TMP, exist_ok=True)
os.environ["TMP"] = _ADB_TMP
os.environ["TEMP"] = _ADB_TMP


PORT = int(os.environ.get("FROG_PORT", "8787"))


class Job:
    def __init__(self):
        self.proc = None
        self.args = {}
        self.lock = threading.Lock()

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, cards, max_page, start, dry, start_page=None):
        with self.lock:
            if self.running:
                return False, "已经在跑了"
            env = os.environ.copy()
            env["PYTHONPATH"] = PYLIB + os.pathsep + env.get("PYTHONPATH", "")
            env["PYTHONIOENCODING"] = "utf-8"
            if start_page is None:
                start_page = (int(start) - 1) // 6 + 1
            cmd = [sys.executable, os.path.join(SCRIPTS, "frogcap.py"),
                   "--cards", str(cards), "--max-page", str(max_page), "--start", str(start),
                   "--start-page", str(start_page)]
            if dry:
                cmd.append("--dry-run")
            self.proc = subprocess.Popen(cmd, cwd=ROOT, env=env,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.args = {"cards": cards, "max_page": max_page, "start": start, "dry": dry,
                         "started": time.strftime("%H:%M:%S")}
            return True, "已启动"

    def start_sweep(self, maxn, backward=False):
        with self.lock:
            if self.running:
                return False, "已经在跑了"
            env = os.environ.copy()
            env["PYTHONPATH"] = PYLIB + os.pathsep + env.get("PYTHONPATH", "")
            env["PYTHONIOENCODING"] = "utf-8"
            cmd = [sys.executable, os.path.join(SCRIPTS, "swipecap.py"), "--max", str(maxn)]
            if backward:
                cmd.append("--backward")
            self.proc = subprocess.Popen(cmd, cwd=ROOT, env=env,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.args = {"mode": "sweep", "max": maxn, "backward": backward,
                         "started": time.strftime("%H:%M:%S")}
            return True, "滑扫已启动"

    def start_verify(self):
        with self.lock:
            if self.running:
                return False, "已经在跑了"
            env = os.environ.copy()
            env["PYTHONPATH"] = PYLIB + os.pathsep + env.get("PYTHONPATH", "")
            env["PYTHONIOENCODING"] = "utf-8"
            cmd = [sys.executable, os.path.join(SCRIPTS, "swipecap.py"), "--verify", "--max", "520"]
            self.proc = subprocess.Popen(cmd, cwd=ROOT, env=env,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.args = {"mode": "sweep", "verify": True, "started": time.strftime("%H:%M:%S")}
            return True, "校验已启动"

    def stop(self):
        with self.lock:
            if self.running:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                return True
            return False


JOB = Job()


def adb_run(cmd, timeout=120):
    try:
        p = subprocess.run([ADB, "shell", cmd], capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except Exception as e:
        return -1, "", str(e)


def adb_out(args, timeout=30):
    try:
        p = subprocess.run([ADB] + args, capture_output=True, timeout=timeout)
        return p.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


def device_model():
    if _MODEL["v"] is None:
        _MODEL["v"] = adb_out(["shell", "getprop", "ro.product.model"]).strip() or "unknown"
    return _MODEL["v"]


def screen_size():
    out = adb_out(["shell", "wm", "size"])
    for line in out.splitlines():
        if "Physical size:" in line:
            try:
                w, h = line.split(":")[1].strip().split("x")
                return int(w), int(h)
            except Exception:
                pass
    return 1316, 2832


def resolve_steps():
    """返回 (steps, source)。优先用户录制，其次内置预设。"""
    if os.path.exists(NAV):
        try:
            st = json.load(open(NAV, encoding="utf-8")).get("steps", [])
            if st:
                return st, "user"
        except Exception:
            pass
    w, h = screen_size()
    return [{"x": int(round(nx * w)), "y": int(round(ny * h)), "wait": 1.4}
            for nx, ny in PRESET_NORM], "preset"


def read_state():
    if not os.path.exists(STATE):
        return {"cards_saved": 0, "done": {}}
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"cards_saved": 0, "done": {}}


def first_missing(state):
    done = state.get("done", {})
    i = 1
    while str(i) in done:
        i += 1
    return i


def tail(path, n=120):
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-n:]
    except Exception:
        return []


def current_page():
    """从日志里抓最后一次出现的页码。"""
    for line in reversed(tail(LOG, 400)):
        if "第 " in line and " 页" in line:
            try:
                return int(line.split("第 ")[1].split(" 页")[0])
            except Exception:
                pass
    return 1


class Handler(BaseHTTPRequestHandler):
    server_version = "FrogRescue/1.0"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        q = urllib.parse.parse_qs(u.query)

        if p in ("/", "/index.html"):
            if not os.path.exists(UI):
                return self._send(500, "ui/index.html 缺失", "text/plain; charset=utf-8")
            with open(UI, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")

        if p == "/api/status":
            st = read_state()
            saved = None
            if os.path.exists(MANIFEST):
                try:
                    saved = len(json.load(open(MANIFEST, encoding="utf-8")).get("items", []))
                except Exception:
                    saved = None
            if saved is None:
                saved = st.get("cards_saved", 0)
            return self._json({
                "running": JOB.running,
                "saved": saved,
                "done": len(st.get("done", {})),
                "page": current_page(),
                "total_pages": 83,
                "args": JOB.args,
                "log": tail(SWEEP_LOG if JOB.args.get("mode") == "sweep" else LOG, 60),
                "device": self._device_ok(),
                "model": device_model(),
                "server_pid": os.getpid(),
                "screen": _LAST,
                "sort": _LAST.get("sort", "unknown"),
            })

        if p == "/api/items":
            if not os.path.exists(MANIFEST):
                return self._json({"items": []})
            try:
                with open(MANIFEST, encoding="utf-8") as f:
                    d = json.load(f)
                items = d.get("items", [])
                return self._json({"items": items[::-1], "count": len(items)})
            except Exception as e:
                return self._json({"items": [], "error": str(e)})

        if p == "/api/archive_info":
            size = 0
            n = 0
            for dirpath, _, files in os.walk(ARCHIVE):
                for f in files:
                    size += os.path.getsize(os.path.join(dirpath, f))
                    n += 1
            zips = []
            exp = os.path.join(ROOT, "export")
            if os.path.isdir(exp):
                for f in sorted(os.listdir(exp), reverse=True):
                    if f.endswith(".zip"):
                        zips.append({"name": f, "bytes": os.path.getsize(os.path.join(exp, f))})
            return self._json({"path": ARCHIVE, "files": n, "bytes": size, "zips": zips[:10]})

        if p == "/api/download":
            name = (q.get("f") or [""])[0]
            name = os.path.basename(name)
            full = os.path.join(ROOT, "export", name)
            if os.path.exists(full):
                with open(full, "rb") as fh:
                    data = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            return self._send(404, b"", "application/zip")

        if p == "/api/phone_list":
            scan_p = os.path.join(PROBE, "phone_scan.json")
            if os.path.exists(scan_p):
                try:
                    d = json.load(open(scan_p, encoding="utf-8"))
                    items = [it for it in d.get("unique", []) if it.get("is_postcard")]
                    names = [it["file"] for it in items]
                    return self._json({"path": "/sdcard/Pictures/", "items": names,
                                       "count": len(names), "filtered": True,
                                       "not_postcard": d.get("not_postcard", [])})
                except Exception:
                    pass
            out = adb_out(["shell", "ls", "/sdcard/Pictures"])
            import re as _re
            names = [n.strip() for n in out.splitlines()
                     if _re.fullmatch(r"\d{13}\.png", n.strip(), _re.I)]
            names.sort(reverse=True)
            return self._json({"path": "/sdcard/Pictures/", "items": names[:80], "count": len(names),
                               "filtered": False})

        if p == "/api/phone_dups":
            scan_p = os.path.join(PROBE, "phone_scan.json")
            if not os.path.exists(scan_p):
                return self._json({"ok": False, "msg": "还没有扫描过，请先点「扫描手机重复图」"})
            d = json.load(open(scan_p, encoding="utf-8"))
            groups = {}
            for it in d.get("duplicates_deletable", []):
                g = groups.setdefault(it["md5"], {"md5": it["md5"], "keep": it["keep"], "dups": []})
                g["dups"].append(it["file"])
            return self._json({"ok": True, "groups": list(groups.values()),
                               "total_dups": sum(len(g["dups"]) for g in groups.values())})

        if p == "/api/phone_img":
            name = os.path.basename((q.get("f") or [""])[0])
            if not name:
                return self._send(404, b"", "image/png")
            os.makedirs(PHONE_CACHE, exist_ok=True)
            cached = os.path.join(PHONE_CACHE, name)
            if not os.path.exists(cached):
                subprocess.run([ADB, "pull", f"/sdcard/Pictures/{name}", cached],
                               capture_output=True, timeout=60)
            if os.path.exists(cached):
                with open(cached, "rb") as fh:
                    return self._send(200, fh.read(), "image/png")
            return self._send(404, b"", "image/png")

        if p == "/api/diag":
            import platform
            info = {"adb_path": ADB, "adb_exists": os.path.exists(ADB),
                    "server_pid": os.getpid(),
                "screen": _LAST,
                "sort": _LAST.get("sort", "unknown"), "cwd": os.getcwd(),
                    "python": sys.executable, "port": PORT,
                    "host": platform.node()}
            try:
                q2 = subprocess.run([ADB, "devices", "-l"], capture_output=True, timeout=25)
                info["rc"] = q2.returncode
                info["stdout"] = q2.stdout.decode("utf-8", "replace").strip()
                info["stderr"] = q2.stderr.decode("utf-8", "replace").strip()
            except Exception as e:
                info["rc"] = -1
                info["stdout"] = ""
                info["stderr"] = f"{type(e).__name__}: {e}"
            return self._json(info)

        if p == "/api/nav":
            user_steps = []
            if os.path.exists(NAV):
                try:
                    user_steps = json.load(open(NAV, encoding="utf-8")).get("steps", [])
                except Exception:
                    user_steps = []
            eff, source = resolve_steps()
            return self._json({"steps": user_steps, "effective": eff,
                               "source": source, "count": len(eff)})

        if p == "/api/shot":
            f = os.path.join(PROBE, "_live.png")
            now_ts = time.time()
            got = False
            force = (q.get("force") or ["0"])[0] == "1"
            fresh_enough = (not force) and os.path.exists(f) and (now_ts - _SHOT_TS["t"] < 1.2)
            if not fresh_enough and _SHOT_LOCK.acquire(blocking=False):
                try:
                    with open(f, "wb") as fh:
                        r = subprocess.run([ADB, "exec-out", "screencap", "-p"],
                                           stdout=fh, stderr=subprocess.PIPE, timeout=30)
                        got = r.returncode == 0 and os.path.getsize(f) > 10000
                    if got:
                        _SHOT_TS["t"] = time.time()
                except Exception:
                    got = False
                finally:
                    _SHOT_LOCK.release()
            if got:
                try:
                    self._classify(f)
                except Exception:
                    pass
            if os.path.exists(f):
                with open(f, "rb") as fh:
                    return self._send(200, fh.read(), "image/png")
            return self._send(404, b"", "image/png")

        if p == "/api/img":
            name = (q.get("f") or [""])[0]
            name = os.path.basename(name)
            full = os.path.join(POSTCARDS, name)
            if os.path.exists(full):
                with open(full, "rb") as fh:
                    return self._send(200, fh.read(), "image/png")
            return self._send(404, b"", "image/png")

        return self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else "{}"
        try:
            body = json.loads(raw or "{}")
        except Exception:
            body = {}

        if u.path == "/api/start":
            self._stay_awake()
            # 若已经在相册页，先退回上一级，再按录制路径重新进入，
            # 这样保证一定停在相册第 1 页（否则页码推算会错位）。
            if self._album_score() > 0.7:
                subprocess.run([ADB, "shell", "input", "swipe",
                                "110", "180", "110", "180", "160"],
                               capture_output=True, timeout=30)
                time.sleep(1.6)
            nav_steps, _nav_src = resolve_steps()
            entered = False
            if nav_steps:
                for s in nav_steps:
                    subprocess.run([ADB, "shell", "input", "swipe",
                                    str(int(s["x"])), str(int(s["y"])), str(int(s["x"])), str(int(s["y"])), "160"],
                                   capture_output=True, timeout=30)
                    time.sleep(float(s.get("wait", 1.2)))
                if self._album_score() <= 0.7:
                    return self._json({"ok": False, "msg": "自动进相册失败：请把游戏切到庭院后重试，或重新录制路径"})
                ok_sort, how = self._ensure_grid_sort()
                if not ok_sort:
                    return self._json({"ok": False, "msg": "无法切换到「网格」排序，请手动点一下再开始"})
                entered = True
            st = read_state()
            start = body.get("start")
            if not start:
                start = first_missing(st)
            cards = int(body.get("cards") or 9999)
            max_page = int(body.get("max_page") or 83)
            dry = bool(body.get("dry"))
            ok, msg = JOB.start(cards, max_page, int(start), dry, (1 if entered else None))
            return self._json({"ok": ok, "msg": msg, "start": int(start)})

        if u.path == "/api/export_zip":
            import zipfile
            exp = os.path.join(ROOT, "export")
            os.makedirs(exp, exist_ok=True)
            st = read_state()
            n = st.get("cards_saved", 0)
            name = f"frog-postcards-{n}cards-{time.strftime('%Y%m%d-%H%M%S')}.zip"
            full = os.path.join(exp, name)
            count = 0
            with zipfile.ZipFile(full, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                for dirpath, _, files in os.walk(ARCHIVE):
                    for f in sorted(files):
                        src = os.path.join(dirpath, f)
                        rel = os.path.relpath(src, os.path.dirname(ARCHIVE))
                        z.write(src, rel)
                        count += 1
            return self._json({"ok": True, "name": name, "files": count,
                               "bytes": os.path.getsize(full)})

        if u.path == "/api/open_folder":
            target = body.get("target") or ARCHIVE
            target = os.path.abspath(target)
            if not os.path.isdir(target):
                return self._json({"ok": False, "msg": "目录不存在"})
            subprocess.Popen(["explorer", target])
            return self._json({"ok": True, "path": target})

        if u.path == "/api/tap":
            try:
                x, y = int(float(body.get("x"))), int(float(body.get("y")))
            except Exception:
                return self._json({"ok": False, "msg": "坐标无效"})
            if not (0 <= x < 2000 and 0 <= y < 4000):
                return self._json({"ok": False, "msg": f"坐标越界 ({x},{y})"})
            r = subprocess.run([ADB, "shell", "input", "swipe", str(x), str(y), str(x), str(y), "160"],
                               capture_output=True, timeout=30)
            time.sleep(float(body.get("wait", 1.2)))
            return self._json({"ok": r.returncode == 0, "x": x, "y": y, "rc": r.returncode,
                               "err": r.stderr.decode("utf-8", "replace").strip()})

        if u.path == "/api/nav_reset":
            if os.path.exists(NAV):
                os.remove(NAV)
            return self._json({"ok": True})

        if u.path == "/api/nav_save":
            steps = body.get("steps") or []
            with open(NAV + ".new", "w", encoding="utf-8") as f:
                json.dump({"steps": steps, "saved": time.strftime("%Y-%m-%dT%H:%M:%S")}, f, ensure_ascii=False, indent=2)
            os.replace(NAV + ".new", NAV)
            return self._json({"ok": True, "count": len(steps)})

        if u.path == "/api/launch":
            subprocess.run([ADB, "shell", "monkey", "-p", "com.Aligames.lxqw.huawei",
                            "-c", "android.intent.category.LAUNCHER", "1"],
                           capture_output=True, timeout=30)
            time.sleep(float(body.get("wait", 8)))
            return self._json({"ok": True})

        if u.path == "/api/goto_album":
            if self._album_score() > 0.7:
                return self._json({"ok": True, "score": 1.0, "steps": 0, "source": "already", "msg": "已经在相册页"})
            steps, source = resolve_steps()
            for s in steps:
                subprocess.run([ADB, "shell", "input", "swipe",
                                str(int(s["x"])), str(int(s["y"])), str(int(s["x"])), str(int(s["y"])), "160"],
                               capture_output=True, timeout=30)
                time.sleep(float(s.get("wait", 1.2)))
            score = self._album_score()
            return self._json({"ok": score > 0.7, "score": round(score, 3), "steps": len(steps), "source": source,
                               "msg": "已在相册页" if score > 0.7 else "没到相册页，请重新录制路径"})

        if u.path == "/api/sweep":
            maxn = int(body.get("max") or 9999)
            self._stay_awake()
            preload = bool(body.get("preload", True))
            if not self._cold_to_album():
                return self._json({"ok": False, "msg": "无法进入相册：请确认手机已解锁，且游戏能正常进入"})
            backward = False
            if preload:
                env = os.environ.copy()
                env["PYTHONPATH"] = PYLIB + os.pathsep + env.get("PYTHONPATH", "")
                env["PYTHONIOENCODING"] = "utf-8"
                subprocess.run([sys.executable, os.path.join(SCRIPTS, "swipecap.py"), "--preload", "83"],
                               cwd=ROOT, env=env, capture_output=True, timeout=400)
                # 预热后已经停在最后一页，直接打开最后一张往回扫，不再冷启动
                if not self._open_last_card():
                    return self._json({"ok": False, "msg": "预热后打不开最后一张，请重试"})
                backward = True
            if (not backward) and _LAST.get("state") != "detail" and not self._open_first_card():
                return self._json({"ok": False, "msg": "打不开第一张明信片，请手动打开一张后重试"})
            ok, msg = JOB.start_sweep(maxn, backward)
            return self._json({"ok": ok, "msg": msg})

        if u.path == "/api/phone_scan":
            env = os.environ.copy()
            env["PYTHONPATH"] = PYLIB + os.pathsep + env.get("PYTHONPATH", "")
            r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "phone_clean.py"), "scan"],
                               cwd=ROOT, env=env, capture_output=True, timeout=600)
            scan_p = os.path.join(PROBE, "phone_scan.json")
            if os.path.exists(scan_p):
                d = json.load(open(scan_p, encoding="utf-8"))
                return self._json({"ok": True,
                                   "total": d["total_png"],
                                   "unique": d["unique_images"],
                                   "postcards": d.get("postcard_count", 0),
                                   "dups": len(d.get("duplicates_deletable", [])),
                                   "not_postcard": d.get("not_postcard", []),
                                   "dups_list": d.get("duplicates_deletable", [])[:200]})
            return self._json({"ok": False, "msg": r.stderr.decode("utf-8", "replace")[-400:]})

        if u.path == "/api/phone_delete":
            files = [os.path.basename(f) for f in (body.get("files") or [])]
            if not files:
                return self._json({"ok": False, "msg": "没有选中任何文件"})
            scan_p = os.path.join(PROBE, "phone_scan.json")
            if not os.path.exists(scan_p):
                return self._json({"ok": False, "msg": "缺少扫描结果"})
            d = json.load(open(scan_p, encoding="utf-8"))
            allowed = {it["file"] for it in d.get("duplicates_deletable", [])}
            bad = [f for f in files if f not in allowed]
            if bad:
                return self._json({"ok": False, "msg": f"拒绝执行：{len(bad)} 个文件不在重复清单里（{bad[:3]}）"})
            deleted, failed = 0, []
            for i in range(0, len(files), 20):
                chunk = files[i:i + 20]
                cmd = "cd /sdcard/Pictures && rm -f " + " ".join(chunk)
                rc, _, err = adb_run(cmd)
                if rc == 0:
                    deleted += len(chunk)
                else:
                    failed.extend(chunk)
            removed = set(files) - set(failed)
            d["duplicates_deletable"] = [x for x in d.get("duplicates_deletable", [])
                                         if x["file"] not in removed]
            with open(scan_p + ".new", "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            os.replace(scan_p + ".new", scan_p)
            return self._json({"ok": True, "deleted": deleted, "failed": failed})

        if u.path == "/api/phone_clean":
            if not body.get("confirm"):
                return self._json({"ok": False, "msg": "需要确认"})
            env = os.environ.copy()
            env["PYTHONPATH"] = PYLIB + os.pathsep + env.get("PYTHONPATH", "")
            r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "phone_clean.py"), "clean", "--yes"],
                               cwd=ROOT, env=env, capture_output=True, timeout=600)
            out = r.stdout.decode("utf-8", "replace")
            return self._json({"ok": r.returncode == 0, "msg": out[-300:]})

        if u.path == "/api/verify":
            if not self._cold_to_album():
                return self._json({"ok": False, "msg": "无法进入相册"})
            if not self._open_first_card():
                return self._json({"ok": False, "msg": "打不开第一张明信片"})
            ok, msg = JOB.start_verify()
            return self._json({"ok": ok, "msg": msg})

        if u.path == "/api/stop":
            return self._json({"ok": JOB.stop()})

        return self._send(404, "not found", "text/plain; charset=utf-8")

    def _sort_mode(self, shot_path=None):
        """判断相册排序：黄色=选中。返回 grid / clock / unknown。"""
        import numpy as np
        from PIL import Image
        p = shot_path
        if not p or not os.path.exists(p):
            p = os.path.join(PROBE, "_sort_probe.png")
            with open(p, "wb") as fh:
                subprocess.run([ADB, "exec-out", "screencap", "-p"], stdout=fh,
                               stderr=subprocess.PIPE, timeout=30)
        try:
            a = np.asarray(Image.open(p).convert("RGB")).astype(np.int16)
        except Exception:
            return "unknown"
        def yellow(x0, y0, x1, y1):
            reg = a[y0:y1, x0:x1]
            r, g, b = reg[:, :, 0], reg[:, :, 1], reg[:, :, 2]
            return int(((r > 185) & (g > 165) & (b < 170)).sum())
        clk = yellow(75, 445, 195, 560)
        grd = yellow(245, 445, 365, 560)
        if grd > clk * 1.5 and grd > 200:
            return "grid"
        if clk > grd * 1.5 and clk > 200:
            return "clock"
        return "unknown"

    def _ensure_grid_sort(self):
        """确保处于网格排序；必要时点一下按钮。"""
        if self._sort_mode() == "grid":
            return True, "already"
        subprocess.run([ADB, "shell", "input", "swipe", "302", "505", "302", "505", "160"],
                       capture_output=True, timeout=30)
        time.sleep(1.5)
        return (self._sort_mode() == "grid"), "tapped"

    def _stay_awake(self):
        """充电时常亮 + 必要时唤醒屏幕。"""
        subprocess.run([ADB, "shell", "svc", "power", "stayon", "true"],
                       capture_output=True, timeout=30)
        out = adb_out(["shell", "dumpsys", "power"])
        if "mWakefulness=Awake" not in out:
            subprocess.run([ADB, "shell", "input", "keyevent", "224"],
                           capture_output=True, timeout=30)
            time.sleep(1.6)

    def _tap(self, x, y, ms=200, wait=1.2):
        subprocess.run([ADB, "shell", "input", "swipe", str(x), str(y), str(x), str(y), str(ms)],
                       capture_output=True, timeout=30)
        time.sleep(wait)

    def _cold_to_album(self):
        """强杀 -> 冷启动 -> 点进入游戏 -> 走导航路径 -> 相册第 1 页。"""
        subprocess.run([ADB, "shell", "am", "force-stop", "com.Aligames.lxqw.huawei"],
                       capture_output=True, timeout=60)
        time.sleep(1.0)
        subprocess.run([ADB, "shell", "monkey", "-p", "com.Aligames.lxqw.huawei",
                        "-c", "android.intent.category.LAUNCHER", "1"],
                       capture_output=True, timeout=60)
        time.sleep(8.0)
        self._tap(656, 2053, 200, 9.0)          # 「点击进入游戏」
        steps, _ = resolve_steps()
        for st in steps:
            self._tap(int(st["x"]), int(st["y"]), 200, float(st.get("wait", 1.4)))
        for _ in range(3):
            if self._album_score() > 0.7:
                return True
            time.sleep(2.5)
        return False

    def _open_last_card(self):
        """在相册最后一页打开最靠后的一张（从右下往左上依次尝试）。"""
        for x, y in [(980, 1790), (388, 1790), (980, 1370), (388, 1370), (980, 955), (388, 955)]:
            for _ in range(2):
                self._tap(x, y, 250, 0.9)
                self._tap(x, y, 160, 1.6)
                shot = os.path.join(PROBE, "_last_card.png")
                with open(shot, "wb") as fh:
                    subprocess.run([ADB, "exec-out", "screencap", "-p"], stdout=fh,
                                   stderr=subprocess.PIPE, timeout=60)
                try:
                    self._classify(shot)
                except Exception:
                    pass
                if _LAST.get("state") == "detail":
                    return True
        return False

    def _open_first_card(self):
        """在相册第 1 页打开左上角第一张，确认详情弹窗出现。"""
        for _ in range(2):
            self._tap(388, 955, 250, 0.9)
            self._tap(388, 955, 160, 1.8)
            shot = os.path.join(PROBE, "_first_card.png")
            with open(shot, "wb") as fh:
                subprocess.run([ADB, "exec-out", "screencap", "-p"], stdout=fh,
                               stderr=subprocess.PIPE, timeout=60)
            try:
                self._classify(shot)
            except Exception:
                pass
            if _LAST.get("state") == "detail":
                return True
        return False

    def _classify(self, shot_path):
        """判断当前手机画面：album（相册页）/ detail（明信片详情弹窗）/ other / asleep。"""
        import numpy as _np
        from PIL import Image as _Im
        try:
            g = _np.asarray(_Im.open(shot_path).convert("L"), dtype=_np.float32)
        except Exception:
            return
        if g.mean() < 30:
            _LAST.update(state="asleep", score=0.0, ts=time.time())
            return
        cam_tpl = os.path.join(ROOT, "templates", "camera_icon.png")
        if os.path.exists(cam_tpl):
            t = _np.asarray(_Im.open(cam_tpl).convert("L"), dtype=_np.float32)
            th, tw = t.shape
            win = g[1765:1765 + th, 820:820 + tw]
            if win.shape == t.shape:
                t0, w0 = t - t.mean(), win - win.mean()
                den = (_np.sqrt((t0 * t0).sum()) + 1e-6) * (_np.sqrt((w0 * w0).sum()) + 1e-6)
                if float((t0 * w0).sum() / den) > 0.85:
                    _LAST.update(state="detail", score=1.0, ts=time.time())
                    return
        tpl = os.path.join(ROOT, "templates", "album_header.png")
        if not os.path.exists(tpl):
            ref = os.path.join(PROBE, "now.png")
            if os.path.exists(ref):
                os.makedirs(os.path.dirname(tpl), exist_ok=True)
                _Im.open(ref).convert("L").crop((40, 90, 1280, 330)).save(tpl)
        if os.path.exists(tpl):
            b = _np.asarray(_Im.open(tpl).convert("L"), dtype=_np.float32)
            a = g[90:330, 40:1280]
            if a.shape == b.shape:
                a0, b0 = a - a.mean(), b - b.mean()
                den = (_np.sqrt((a0 * a0).sum()) + 1e-6) * (_np.sqrt((b0 * b0).sum()) + 1e-6)
                sc = float((a0 * b0).sum() / den)
                _LAST.update(state=("album" if sc > 0.7 else "other"), score=round(sc, 3), ts=time.time())
                if sc > 0.7:
                    _LAST["sort"] = self._sort_mode(shot_path)
                return
        _LAST.update(state="other", score=0.0, ts=time.time())
    def _album_score(self):
        import numpy as _np
        from PIL import Image as _Im
        tpl_path = os.path.join(ROOT, "templates", "album_header.png")
        if not os.path.exists(tpl_path):
            ref = os.path.join(PROBE, "now.png")
            if not os.path.exists(ref):
                return 0.0
            os.makedirs(os.path.dirname(tpl_path), exist_ok=True)
            _Im.open(ref).convert("L").crop((40, 90, 1280, 330)).save(tpl_path)
        shot = os.path.join(PROBE, "_nav_check.png")
        with open(shot, "wb") as fh:
            subprocess.run([ADB, "exec-out", "screencap", "-p"], stdout=fh,
                           stderr=subprocess.PIPE, timeout=60)
        a = _np.asarray(_Im.open(shot).convert("L"), dtype=_np.float32)[90:330, 40:1280]
        b = _np.asarray(_Im.open(tpl_path).convert("L"), dtype=_np.float32)
        a0, b0 = a - a.mean(), b - b.mean()
        den = (_np.sqrt((a0 * a0).sum()) + 1e-6) * (_np.sqrt((b0 * b0).sum()) + 1e-6)
        return float((a0 * b0).sum() / den)

    def _device_ok(self):
        for attempt in (1, 2):
            try:
                p = subprocess.run([ADB, "devices"], capture_output=True, timeout=15)
                for line in p.stdout.decode("utf-8", "replace").splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == "device":
                        return True
            except Exception:
                pass
            if attempt == 1:
                try:
                    subprocess.run([ADB, "kill-server"], capture_output=True, timeout=15)
                except Exception:
                    pass
                time.sleep(0.8)
        return False


def port_busy(port):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.35)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def main():
    global PORT
    os.makedirs(PROBE, exist_ok=True)
    srv = None
    while PORT < 8999:
        if port_busy(PORT):
            PORT += 1
            continue
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
            break
        except OSError:
            PORT += 1
    if srv is None:
        print("找不到空闲端口", flush=True)
        return
    print(f"服务已启动: http://127.0.0.1:{PORT}/", flush=True)
    print("关闭这个窗口即停止服务。", flush=True)
    if not os.environ.get("FROG_NO_BROWSER"):
        threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}/")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()