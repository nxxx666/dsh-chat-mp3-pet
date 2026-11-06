# -*- coding: utf-8 -*-
"""源码版稳定性测试：挂 PetWindow 跑 90 秒，每 10 秒报告存活与 GL 状态。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from pet_window import PetWindow  # noqa: E402


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    pet = PetWindow()
    pet.show()
    print("started: live2d_mode=%s" % pet.live2d_mode, flush=True)

    state = {"t": 0}

    def tick():
        state["t"] += 10
        gl = pet.gl_view
        print("t=%3ds alive win=%dx%d gl_ok=%s gl_err=%s" % (
            state["t"], pet.width(), pet.height(),
            getattr(gl, "ok", None), getattr(gl, "err", None)), flush=True)
        if state["t"] >= 90:
            print("STABLE", flush=True)
            app.quit()

    t = QTimer()
    t.timeout.connect(tick)
    t.start(10000)
    app.exec()


if __name__ == "__main__":
    main()
