# -*- coding: utf-8 -*-
"""思考泡专项测试（不需要真的 DSH，也不需要 API Key）。

覆盖两条链路：

A. 跟桌宠聊：DSH 回合的思考阶段（reasoning-delta）→ 气泡从「...」变成
   「(💭 思考中…)」，正文一来就被盖掉。
B. 被动监听：用户在 DSH 桌面版里直接聊（桌宠没发话）→ 监听线程也收到
   思考帧 → 桌宠头上冒「(💭 想事情中…)」，正文来了收掉，回合结束回待机。
C. close() 掐 socket：流阻塞在无数据读上时，从别的线程关连接必须立刻醒
   （不然退出时会变成「析构运行中的 QThread」→ abort）。

跑： PYTHONIOENCODING=utf-8 /d/Anaconda/python.exe test_dsh_thinkbubble.py
"""

import os
import subprocess
import sys
import threading
import time

os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")
os.environ["PET_TEST_CHUNK_DELAY_MS"] = "300"   # 拉宽思考→正文的时间窗，便于观察
sys.path.insert(0, os.path.abspath("."))

from PyQt6.QtWidgets import QApplication   # noqa: E402

import config_store   # noqa: E402
import dsh_client     # noqa: E402
import memory         # noqa: E402
import pet_window     # noqa: E402

BRIDGE_DIR = os.path.join(os.path.abspath("."), "dsh-bridge")
TEST_DB = os.path.abspath("_test_thinkbubble.db")
PORT_A = 47211   # 正常模式（等 prompt 才推流）
PORT_B = 47212   # 自由推轮模式（订阅即推，模拟用户在 DSH 界面直接聊）

# 测试会写配置，换成空实现，别覆盖真实 config.json
config_store.save = lambda cfg: None

ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print(f"[{'OK ' if cond else 'FAIL'}] {name}{(' | ' + str(extra)) if extra else ''}",
          flush=True)


def start_server(port, free_turn):
    env = dict(os.environ)
    if free_turn:
        env["PET_TEST_FREE_TURN"] = "1"
    return subprocess.Popen(
        ["node", "test_server.mjs", str(port)],
        cwd=BRIDGE_DIR, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def wait_bridge(port, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = dsh_client.DshClient("127.0.0.1", port).health()
            if r[0]:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def pump(app, seconds):
    """带着事件循环等一段时间（让信号/定时器都有机会送达）。"""
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def wait_for(pred, timeout, step=0.02, app=None):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        if app is not None:
            app.processEvents()
        time.sleep(step)
    return pred()


srv_a = start_server(PORT_A, free_turn=False)
srv_b = start_server(PORT_B, free_turn=True)
app = QApplication(sys.argv)
try:
    check("假 DSH A（正常模式）就绪", wait_bridge(PORT_A))
    check("假 DSH B（自由推轮）就绪", wait_bridge(PORT_B))

    # ---------- 0. parse 层：reasoning-delta 归一化成 status/thinking ----------
    evs = dsh_client.parse_frame({
        "type": "assistant-stream",
        "frame": {"type": "chunk", "chunk": {"type": "reasoning-delta", "text": "想想"}},
    })
    check("reasoning-delta → status/thinking",
          len(evs) == 1 and evs[0].kind == "status" and evs[0].text == "thinking",
          [(e.kind, e.text) for e in evs])

    # ============================================================
    # A. 跟桌宠聊：思考阶段冒「(💭 思考中…)」
    # ============================================================
    dsh_client.bridge_info = lambda: ("127.0.0.1", PORT_A, "")
    win = pet_window.PetWindow()
    win.show()
    win.memory.close()
    win.memory = memory.MemoryStore(TEST_DB)
    win.memory.clear()
    win._set_mode(pet_window.MODE_DSH)

    seen_thinking_bubble = [False]

    def catch_thinking():
        seen_thinking_bubble[0] = seen_thinking_bubble[0] or (
            "思考中" in (win.bubble.text or ""))

    win.send("今天天气怎么样")
    check("send 后立刻是 DshChatWorker",
          isinstance(win.worker, pet_window.DshChatWorker))
    # 流式窗口内轮询：必须先看到思考泡，再看到正文
    end = time.time() + 30
    seen_reply = [False]
    while time.time() < end:
        app.processEvents()
        catch_thinking()
        if "今天天气不错哦" in (win.bubble.text or ""):
            seen_reply[0] = True
            break
        if not (win.worker and win.worker.isRunning()) and not win._chatting:
            break
        time.sleep(0.02)
    check("思考阶段气泡冒出「💭 思考中」", seen_thinking_bubble[0],
          win.bubble.text[:40])
    check("正文到了，思考泡被盖掉", seen_reply[0], win.bubble.text[:40])

    # 等这轮彻底结束（worker 退场）
    wait_for(lambda: win.worker is None or not win.worker.isRunning(), 10, app=app)
    pump(app, 0.3)

    # ============================================================
    # B. 被动监听：不发话，DSH 界面那边聊 → 桌宠跟着冒思考泡
    # ============================================================
    dsh_client.bridge_info = lambda: ("127.0.0.1", PORT_B, "")
    win._set_mode(pet_window.MODE_DSH)      # 切到 B 桥 → watcher 挂上（自由推轮）
    check("监听线程已挂上（DshWatchWorker 在跑）",
          isinstance(win.worker, pet_window.DshWatchWorker)
          and win.worker.isRunning(),
          type(win.worker).__name__)

    # 没有 send！等 watcher 从自由推轮里收到思考帧
    got_think = wait_for(
        lambda: "想事情中" in (win.bubble.text or ""), 15, app=app)
    check("没发话也冒出了「💭 想事情中」思考泡", got_think,
          win.bubble.text[:40])
    check("监听思考时 _watch_think 标记在", win._watch_think is True)
    check("监听思考时角色是 thinking 状态", win.renderer.state == "thinking",
          win.renderer.state)

    # 正文帧到了 → 思考泡收掉、回待机
    gone = wait_for(lambda: not win._watch_think, 10, app=app)
    check("正文来了，思考泡收掉（_watch_think 复位）", gone)
    check("思考泡收掉后角色回待机", win.renderer.state == "idle",
          win.renderer.state)

    # 切回独立 AI → 监听线程必须停干净
    win._set_mode(pet_window.MODE_STANDALONE)
    stopped = wait_for(
        lambda: not (win.worker and type(win.worker).__name__ == "DshWatchWorker"
                     and win.worker.isRunning()), 5, app=app)
    check("切回独立 AI 后监听线程已停", stopped,
          type(win.worker).__name__ if win.worker else None)

    # ============================================================
    # C. close() 从别的线程掐断阻塞中的流
    # ============================================================
    c = dsh_client.DshClient("127.0.0.1", PORT_A)   # A 没发话 → follow 挂着不推帧
    c.attach("sess-pet-test")
    state = {"done": False}

    def reader():
        c.stream(lambda e: None, session_id="sess-pet-test")
        state["done"] = True

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(1.0)                 # 让它阻塞进 iter_lines
    check("掐之前流确实阻塞着", not state["done"])
    t0 = time.time()
    c.close()                       # 从主线程掐 socket
    t.join(timeout=3)
    took = time.time() - t0
    check("close() 后阻塞的流立刻醒来（<3s）", state["done"] and took < 3.0,
          f"{took:.2f}s")

    win.memory.close()
    win.quit_app()
finally:
    for p in (srv_a, srv_b):
        try:
            p.kill()
        except Exception:
            pass

print("\nRESULT:", "ALL PASS" if ok_all else "SOME FAILED")
sys.exit(0 if ok_all else 1)
