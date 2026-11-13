# -*- coding: utf-8 -*-
"""角落气泡回归：贴屏四角 + 临界位置 + 流式长回复 + 拖动跟随。"""
import os, sys, time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
sys.setrecursionlimit(3000)

from PyQt6.QtWidgets import QApplication
import pet_window

app = QApplication(sys.argv)
scr = app.primaryScreen().availableGeometry()
win = pet_window.PetWindow()
win.show()
app.processEvents()

full = ("好呀，这个我可以帮你做。首先我们需要明确一下整体的目标和边界条件，"
        "然后再拆解成几个可以独立验证的小步骤，每一步都跑一遍确认结果符合预期，"
        "最后把它们串起来做成一个完整的流程，这样以后每次都能直接复用啦。"
        "另外我还建议顺便把异常情况也处理一下，比如超时、重试和降级，"
        "这样就算中间某一步出了问题也不会影响整体的使用体验哦。")
CHUNK = 6
chunks = [full[i:i + CHUNK] for i in range(0, len(full), CHUNK)]

def stream():
    """模拟流式回复：逐 chunk 送达（_on_chunk 直接调用，sender()=None 会放行）。"""
    win._pending_reply = ""
    win.renderer.state = "talking"
    for c in chunks:
        win._on_chunk(c)
        app.processEvents()
    win._flush_reply_paint()
    app.processEvents()

def check_inside(tag):
    b = win.bubble
    ok = (scr.left() + 2 <= b.x() and b.x() + b.width() <= scr.right() - 2
          and scr.top() + 2 <= b.y() and b.y() + b.height() <= scr.bottom() - 2)
    print(f"  {tag}: bubble=({b.x()},{b.y()}) {b.width()}x{b.height()} tail={b.tail_side} "
          f"{'OK' if ok else '越界!'}", flush=True)
    return ok

results = []
print(f"offscreen 屏: {scr.width()}x{scr.height()} | 窗口 {win.width()}x{win.height()}", flush=True)

# 1) 四个角落流式长回复
for name, (x, y) in {
    "左上": (scr.left(), scr.top()),
    "右上": (scr.right() - win.width(), scr.top()),
    "左下": (scr.left(), scr.bottom() - win.height()),
    "右下": (scr.right() - win.width(), scr.bottom() - win.height()),
}.items():
    win.move(x, y)
    app.processEvents()
    try:
        stream()
        results.append(check_inside(f"角落{name}"))
    except RecursionError as e:
        print(f"  角落{name}: RecursionError {e}", flush=True); results.append(False)
    except Exception as e:
        print(f"  角落{name}: {type(e).__name__}: {e}", flush=True); results.append(False)

# 2) 临界位置：右缘恰好差 0~15px 放不下气泡（翻转敏感区），逐像素扫
print("临界扫描（右缘 ±15px）：", flush=True)
crit_ok = True
base_x = scr.right() - win.width()
for dx in range(0, 16):
    win.move(base_x - dx, scr.top())
    app.processEvents()
    try:
        stream()
        b = win.bubble
        inside = (b.x() + b.width() <= scr.right() - 2)
        if not inside:
            crit_ok = False
    except RecursionError:
        print(f"  dx={dx}: RecursionError!", flush=True); crit_ok = False; break
    except Exception as e:
        print(f"  dx={dx}: {type(e).__name__}", flush=True); crit_ok = False; break
print(f"  临界扫描: {'OK' if crit_ok else 'FAIL'}", flush=True)
results.append(crit_ok)

# 3) 拖动跟随：显示气泡后挪动窗口，气泡应跟着走
win.move(scr.left() + 100, scr.top() + 100)
app.processEvents()
b0 = (win.bubble.x(), win.bubble.y())
win.move(scr.left() + 600, scr.top() + 300)
app.processEvents()
b1 = (win.bubble.x(), win.bubble.y())
followed = b0 != b1
print(f"  拖动跟随: {b0} -> {b1} {'OK' if followed else '没跟随!'}", flush=True)
results.append(followed)

win.bubble.hide()
win.close()
print()
print("RESULT:", "PASS 全部通过" if all(results) else f"FAIL {results}")
os._exit(0 if all(results) else 1)
