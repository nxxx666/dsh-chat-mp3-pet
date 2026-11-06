# -*- coding: utf-8 -*-
"""DSH 模式端到端实测（不需要真的 DSH，也不需要 API Key）。

起一个「假 DSH」（node dsh-bridge/test_server.mjs，跑的是真的桥插件 + mock 的会话服务），
然后验证桌宠这一侧：

- 切到 DSH 模式后，send 走的是 DshChatWorker 而不是本地模型
- 气泡能流式收到内容，最终显示完整一句
- 会话 id 会被记住写进配置（下次接着聊同一个）
- DSH 模式下不再往桌面自己的 memory.db 里塞（记忆归 DSH 的记忆插件）
- 右键菜单里有「聊天模式」入口
- 切回独立 AI 后行为恢复

跑： PYTHONIOENCODING=utf-8 /d/Anaconda/python.exe test_dsh_mode.py
"""

import os
import subprocess
import sys
import time

os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")
sys.path.insert(0, os.path.abspath("."))

from PyQt6.QtWidgets import QApplication, QMenu   # noqa: E402

import config_store   # noqa: E402
import dsh_client     # noqa: E402
import memory         # noqa: E402
import pet_window     # noqa: E402

BRIDGE_PORT = 47198
BRIDGE_DIR = os.path.join(os.path.abspath("."), "dsh-bridge")
TEST_DB = os.path.abspath("_test_dsh_mode.db")

# 测试会写配置（切模式、记 session id），换成空实现，别覆盖真实 config.json
config_store.save = lambda cfg: None

ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print(f"[{'OK ' if cond else 'FAIL'}] {name}{(' | ' + str(extra)) if extra else ''}",
          flush=True)


# ---------- 起假 DSH ----------
if os.path.isfile("_test_pet_bridge.json"):
    os.remove("_test_pet_bridge.json")
proc = subprocess.Popen(
    ["node", "test_server.mjs", str(BRIDGE_PORT)],
    cwd=BRIDGE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)

# 让桌宠这里的端口发现指向假 DSH（真机上是读 ~/.dsh/pet-bridge.json）
dsh_client.bridge_info = lambda: ("127.0.0.1", BRIDGE_PORT, "")

app = QApplication(sys.argv)
try:
    for _ in range(40):
        if dsh_client.DshClient().health()[0]:
            break
        time.sleep(0.25)

    ok, info = dsh_client.DshClient().health()
    check("假 DSH 的桥插件已就绪", ok, info)

    win = pet_window.PetWindow()
    win.show()
    # 隔离记忆库：测试不要写进用户真实 memory.db
    win.memory.close()
    win.memory = memory.MemoryStore(TEST_DB)
    win.memory.clear()

    def wait_idle(timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            app.processEvents()
            busy = win.worker is not None and win.worker.isRunning()
            if not busy and not win._chatting:
                app.processEvents()
                return True
            time.sleep(0.02)
        return False

    # ---------- 1. 模式判定 ----------
    check("默认是独立 AI 模式", win.ai_mode() == pet_window.MODE_STANDALONE, win.ai_mode())

    win._set_mode(pet_window.MODE_DSH)
    check("能切到 DSH 模式", win.ai_mode() == pet_window.MODE_DSH, win.ai_mode())
    check("模式标签正确", win._mode_label() == "DSH 会话", win._mode_label())

    # ---------- 2. 走 DSH 说话 ----------
    before_mem = win.memory.count(win.persona_name)
    win.send("今天天气怎么样")
    app.processEvents()
    check("用的是 DshChatWorker（不是本地模型线程）",
          isinstance(win.worker, pet_window.DshChatWorker),
          type(win.worker).__name__)

    done = wait_idle(25)
    check("一轮对话正常结束", done, f"chatting={win._chatting}")

    bubble_text = win.bubble.text or ""
    check("气泡里出现了 DSH 的回复",
          "今天天气不错哦" in bubble_text, bubble_text[:60])
    check("会话 id 被记住", win._dsh_session_id == "sess-pet-test", win._dsh_session_id)
    check("会话 id 写进了配置", win.cfg.get("dsh_session_id") == "sess-pet-test",
          win.cfg.get("dsh_session_id"))

    after_mem = win.memory.count(win.persona_name)
    check("DSH 模式下不往桌宠自己的记忆库塞东西（记忆归 DSH）",
          after_mem == before_mem, f"{before_mem} -> {after_mem}")

    # ---------- 3. 再问一句，沿用同一个会话 ----------
    win.send("那明天呢")
    wait_idle(25)
    check("第二句仍然复用同一个会话", win._dsh_session_id == "sess-pet-test",
          win._dsh_session_id)

    # ---------- 4. 菜单里有聊天模式 ----------
    _orig_exec = QMenu.exec
    _menus = []
    QMenu.exec = lambda self, *a, **k: (_menus.append(self), None)[1]
    try:
        win._show_menu(win.pos())
    finally:
        QMenu.exec = _orig_exec
    titles = [a.text() for a in _menus[0].actions()] if _menus else []
    check("右键菜单能构建", bool(titles), len(titles))
    check("菜单里有「聊天模式」", any("聊天模式" in t for t in titles),
          [t for t in titles if "模式" in t])
    mode_menu = [a.menu() for a in _menus[0].actions()
                 if a.menu() and "聊天模式" in a.text()]
    mode_items = [a.text() for a in mode_menu[0].actions()] if mode_menu else []
    check("模式子菜单里有独立 AI / DSH 会话 / 检查连接",
          any("独立 AI" in t for t in mode_items)
          and any("DSH 会话" in t for t in mode_items)
          and any("检查 DSH 连接" in t for t in mode_items),
          mode_items)

    # ---------- 5. 切回独立 AI ----------
    win._set_mode(pet_window.MODE_STANDALONE)
    check("能切回独立 AI", win.ai_mode() == pet_window.MODE_STANDALONE, win.ai_mode())
    check("切回后标签也变了", win._mode_label() == "独立 AI", win._mode_label())

    win.memory.close()
    win.quit_app()
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    for f in (TEST_DB, "_test_pet_bridge.json"):
        try:
            os.remove(f)
        except OSError:
            pass

print("\nRESULT: " + ("ALL PASS" if ok_all else "FAIL"))
sys.exit(0 if ok_all else 1)
