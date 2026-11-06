# -*- coding: utf-8 -*-
"""记忆系统端到端实测（不需要真 Key）：起一个本地假 OpenAI 接口。

验证链条：
落库 → 主动召回注入 system → 上下文溢出触发后台摘要 → 摘要落库并注入 → 开关生效

跑： PYTHONIOENCODING=utf-8 /d/Anaconda/python.exe test_memory_e2e.py
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")
sys.path.insert(0, os.path.abspath("."))

from PyQt6.QtWidgets import QApplication   # noqa: E402

import config_store  # noqa: E402
import memory     # noqa: E402
import pet_window  # noqa: E402

# 这个测试会点「开关记忆」这些菜单动作，它们会调 config_store.save 落盘。
# 测试里换成空实现，绝不覆盖用户真实的 config.json。
config_store.save = lambda cfg: None

TEST_DB = os.path.abspath("_test_memory_e2e.db")
SUMMARY_TEXT = "用户在做桌宠的记忆功能测试，他喜欢简洁直白的回复。"
captured = []          # 每次请求的 payload


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        msgs = body.get("messages") or []
        sys_text = " ".join(m.get("content", "") for m in msgs
                            if m.get("role") == "system")
        is_summary = "对话记忆整理器" in sys_text
        captured.append({"payload": body, "summary": is_summary, "sys": sys_text})

        reply = SUMMARY_TEXT if is_summary else "嗯嗯，我记住啦。"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        step = 8
        for i in range(0, len(reply), step):
            data = {"choices": [{"delta": {"content": reply[i:i + step]}}]}
            self.wfile.write(
                ("data: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()
            time.sleep(0.002)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *a):
        pass


server = HTTPServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
print(f"[server] 假接口已起：http://127.0.0.1:{port}", flush=True)

app = QApplication(sys.argv)
ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print(f"[{'OK ' if cond else 'FAIL'}] {name}{(' | ' + str(extra)) if extra else ''}",
          flush=True)


def last_chat():
    chats = [c for c in captured if not c["summary"]]
    return chats[-1] if chats else {}


def wait_idle(timeout=20.0):
    """等聊天线程 + 摘要线程都跑完（信号是队列投递的，必须转事件循环）。"""
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        busy = (win.worker is not None and win.worker.isRunning()) or win._chatting
        busy = busy or (win._sum_worker is not None and win._sum_worker.isRunning())
        if not busy:
            app.processEvents()
            return True
        time.sleep(0.02)
    return False


win = pet_window.PetWindow()
win.show()
win.cfg.update({
    "ai_provider": "custom",
    "ai_base_url": f"http://127.0.0.1:{port}",
    "ai_api_key": "sk-local-test",
    "ai_reply_style": "short",
    "memory_enabled": True,
    "memory_recall": 3,
    "memory_summary": True,
})

# 换成隔离的测试库，避免污染真正的 memory.db
win.memory.close()
win.memory = memory.MemoryStore(TEST_DB)
win.memory.clear()
win._summary = ""
win.history = [{"role": "system", "content": win.persona_text}]
PERSONA = win.persona_name

# 预埋一条「很久以前」的记忆（60 天前说过猫的名字）
old_id = win.memory.add("user", "我的猫叫团子，是一只很胖的橘猫",
                        persona=PERSONA, session="old-session")
with win.memory._lock:
    win.memory._db.execute("UPDATE memos SET ts=? WHERE id=?",
                           (time.time() - 60 * 86400, old_id))
    win.memory._db.commit()
check("预埋旧记忆成功", win.memory.count(PERSONA) == 1, win.memory.count(PERSONA))

# ---------- 1. 主动召回 ----------
captured.clear()
win.send("我家猫叫什么名字来着")
wait_idle()
payload = last_chat()
check("聊天请求发出去了", bool(payload))
check("用户这句话已落库", any(h["content"] == "我家猫叫什么名字来着"
                              for h in win.memory.recent(10, persona=PERSONA)),
      [h["content"][:12] for h in win.memory.recent(10, persona=PERSONA)])
check("system 里注入了【想起一些旧事】", "【想起一些旧事】" in payload["sys"])
check("召回到 60 天前那句猫的名字", "团子" in payload["sys"],
      payload["sys"][payload["sys"].find("【想起一些旧事】"):][:100])
check("召回的旧片段带了角色和时间",
      "用户：" in payload["sys"] and "- " in payload["sys"])
check("注入的召回没有污染 history",
      "【想起一些旧事】" not in win.history[0]["content"])
_n_self = payload["sys"].count("我家猫叫什么名字来着")
check("召回不会把自己刚说的那句塞回来（system 里不该有本轮提问）", _n_self == 0,
      f"出现 {_n_self} 次")

# ---------- 2. 聊到上下文溢出，触发摘要 ----------
for i in range(9):
    win.send(f"第 {i + 1} 句：今天在写桌宠的记忆功能，写得很顺利")
    wait_idle()
    if len(win.history) > pet_window.MAX_HISTORY + 1:
        break

check("上下文被裁剪过（不会无限增长）",
      len([m for m in win.history if m["role"] != "system"]) <= pet_window.MAX_HISTORY + 1,
      len(win.history))
check("后台摘要跑过一轮", any(c["summary"] for c in captured),
      sum(1 for c in captured if c["summary"]))
check("摘要已写进长期记忆", "记忆功能测试" in win.memory.latest_summary(PERSONA),
      win.memory.latest_summary(PERSONA)[:40])
check("内存里的摘要也更新了", win._summary == win.memory.latest_summary(PERSONA))

# ---------- 3. 摘要注入下一轮 system ----------
captured.clear()
win.send("刚才说到哪儿了")
wait_idle()
sys_text = last_chat()["sys"]
check("system 里注入了长期摘要", "【长期记忆" in sys_text and "记忆功能测试" in sys_text,
      sys_text[:120])
check("摘要块位置在长度指令之前（顺序：人设→摘要→召回→长度）",
      sys_text.find("【长期记忆") < sys_text.find("【回复长度】"),
      [sys_text.find(x) for x in ("【长期记忆", "【回复长度】")])

# ---------- 4. 字数预算 ----------
total = sum(len(m["content"]) for m in win.history if m["role"] != "system")
check("短期上下文总字数在预算内",
      total <= pet_window.HISTORY_CHAR_BUDGET + 600, total)

# ---------- 5. 开关 ----------
win._set_recall(0)
captured.clear()
win.send("随便聊聊，测试一下不回忆")
wait_idle()
check("回忆条数设 0 后不再注入旧片段",
      "【想起一些旧事】" not in last_chat()["sys"])

win._toggle_memory(False)
before = win.memory.count(PERSONA)
captured.clear()
win.send("记忆关掉之后说的话不该进库")
wait_idle()
check("关掉记忆后不再落库", win.memory.count(PERSONA) == before,
      f"{before} -> {win.memory.count(PERSONA)}")

win._toggle_memory(True)
win._set_recall(3)

# ---------- 6. 换人设 = 换记忆 ----------
win.memory.add("user", "我养了一条叫小黑的狗", persona="别人设", session="x")
win.cfg["persona"] = "别人设"
win.switch_persona(PERSONA)
check("换回原人设后摘要还在", win._summary == win.memory.latest_summary(PERSONA))

# ---------- 7. 记忆窗口能开 ----------
try:
    import memory_dialog
    dlg = memory_dialog.MemoryDialog(win.memory, PERSONA, win)
    dlg.show()
    app.processEvents()
    n_items = dlg.list.count()
    dlg.close()
    check("记忆窗口能列出条目", n_items > 0, n_items)
except Exception as exc:
    check("记忆窗口能开", False, repr(exc))

# ---------- 8. 右键菜单能正常构建 ----------
from PyQt6.QtWidgets import QMenu   # noqa: E402
_orig_exec = QMenu.exec
_menus = []
QMenu.exec = lambda self, *a, **k: (_menus.append(self), None)[1]
try:
    win._show_menu(win.pos())
finally:
    QMenu.exec = _orig_exec

titles = [a.text() for a in _menus[0].actions()] if _menus else []
check("右键菜单能构建出来", bool(titles), len(titles))
check("菜单里有「记忆（N 条）」入口",
      any(t.startswith("记忆（") for t in titles), [t for t in titles if "记忆" in t])
mem_menu = [a.menu() for a in _menus[0].actions()
            if a.menu() and a.text().startswith("记忆（")]
mem_items = [a.text() for a in mem_menu[0].actions()] if mem_menu else []
check("记忆子菜单项齐全（开关/回忆条数/整理/查看/清空）",
      len(mem_items) >= 6 and any("启用长期记忆" in t for t in mem_items),
      mem_items)

# ---------- 收尾 ----------
print("\n摘要内容：", win.memory.latest_summary(PERSONA), flush=True)
print("库统计：", win.memory.count(PERSONA), "条 /", win.memory.sessions(PERSONA), "场",
      flush=True)
win.memory.clear()
win.memory.close()
server.shutdown()
print("\nRESULT:", "ALL PASS" if ok_all else "HAS FAILURE", flush=True)
sys.exit(0 if ok_all else 1)
