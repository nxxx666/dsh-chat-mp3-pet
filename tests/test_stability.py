# -*- coding: utf-8 -*-
"""稳定性压测：连续多轮对话 / 抢话，看桌宠会不会闪退。

用假 DSH（慢速流），不需要 API Key。

用法：
    python test_stability.py            # 新写法（应该稳）
    python test_stability.py legacy     # 旧写法（直接覆盖 self.worker，对照用）

退出码：0 = 跑完没崩；非 0 = 中途 abort（Windows 上常见 0xC0000409 = 3221226505）
"""
import os
import subprocess
import sys
import time

os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")
sys.path.insert(0, os.path.abspath("."))

from PyQt6.QtCore import QTimer                          # noqa: E402
from PyQt6.QtWidgets import QApplication                 # noqa: E402

import config_store    # noqa: E402
import dsh_client      # noqa: E402
import memory          # noqa: E402
import pet_window      # noqa: E402

MODE = (sys.argv[1] if len(sys.argv) > 1 else "new").lower()
PORT = 47197
BRIDGE_DIR = os.path.join(os.path.abspath("."), "dsh-bridge")
TEST_DB = os.path.abspath("_test_stability.db")

config_store.save = lambda cfg: None

if os.path.isfile("_test_pet_bridge.json"):
    os.remove("_test_pet_bridge.json")

env = dict(os.environ)
env["PET_TEST_CHUNK_DELAY_MS"] = "250"      # 每帧 250ms，一轮约 1.2 秒，方便抢话
proc = subprocess.Popen(
    ["node", "test_server.mjs", str(PORT)],
    cwd=BRIDGE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
)
dsh_client.bridge_info = lambda: ("127.0.0.1", PORT, "")

app = QApplication(sys.argv)
rounds = 0
sent = 0
alive_at = []

try:
    for _ in range(40):
        if dsh_client.DshClient().health()[0]:
            break
        time.sleep(0.25)

    win = pet_window.PetWindow()
    win.cfg["ai_mode"] = "dsh"
    win.cfg["memory_enabled"] = False
    win.memory.close()
    win.memory = memory.MemoryStore(TEST_DB)
    win.memory.clear()
    win.show()
    print(f"模式={MODE}  窗口已开", flush=True)

    texts = ["第一句", "第二句", "第三句", "第四句", "第五句",
             "第六句", "第七句", "第八句", "第九句", "第十句"]

    def fire(i):
        """每 400ms 发一句——上一轮通常还没答完，正是最容易崩的时序。"""
        global sent
        if i >= len(texts):
            return
        t = texts[i]
        if MODE == "legacy":
            # 复刻旧代码：直接覆盖 self.worker，不保活、不断信号
            win.worker = pet_window.DshChatWorker(t, "")
            win.worker.chunk.connect(win._on_chunk)
            win.worker.failed.connect(win._on_failed)
            win.worker.finished_ok.connect(win._on_done)
            win.worker.session_ready.connect(win._on_dsh_session)
            win.worker.start()
        else:
            win.send(t)
        sent += 1
        print(f"  -> 已发第 {sent} 句", flush=True)
        QTimer.singleShot(400, lambda: fire(i + 1))

    QTimer.singleShot(600, lambda: fire(0))

    def watch():
        """每 500ms 记一次存活与线程数，顺便把事件循环跑起来。"""
        global rounds
        rounds += 1
        try:
            live = len(win._live_workers)
        except Exception:
            live = -1
        alive_at.append(live)
        if rounds >= 40:          # 约 20 秒
            app.quit()

    timer = QTimer()
    timer.timeout.connect(watch)
    timer.start(500)

    app.exec()
    print(f"事件循环正常退出；发话 {sent} 次，存活采样 {len(alive_at)} 次，"
          f"同时在跑的线程最多 {max(alive_at) if alive_at else 0} 个", flush=True)
    print("RESULT: SURVIVED", flush=True)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    for path in (TEST_DB, "_test_pet_bridge.json"):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
