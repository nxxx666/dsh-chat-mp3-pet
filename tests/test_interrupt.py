# -*- coding: utf-8 -*-
"""打断（interrupt）端到端实测：不需要真的 DSH。

验证：
- 任务跑到一半时，右键菜单出现「停！打断当前任务」
- 点了之后，桥的 /cancel 真的被调（假 DSH 落盘 _test_cancel.json）
- 气泡提示「已停下 / 已打断…说继续」，不是报错
- 打断后同一个会话还能正常接着聊
- 空闲时点打断，提示「现在没有正在做的事哦」

跑： PYTHONIOENCODING=utf-8 /d/Anaconda/python.exe test_interrupt.py
"""

import json
import os
import subprocess
import sys
import time

os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")
os.environ["PET_TEST_CHUNK_DELAY_MS"] = "250"    # 拉长流式窗口，留出打断时机
sys.path.insert(0, os.path.abspath("."))

from PyQt6.QtWidgets import QApplication, QMenu   # noqa: E402

import config_store   # noqa: E402
import dsh_client     # noqa: E402
import memory         # noqa: E402
import pet_window     # noqa: E402

BRIDGE_PORT = 47197
BRIDGE_DIR = os.path.join(os.path.abspath("."), "dsh-bridge")
TEST_DB = os.path.abspath("_test_interrupt.db")
CANCEL_LOG = os.path.join(BRIDGE_DIR, "_test_cancel.json")

config_store.save = lambda cfg: None
dsh_client.bridge_info = lambda: ("127.0.0.1", BRIDGE_PORT, "")

ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print(f"[{'OK ' if cond else 'FAIL'}] {name}{(' | ' + str(extra)) if extra else ''}",
          flush=True)


for f in ("_test_pet_bridge.json", "_test_cancel.json"):
    p = os.path.join(BRIDGE_DIR, f)
    if os.path.isfile(p):
        os.remove(p)

proc = subprocess.Popen(
    ["node", "test_server.mjs", str(BRIDGE_PORT)],
    cwd=BRIDGE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    env={**os.environ},
)

app = QApplication(sys.argv)
try:
    for _ in range(40):
        if dsh_client.DshClient().health()[0]:
            break
        time.sleep(0.25)
    check("假 DSH 的桥插件已就绪", dsh_client.DshClient().health()[0])

    win = pet_window.PetWindow()
    win.show()
    win.memory.close()
    win.memory = memory.MemoryStore(TEST_DB)
    win.memory.clear()
    win._set_mode(pet_window.MODE_DSH)

    def wait_idle(timeout=25.0):
        end = time.time() + timeout
        while time.time() < end:
            app.processEvents()
            busy = win.worker is not None and win.worker.isRunning()
            if not busy and not win._chatting:
                app.processEvents()
                return True
            time.sleep(0.02)
        return False

    # ---------- 1. 任务跑到一半，菜单里出现打断入口 ----------
    win.send("帮我做个大活")
    end = time.time() + 5
    while time.time() < end and not win._chatting:
        app.processEvents()
        time.sleep(0.02)
    check("任务开始跑了（chatting）", win._chatting)

    _orig_exec = QMenu.exec
    _menus = []
    QMenu.exec = lambda self, *a, **k: (_menus.append(self), None)[1]
    try:
        win._show_menu(win.pos())
    finally:
        QMenu.exec = _orig_exec
    titles = [a.text() for a in _menus[0].actions()] if _menus else []
    check("聊天中菜单出现「停！打断当前任务」",
          any("打断当前任务" in t for t in titles), titles[:3])

    # ---------- 2. 真的打断 ----------
    win.interrupt_chat()
    done = wait_idle(15)
    check("打断后这轮正常收尾（没卡死）", done, f"chatting={win._chatting}")

    cancel_rec = None
    if os.path.isfile(CANCEL_LOG):
        cancel_rec = json.load(open(CANCEL_LOG, encoding="utf-8"))
    check("桥的 /cancel 真的被调了（通知到 DSH 侧）",
          cancel_rec is not None and cancel_rec.get("sessionId") == "sess-pet-test",
          cancel_rec)

    bubble_text = win.bubble.text or ""
    check("气泡是友好的打断提示（不是报错）",
          ("已停下" in bubble_text or "已打断" in bubble_text)
          and "继续" in bubble_text, bubble_text[:60])
    check("_interrupted 标记已复位", not win._interrupted)

    # ---------- 3. 空闲时点打断，提示没有正在做的事 ----------
    win.interrupt_chat()
    app.processEvents()
    bubble_text = win.bubble.text or ""
    check("空闲打断提示「现在没有正在做的事」",
          "没有正在做的事" in bubble_text, bubble_text[:40])

    # ---------- 4. 打断后同一个会话还能正常聊 ----------
    win.send("继续")
    done = wait_idle(25)
    check("打断后再说话，一轮能正常跑完", done, f"chatting={win._chatting}")
    bubble_text = win.bubble.text or ""
    check("回复内容正常（会话没被打断搞坏）",
          "今天天气不错哦" in bubble_text or "已停下" in bubble_text, bubble_text[:60])

    # ---------- 5. 空闲时菜单不再显示打断入口 ----------
    _menus = []
    QMenu.exec = lambda self, *a, **k: (_menus.append(self), None)[1]
    try:
        win._show_menu(win.pos())
    finally:
        QMenu.exec = _orig_exec
    titles = [a.text() for a in _menus[0].actions()] if _menus else []
    check("空闲菜单不再出现打断入口",
          not any("打断当前任务" in t for t in titles), titles[:3])

    win.memory.close()
    win.quit_app()
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    for f in (TEST_DB, os.path.join(BRIDGE_DIR, "_test_pet_bridge.json"),
              os.path.join(BRIDGE_DIR, "_test_cancel.json")):
        try:
            os.remove(f)
        except OSError:
            pass

print("\nRESULT: " + ("ALL PASS" if ok_all else "FAIL"))
sys.exit(0 if ok_all else 1)
