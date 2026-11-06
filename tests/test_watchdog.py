# -*- coding: utf-8 -*-
"""验证看门狗：主线程故意卡 6 秒，看 _trace.log 里有没有抓到全线程栈。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

import trace

trace.start()
app = QApplication(sys.argv)

beat = QTimer(app)
beat.setInterval(400)
beat.timeout.connect(trace.touch)
beat.start()
trace.start_watchdog()

# 后台线程模拟「有活干的主线程对照组」
import threading


def _bg():
    time.sleep(4.0)


threading.Thread(target=_bg, name="fake-bg").start()

# 卡死现场：主线程睡 6 秒（>3s 阈值），看门狗应抓栈；睡的栈应指向 time.sleep
print(">>> 主线程故意卡 6 秒…", flush=True)
time.sleep(6.0)
trace.touch()

# 给看门狗一秒时间把「恢复响应」写出来
time.sleep(1.5)
print(">>> 日志内容：")
print(trace.dump_tail(80))
