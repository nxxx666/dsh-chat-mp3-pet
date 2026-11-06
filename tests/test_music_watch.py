# -*- coding: utf-8 -*-
"""验证：往 music/ 丢文件后自动重扫 + 气泡提示；删掉后也能反映。"""
import os
import sys
import wave

sys.path.insert(0, os.path.abspath("."))
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

import pet_window
from music_player import MUSIC_DIR

app = QApplication(sys.argv)
win = pet_window.PetWindow()
win.show()
TMP = os.path.join(str(MUSIC_DIR), "_watch_test_tone.wav")


def step1():
    print("[before] 歌数 =", len(win.music.playlist), flush=True)
    with wave.open(TMP, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 8000)
    print("[drop] 丢入", os.path.basename(TMP), flush=True)
    QTimer.singleShot(2200, step2)


def step2():
    print("[after-add] 歌数 =", len(win.music.playlist),
          "| 气泡 =", repr(win.bubble.text), flush=True)
    hit = [p for p in win.music.playlist if "_watch_test_tone" in p]
    print("[check] 新歌已进列表:", bool(hit), flush=True)
    os.remove(TMP)
    QTimer.singleShot(2200, step3)


def step3():
    print("[after-del] 歌数 =", len(win.music.playlist),
          "| 气泡 =", repr(win.bubble.text), flush=True)
    print("RESULT: DONE", flush=True)
    app.quit()


QTimer.singleShot(1800, step1)
app.exec()
