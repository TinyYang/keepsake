# 故障排查

按"现象 → 原因 → 处理"组织。多数问题可以在 1 分钟内定位。

---

## 一、连接类

### 页面显示「未检测到手机」，但手机明明插着

先在命令行手动验证，排除工具的干扰：

```bash
adb devices -l
```

| 输出 | 含义 | 处理 |
|---|---|---|
| 空列表 | 系统没枚举到设备 | 换数据线（很多线只供电）、换 USB 口、解锁屏幕 |
| `unauthorized` | 未授权 | 手机解锁后重新插拔，点「允许 USB 调试」并勾选「始终允许」 |
| `offline` | 调试会话异常 | `adb kill-server` 后重插 |
| 有 `device` | 正常 | 说明是页面连到了旧的服务实例，见下条 |

### adb 有设备，但页面仍显示未连接

点页面上的「**诊断**」按钮，它会显示服务进程执行 `adb devices` 的原始输出。

若出现：

```
cannot open %TEMP%\adb.log: Permission denied
failed to start daemon
```

说明 adb 无法写入临时目录（常见于受限环境或权限异常）。
处理：设置临时目录到可写位置，或直接在命令行启动工具（它会自动处理）：

```bat
set TMP=%~dp0tmp
set TEMP=%~dp0tmp
python scripts\server.py
```

### 页面打不开 / 端口被占用

工具默认监听 `8787`，被占用时会自动顺延。**以命令行窗口打印的地址为准**，
不要沿用浏览器里的旧标签页。

旧进程残留时，Windows 上可以：

```bat
netstat -ano | findstr :8787
taskkill /PID <上面查到的PID> /F
```

---

## 二、点击类

### 点了没反应，需要点两次

`adb shell input tap` 的按压时间过短，被应用引擎丢弃。
本项目已统一改用长按：

```bash
adb shell input swipe x y x y 160
```

若你改过代码，确认没有退回 `input tap`。

### 保存后点「关闭」没反应

保存动作会锁住输入约 2~3 秒。本项目在保存后固定等待 4 秒。
若仍频繁失败，把 `scripts/swipecap.py` 里的 `time.sleep(4.0)` 调到 `5.0`。

### 点击落在错误位置

说明坐标与当前分辨率不匹配，需要标定 → 见 [CALIBRATION.md](CALIBRATION.md)。

---

## 三、采集中断类

### 反复出现「滑动没生效」

引擎会自动重试最多 8 次。若最终停止，说明应用确实没有响应滑动：

1. 手动看一眼手机：是否还停在明信片详情页？
2. 是否弹出了系统对话框（低电量、权限请求、系统更新）？
3. 是否因为长时间运行导致应用卡顿？重启应用后重新点「一键滑扫」

### 提示「手机锁屏了」

ADB **无法**解锁带密码/图案/人脸的锁屏。请手动解锁后重试。
建议采集前开启「充电时保持屏幕常亮」（工具会自动执行
`svc power stayon true`），并且不要按电源键。

### 跑到一半就停，像"提前完成"

旧版本会用"画面连续 3 次不变"判定到达末尾，而滑动被吞时也会触发。
当前版本已改为：

- 连续两次截图哈希一致才算"画面没变"
- 连续 8 次滑动无效且已走过 ≥480 个位置，才判定末尾
- 否则报"卡住"并保留进度

若仍遇到，请查看 `probe/sweep_log.txt` 末尾的判定理由。

### 截图全黑

屏幕已熄。工具会尝试发送唤醒键并重试一次；仍黑则说明已锁屏，需人工解锁。

---

## 四、数据类

### 手机里重复图片越来越多

早期版本在"已归档"的卡片上仍会点保存。当前版本会先比对画面哈希，已归档的跳过。
历史遗留的重复用页面上的「扫描 → 评审 → 清理」处理。

### 归档里出现重复文件

按 `sha256` 去重即可：

```bash
python - <<'PY'
import hashlib, os, collections
d = "archive/postcards"
seen = collections.defaultdict(list)
for f in os.listdir(d):
    if f.endswith(".png"):
        p = os.path.join(d, f)
        seen[hashlib.sha256(open(p, "rb").read()).hexdigest()].append(f)
for h, fs in seen.items():
    if len(fs) > 1:
        print(h[:12], fs)
PY
```

### manifest.json 的序号有重复 / 与实际文件不一致

用修复脚本重建（只读图片，重排序号）：

```bash
python scripts/fix_archive.py
```

它会：按现有文件重命名序号 → 重新计算哈希 → 重建 manifest。

### 导出的图片只有 500×350

这是应用自身导出的原始分辨率，不是工具压缩的结果。
屏幕截图只能得到被放大的版本（实测屏幕显示约 2 倍，但细节并不增加）。

---

## 五、环境类

### `ModuleNotFoundError: No module named 'numpy'`

```bash
pip install -r requirements.txt
```

若想隔离环境：

```bash
python -m venv .venv
.venv\Scripts\activate      # Windows
pip install -r requirements.txt
```

### `pip install` 报 `Permission denied`（写入临时目录失败）

多见于目录 ACL 异常。改用"下载 wheel 后解压"的方式（仓库内已带脚本）：

```bash
python scripts/bootstrap_pylibs.py numpy pillow
set PYTHONPATH=%CD%\pylibs
```

### 中文显示成乱码

命令行编码不一致。Windows 下先执行：

```bat
chcp 65001
set PYTHONIOENCODING=utf-8
```

---

## 六、需要提供的排障信息

提 issue 时请附上（**先删除其中的个人信息**）：

1. `adb devices -l` 的输出（设备序列号请打码）
2. 页面「诊断」里的 `rc` / `stdout` / `stderr`
3. `probe/sweep_log.txt` 的最后 20 行
4. 手机型号与系统版本、屏幕分辨率

**不要**上传：`probe/` 里的截图（可能含壁纸、通知、聊天内容）、`archive/` 里的图片。
