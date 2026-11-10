# -*- coding: utf-8 -*-
"""被动监听的「跟随活跃会话」测试（不需要真 DSH / 不需要 Key）。

背景：桌宠原来只监听听 config 里记的那条会话（dsh_session_id）。用户在 DSH
桌面版里另开一条对话聊时，桌宠盯着旧会话，自然不冒思考泡。

这里用假 DSH 复现：
  1. 起一个自由推轮的假桥，桌宠先挂到一条「永远静默」的会话 sess-quiet
  2. 往 _test_switch.json 写 {"sessionId":"sess-pet-test"} —— 等价于用户在
     DSH 界面里切到另一条会话（插件据此更新 lastSessionId）
  3. 桌宠必须在几秒内跟过去，并冒出思考信号

跑： PYTHONIOENCODING=utf-8 /d/Anaconda/python.exe test_dsh_follow.py
"""

import json
import os
import subprocess
import sys
import time

os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")
os.environ["PET_TEST_CHUNK_DELAY_MS"] = "300"
sys.path.insert(0, os.path.abspath("."))

from PyQt6.QtWidgets import QApplication   # noqa: E402

import dsh_client     # noqa: E402
import pet_window     # noqa: E402

BRIDGE_DIR = os.path.join(os.path.abspath("."), "dsh-bridge")
PORT = 47213
SWITCH_FILE = os.path.join(BRIDGE_DIR, "_test_switch.json")

ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print(f"[{'OK ' if cond else 'FAIL'}] {name}{(' | ' + str(extra)) if extra else ''}",
          flush=True)


def pump(app, pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        app.processEvents()
        time.sleep(0.02)
    return pred()


def wait_bridge(port, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if dsh_client.DshClient("127.0.0.1", port).health()[0]:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


# 清掉上次残留：不然假桥一启动就会切会话
try:
    os.remove(SWITCH_FILE)
except OSError:
    pass

env = dict(os.environ)
env["PET_TEST_FREE_TURN"] = "1"
srv = subprocess.Popen(["node", "test_server.mjs", str(PORT)], cwd=BRIDGE_DIR, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
app = QApplication(sys.argv)
w = None
try:
    check("假 DSH 就绪", wait_bridge(PORT))
    dsh_client.bridge_info = lambda: ("127.0.0.1", PORT, "")

    got = {"thinking": False}
    w = pet_window.DshWatchWorker("sess-quiet")
    w.thinking.connect(lambda: got.__setitem__("thinking", True))
    w.start()

    # 1. 先确认它老老实实挂在静默会话上：不该有任何思考信号
    pump(app, lambda: got["thinking"], 4.0)
    check("静默会话上不会误报思考", not got["thinking"])
    check("此时监听的确实是传入的旧会话", w._get_target() == "sess-quiet",
          w._get_target())

    # 2. 模拟用户在 DSH 界面里切到另一条会话
    with open(SWITCH_FILE, "w", encoding="utf-8") as f:
        json.dump({"sessionId": "sess-pet-test"}, f)

    followed = pump(app, lambda: w._get_target() == "sess-pet-test", 12.0)
    check("检测到活跃会话变化并跟过去", followed, w._get_target())

    bubbled = pump(app, lambda: got["thinking"], 15.0)
    check("跟过去后收到了思考帧（会冒思考泡）", bubbled)

    # 3. stop 要能干净收尾（探针线程 + 流线程都不能残留）
    w.stop()
    stopped = pump(app, lambda: not w.isRunning(), 5.0)
    check("stop() 后监听线程退出", stopped)
finally:
    if w is not None:
        try:
            w.stop()
            w.wait(3000)
        except Exception:
            pass
    try:
        srv.kill()
    except Exception:
        pass
    try:
        os.remove(SWITCH_FILE)
    except OSError:
        pass

print("\nRESULT:", "ALL PASS" if ok_all else "SOME FAILED")
sys.exit(0 if ok_all else 1)
