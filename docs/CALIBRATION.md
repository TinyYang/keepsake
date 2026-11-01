# 机型标定指南

当前版本的坐标常量是按 **1316 × 2832** 的屏幕实测的。
换分辨率、换 App 版本后必须重新标定，否则点击会落在错误位置。

本文给出完整的标定流程。全程只需要：手机 + adb + 任意看图工具。

---

## 0. 先确认分辨率

```bash
adb shell wm size
# Physical size: 1316x2832
```

同时确认密度（影响 UI 尺寸）：

```bash
adb shell wm density
```

> 若分辨率与 1316×2832 差异较大，请先改 `scripts/swipecap.py` 顶部的
> `POSTCARD_CROP` / `PAGE_CROP` / 各坐标点，再跑一次标定验证。

---

## 1. 抓三张基准截图

```bash
adb exec-out screencap -p > grid.png      # 相册网格页
adb exec-out screencap -p > detail.png    # 打开一张卡片后的详情页
```

详情页的抓法：先长按卡片打开（见第 4 节），再截图。

---

## 2. 标定网格页的 6 个格子

`scripts/frogcap.py` 里的 `SLOTS` 是 6 个卡片的中心点，顺序为
**左→右、上→下**：

```
1  2
3  4
5  6
```

用下面的脚本可以从 `grid.png` 自动估算（基于"卡片区域与背景色差异"）：

```bash
python - <<'PY'
import numpy as np
from PIL import Image

a = np.asarray(Image.open("grid.png").convert("RGB")).astype(np.int16)
h, w, _ = a.shape
y0, y1 = int(h * 0.15), int(h * 0.72)
reg = a[y0:y1]

# 背景色 = 该区域出现最多的颜色
flat = reg.reshape(-1, 3)
cols, counts = np.unique(flat, axis=0, return_counts=True)
bg = cols[counts.argmax()]
mask = (np.abs(reg - bg).sum(axis=2) > 40)

def bands(profile, ratio=0.3, min_len=50):
    on = profile > profile.max() * ratio
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

cb = bands(mask.sum(axis=0))
rb = bands(mask.sum(axis=1))
print("列区间:", [(b[0], b[1]) for b in cb])
print("行区间:", [(b[0], b[1] + y0) for b in rb])
for r in rb:
    for c in cb:
        print(f"  ({(c[0]+c[1])//2}, {(r[0]+r[1])//2 + y0})")
PY
```

输出里应出现 2 列 × 3 行的中心点，把它们按顺序填进 `SLOTS`。

> 若自动检测不准（例如页面有装饰图案干扰），直接用看图工具读像素坐标也可以：
> 把鼠标放在卡片中心，读状态栏坐标，按比例换算到原图尺寸。

---

## 3. 标定详情页

在详情页截图 `detail.png` 上量四个东西：

| 常量 | 含义 | 怎么量 |
|---|---|---|
| `CAMERA_BOX` | 相机图标的矩形区域 `(x0, y0, x1, y1)` | 框住图标本体（含底板，不含投影） |
| `CAMERA_PT` | 相机图标的点击中心 | 通常是 `CAMERA_BOX` 的中心 |
| `CLOSE_PT` | 右上角关闭按钮的中心 | 圆形/方形 X 按钮的圆心 |
| `POSTCARD_CROP` | 明信片画面区域 | 只框画面，不含相框与工具栏 |

`POSTCARD_CROP` 用于计算"是不是同一张画"，**必须只包含画面本身**，
否则相邻卡片会因为背景相同而误判为同一张。

---

## 4. 验证点击是否生效

标定完先不要跑全量，用单张验证：

```bash
# 长按打开（160~250 ms）
adb shell input swipe <x> <y> <x> <y> 200
```

若一次没打开，再点一次。**注意：如果第一次点的是"选中"、第二次才是"打开"，
说明该版本需要两段式交互，`open_card()` 已经做了两次点击 + 校验，无需修改。**

确认详情页出现后，用脚本判断：

```bash
python scripts/match_one.py detail.png
# popup_score=1.0000  (>=0.90 判定弹窗已开)
```

---

## 5. 标定左滑

`scripts/swipecap.py` 的 `SWIPE_FROM` / `SWIPE_TO` 是左滑的起点与终点，
必须落在**画面区域内**（不要滑到工具栏或屏幕边缘，容易触发系统手势）。

参考值（1316×2832）：

```python
SWIPE_FROM = (1050, 1350)
SWIPE_TO   = (300, 1350)
SWIPE_MS   = 260
```

验证：

```bash
adb shell input swipe 1050 1350 300 1350 260
```

截图对比前后两张的画面哈希，应当不同。

---

## 6. 标定页码区域

`PAGE_CROP` 用来判断"是不是还在同一页"，一般框住右下角的 `n / N`：

```python
PAGE_CROP = (850, 2650, 1250, 2800)
```

不需要 OCR，只需要它在换页时会变化即可。

---

## 7. 录制导航路径

坐标标定完，还要告诉工具"怎么进入相册"。这一步在网页里做，不用改代码：

1. 手机停在 App 主界面
2. 点「录制进入相册的路径」
3. 在网页的手机画面里按顺序点几下，直到相册页出现
4. 点「结束录制并保存」

结果写入 `probe/nav.json`。想重来就点「清除校准」，会退回内置默认路径。

---

## 8. 标定完成的自检

| 检查项 | 期望 |
|---|---|
| 页面显示「已连接」 | ✅ |
| 手机当前画面识别为「相册页」 | ✅ |
| 点「一键进相册」能到相册 | ✅ |
| 单张试跑：打开 → 保存 → 有新文件 | ✅ |
| 左滑一次，画面变化 | ✅ |
| 连续 5 张，序号正确递增 | ✅ |

全部通过再跑全量。
