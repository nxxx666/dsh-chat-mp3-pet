"""离屏冒烟测试：不弹窗口，验证渲染和 UI 构建都正常。

在 CI 或无显示环境也能跑：QT_QPA_PLATFORM=offscreen
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QColor, QImage, QPainter
from PyQt6.QtCore import Qt

from pet_widget import PetRenderer
from pet_window import Bubble, PetWindow

app = QApplication([])

# 1. 三种状态各渲染一帧
for state in ("idle", "thinking", "talking"):
    img = QImage(220, 220, QImage.Format.Format_ARGB32)
    img.fill(QColor("#20242c"))
    p = QPainter(img)
    r = PetRenderer()
    r.state = state
    r.t = 1.234
    r.paint(p, 220, 220)
    p.end()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets",
                       f"preview_{state}.png")
    ok = img.save(out)
    print(f"  {state:9s} 渲染 {'成功' if ok else '失败'} -> {out}")

# 2. 气泡换行与尺寸计算
b = Bubble()
b.set_text("这是一段用来测试气泡自动换行和高度计算的较长文字，看看会不会超过最大宽度或者被裁掉。")
print(f"  气泡尺寸: {b.width()}x{b.height()} (最大宽 300)")

b2 = Bubble()
b2.set_text("短")
print(f"  短气泡尺寸: {b2.width()}x{b2.height()}")

# 3. 主窗口能否构建（不 show）
w = PetWindow()
print(f"  主窗口构建成功，尺寸 {w.width()}x{w.height()}，位置 ({w.x()}, {w.y()})")

# 4. 历史裁剪逻辑
w.history = [{"role": "system", "content": "sys"}]
for i in range(30):
    w.history.append({"role": "user" if i % 2 == 0 else "assistant", "content": str(i)})
w._trim_history()
roles = [m["role"] for m in w.history]
assert roles[0] == "system", "system 提示词被裁掉了"
assert len(w.history) == 13, f"裁剪后条数不对: {len(w.history)}"
print(f"  历史裁剪正确：保留 system + 12 条，共 {len(w.history)} 条")

print("\n全部通过")
