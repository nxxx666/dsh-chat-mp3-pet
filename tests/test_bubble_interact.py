# -*- coding: utf-8 -*-
"""气泡交互回归：点击关闭、悬停暂停、时长翻倍。"""
import os, sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from PyQt6.QtWidgets import QApplication
import pet_window

app = QApplication(sys.argv)
win = pet_window.PetWindow()
win.show()
app.processEvents()

results = []
def check(name, ok, extra=""):
    print(f"[{'OK' if ok else 'FAIL'}] {name} {extra}", flush=True)
    results.append(ok)

# 1) 点击关闭：信号触发 + 窗口隐藏 + 外层计时清零 + 角色回待机
fired = {"closed": False, "hover": []}
win.bubble.closed_by_user.connect(lambda: fired.__setitem__("closed", True))
win.bubble.hover_changed.connect(lambda on: fired["hover"].append(on))
win.bubble.set_text("试试点击关闭这条气泡")
app.processEvents()
win.renderer.state = "talking"
win._music_bubble_timer = 10.0
win.bubble.mousePressEvent(None)      # 用户点击（参数未用，直接传 None）
app.processEvents()
check("点击后隐藏", not win.bubble.isVisible())
check("closed_by_user 信号", fired["closed"])
check("计时清零", win._music_bubble_timer == 0.0)
check("角色回待机", win.renderer.state == "idle")

# 2) 悬停暂停：hovering=True 时 _tick 不递减
win.bubble.set_text("悬停暂停测试")
app.processEvents()
win._music_bubble_timer = 20.0
win.bubble.enterEvent(None)
check("悬停状态", win.bubble.hovering is True and fired["hover"][-1] is True)
for _ in range(60):
    win._tick()
check("悬停时计时冻结", win._music_bubble_timer == 20.0,
      f"(= {win._music_bubble_timer})")
win.bubble.leaveEvent(None)
for _ in range(60):
    win._tick()
check("离开后恢复递减", 18.8 < win._music_bubble_timer < 19.6,
      f"(= {round(win._music_bubble_timer, 3)})")

# 3) 递减速度减半：2 秒的 dt 只扣 1 秒
win._music_bubble_timer = 100.0
for _ in range(60):     # 60 帧 * 0.033s ≈ 2s，减半后应扣 ~1s
    win._tick()
check("递减速度减半", 98.8 < win._music_bubble_timer < 99.6,
      f"(扣了 {round(100 - win._music_bubble_timer, 3)}s，约 1s 为过)")

# 4) 隐藏时悬停残留不干扰
win.bubble.hide()
app.processEvents()
check("隐藏后不可见", not win.bubble.isVisible())

win.close()
print()
print("RESULT:", "ALL PASS" if all(results) else f"FAIL {results}")
os._exit(0 if all(results) else 1)
