# -*- coding: utf-8 -*-
"""长回复端到端实测（不需要真 Key）：
起一个本地假的 OpenAI 兼容服务，返回一段很长的中文，
验证：请求参数正确（max_tokens / 长度指令）→ 流式收全 → 气泡分页 → 完整留档。
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")
sys.path.insert(0, os.path.abspath("."))

from PyQt6.QtCore import QTimer           # noqa: E402
from PyQt6.QtWidgets import QApplication   # noqa: E402

import config_store   # noqa: E402
import memory         # noqa: E402
import llm_client     # noqa: E402
import paths          # noqa: E402
import pet_window     # noqa: E402

# 一段 1200 字左右的回复：一页（约 560px 高）必然放不下，必然触发分页
REPLY = ("好呀，那我详细说说这个问题。首先你要确认配置文件里的密钥是否正确，"
         "注意不要有多余的空格或者换行。其次检查网络能不能正常访问接口地址，"
         "有些公司网络会拦截外部请求，这种情况下需要配置代理。"
         "第三，如果前两步都没问题，就看日志里有没有报错信息，"
         "通常 401 是密钥问题，402 是余额不足，429 是请求太频繁。"
         "最后，如果你用的是中转服务，要确认模型名字和对方文档写的一致。"
         "以上就是我建议的排查顺序，按这个来一般都能定位到原因。") * 6

captured = {}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        captured["payload"] = body
        captured["auth"] = self.headers.get("Authorization", "")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        # 每 20 字一 chunk，模拟真实流式
        step = 20
        for i in range(0, len(REPLY), step):
            piece = REPLY[i:i + step]
            data = {"choices": [{"delta": {"content": piece}}]}
            self.wfile.write(
                ("data: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()
            time.sleep(0.01)
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


win = pet_window.PetWindow()
win.show()
# 换成隔离的记忆库：测试对话不要写进用户真实的 memory.db
win.memory.close()
win.memory = memory.MemoryStore(os.path.abspath("_test_memory_long.db"))
win.memory.clear()
win.cfg.update({
    "ai_provider": "custom",
    "ai_base_url": f"http://127.0.0.1:{port}",
    "ai_api_key": "sk-local-test",
    "ai_reply_style": "long",
    "ai_thinking": False,     # 明确关掉，别受本机 config.json 影响
})
win.history = [{"role": "system", "content": win.persona_text}]


def drive():
    win.send("这件事怎么排查？")
    poll()


def poll():
    """等流式真正结束再校验，避免掐在半路。"""
    if win._chatting or win.history[-1].get("role") != "assistant":
        QTimer.singleShot(300, poll)
        return
    QTimer.singleShot(300, verify)


def verify():
    payload = captured.get("payload", {})
    msgs = payload.get("messages", [])
    sys_text = " ".join(m["content"] for m in msgs if m["role"] == "system")

    check("请求真的发出去了", bool(payload), list(payload)[:6])
    check("model 用的是配置里的", payload.get("model") == "deepseek-flash",
          payload.get("model"))
    check("max_tokens = 8192（详细档）", payload.get("max_tokens") == 8192,
          payload.get("max_tokens"))
    check("stream 已开启", payload.get("stream") is True)
    check("Key 以 Bearer 形式发送", captured.get("auth") == "Bearer sk-local-test",
          captured.get("auth"))
    check("system 里带了长度指令", "【回复长度】" in sys_text)
    check("详细档要求忽略字数限制", "忽略人设里任何字数限制" in sys_text)
    check("关掉了思考模式（要快）",
          payload.get("thinking", {}).get("type") == "disabled",
          payload.get("thinking"))

    # 前端结果
    check("收到完整回复（一字不少）",
          REPLY in win.history[-1]["content"],
          f"{len(win.history[-1]['content'])} 字 / 期望 {len(REPLY)}")
    shown = "".join(win.bubble.pages) if win.bubble.pages else win.bubble.text
    check("气泡显示 = 完整回复（单页全文或分页拼回）",
          shown == REPLY,
          f"pages={len(win.bubble.pages)} 显示 {len(shown)} 字")
    check("超长回复确实触发了分页", len(win.bubble.pages) > 1,
          f"{len(win.bubble.pages)} 页")

    limit = win.bubble.max_text_height()
    heights = [win.bubble._text_height(p, pet_window.BUBBLE_MAX_W - win.bubble._pad * 2)
               for p in win.bubble.pages]
    check("每页高度都不超上限", all(h <= limit for h in heights), heights)

    log_path = os.path.join(paths.base_dir(), "最近一次回复.txt")
    saved = open(log_path, encoding="utf-8").read() if os.path.isfile(log_path) else ""
    check("完整回复已留档", saved == REPLY, f"{len(saved)} 字")

    win.bubble.grab().save(os.path.abspath("live2d/_e2e_long_reply.png"))
    print("   截图 ->", os.path.abspath("live2d/_e2e_long_reply.png"), flush=True)

    print("\nRESULT:", "ALL PASS" if ok_all else "HAS FAILURE", flush=True)
    win.bubble.clear_paging()
    app.quit()


QTimer.singleShot(1800, drive)
QTimer.singleShot(20000, app.quit)   # 兜底
app.exec()
server.shutdown()
sys.exit(0 if ok_all else 1)
