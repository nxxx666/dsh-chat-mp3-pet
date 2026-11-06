# -*- coding: utf-8 -*-
"""真实图形栈验证：把 Live2D 角色层挂进桌宠主窗口并截图。

用法：
    python test_pet_live2d.py
产出：
    live2d/_pet_gl.png        GL 层自身抓取（应当有 alpha 通道）
    live2d/_pet_screen.png    屏幕抓取（应当能看到桌面背景透过角色周围）
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from pet_window import PetWindow  # noqa: E402

OUT_GL = os.path.join(HERE, "live2d", "_pet_gl.png")
OUT_SCR = os.path.join(HERE, "live2d", "_pet_screen.png")


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    pet = PetWindow()
    pet.show()
    pet.bubble.set_text("Live2D 上线啦，你看我像不像活的？")

    def report():
        print("live2d_mode :", pet.live2d_mode)
        print("窗口尺寸    :", pet.width(), "x", pet.height())
        if pet.gl_view is not None:
            v = pet.gl_view
            print("GL ok       :", v.ok, "| err:", v.err)
            print("动作组      :", v.motions())
            print("表情        :", v.expressions())
            pm = v.grab()
            pm.save(OUT_GL)
            print("GL 抓取     :", OUT_GL, pm.width(), "x", pm.height(),
                  "alpha:", pm.hasAlpha())
        scr = QApplication.primaryScreen().grabWindow(
            0, pet.x(), pet.y(), pet.width(), pet.height())
        scr.save(OUT_SCR)
        print("屏幕抓取    :", OUT_SCR)
        app.quit()

    QTimer.singleShot(2500, report)
    app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
