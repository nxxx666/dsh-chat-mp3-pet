# -*- coding: utf-8 -*-
"""长回复实测：token 上限 / 人设字数覆盖 / 气泡分页不丢字、不超屏。"""

import json
import os
import sys

os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")
sys.path.insert(0, os.path.abspath("."))

from PyQt6.QtWidgets import QApplication   # noqa: E402

import config_store      # noqa: E402
import llm_client        # noqa: E402
import pet_window        # noqa: E402

app = QApplication(sys.argv)
ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print(f"[{'OK ' if cond else 'FAIL'}] {name}{(' | ' + str(extra)) if extra else ''}",
          flush=True)


# ---------- 1. 档位与 token 上限 ----------
check("三档齐全", set(llm_client.REPLY_STYLES) == {"short", "normal", "long"})
check("简短档 800 tokens",
      llm_client.style_of("short")["max_tokens"] == 800)
check("正常档 4096 tokens",
      llm_client.style_of("normal")["max_tokens"] == 4096)
check("详细档 8192 tokens",
      llm_client.style_of("long")["max_tokens"] == 8192)
check("非法档位退回默认",
      llm_client.style_of("xxx")["max_tokens"] == 4096)
check("旧上限 1024 已被默认档突破",
      llm_client.style_of("normal")["max_tokens"] > 1024)
check("默认档是正常", config_store.DEFAULTS["ai_reply_style"] == "normal")

# ---------- 2. max_tokens 真的发给了服务端 ----------
captured = {}


class FakeResp:
    status_code = 200
    text = ""

    def iter_lines(self, decode_unicode=True):
        return iter(['data: {"choices":[{"delta":{"content":"你好"}}]}',
                     "data: [DONE]"])


def fake_post(url, json=None, headers=None, stream=None, timeout=None):
    captured["payload"] = json
    return FakeResp()


orig_post = llm_client.requests.post
llm_client.requests.post = fake_post
try:
    out = "".join(llm_client.chat_stream(
        [{"role": "user", "content": "hi"}], provider="deepseek",
        api_key="sk-x", max_tokens=8192))
    check("请求里带 max_tokens=8192",
          captured["payload"].get("max_tokens") == 8192,
          captured["payload"].get("max_tokens"))
    check("SSE 解析正常", out == "你好", repr(out))
finally:
    llm_client.requests.post = orig_post

# ---------- 3. 人设字数限制被覆盖（只改副本） ----------
win = pet_window.PetWindow()
win.show()
win.cfg["ai_reply_style"] = "long"
msgs = win._messages_for_api()
sys_msg = msgs[0]["content"]
check("system 里注入了长度指令", "【回复长度】" in sys_msg)
check("详细档明确要求忽略字数限制", "忽略人设里任何字数限制" in sys_msg)
check("原始人设 history 未被污染",
      "【回复长度】" not in win.history[0]["content"])
check("人设正文仍在", win.persona_text.strip()[:12] in sys_msg)

win.cfg["ai_reply_style"] = "short"
check("简短档也给出对应指令",
      "保持简短" in win._messages_for_api()[0]["content"])

# ---------- 4. ai_params 带上档位对应的 max_tokens ----------
win.cfg["ai_reply_style"] = "long"
check("ai_params 用详细档 8192",
      win.ai_params()["max_tokens"] == 8192, win.ai_params()["max_tokens"])
win.cfg["ai_reply_style"] = "normal"
check("ai_params 用正常档 4096",
      win.ai_params()["max_tokens"] == 4096)

# ---------- 5. 气泡分页：不丢字、不超高 ----------
b = win.bubble
limit = b.max_text_height()
print(f"   单页可用高度 = {limit}px, 气泡宽上限 = {pet_window.BUBBLE_MAX_W}px",
      flush=True)

long_text = ("这是第一段。关于这个问题，我想从三个方面来说明。"
             "第一，你需要先确认配置是否正确，尤其是密钥和网络。"
             "第二，如果配置没问题，就要看日志里有没有报错信息。"
             "第三，实在不行可以换一个模型试试看，或者把问题描述得更具体一些。") * 8
print(f"   测试文本长度 = {len(long_text)} 字", flush=True)

pages = b.paginate(long_text)
check("长文本被切成多页", len(pages) > 1, f"{len(pages)} 页")
check("拼回去和原文一致（不丢字）", "".join(pages) == long_text)
check("每页都放得下",
      all(b._text_height(p, pet_window.BUBBLE_MAX_W - b._pad * 2) <= limit
          for p in pages),
      [b._text_height(p, pet_window.BUBBLE_MAX_W - b._pad * 2) for p in pages][:5])
check("分页优先在句号处断开",
      all(p.rstrip().endswith(("。", "！", "？", "；", "\n")) or i == len(pages) - 1
          for i, p in enumerate(pages[:-1])))

short_text = "今天天气不错。"
check("短文本不切页", len(b.paginate(short_text)) == 1)

# 单句超长（无标点）也要能硬切
no_punct = "啊" * 3000
pages2 = b.paginate(no_punct)
check("无标点长文也能硬切", len(pages2) > 1, f"{len(pages2)} 页")
check("硬切也不丢字", "".join(pages2) == no_punct)

# ---------- 6. 分页显示：窗口高度不超单页上限 ----------
b.set_pages(pages)
app.processEvents()
h_body = b.height() - pet_window.TAIL
check("气泡高度受控", h_body <= limit + b._pad * 2 + 8 + 18, f"{h_body}px")
check("首页显示正确", b.text == pages[0])
check("页数记录正确", len(b.pages) == len(pages))
check("翻页计时器已启动", b._page_timer.isActive())

b.grab().save(os.path.abspath("live2d/_bubble_page1.png"))
b._next_page()
app.processEvents()
check("翻页后内容变化", b.text == pages[1], b.text[:12])
b.grab().save(os.path.abspath("live2d/_bubble_page2.png"))
print("   截图 ->", os.path.abspath("live2d/_bubble_page1.png"), flush=True)

# 流式阶段：超长只显示尾部
b.set_text("前" * 2000 + "最新的结论在这里")
app.processEvents()
disp = b._display_text()
check("流式超长只显示尾部", disp.startswith("…") and disp.endswith("最新的结论在这里"),
      disp[:6] + "..." + disp[-8:])
check("流式气泡高度受控", b.height() <= limit + b._pad * 2 + 8 + pet_window.TAIL)

# ---------- 7. 回复长度切换会落盘 ----------
tmp = os.path.abspath("_test_config_len.json")
real = config_store.CONFIG_PATH
config_store.CONFIG_PATH = tmp
try:
    win._set_reply_style("long")
    saved = json.load(open(tmp, encoding="utf-8"))
    check("切换档位已写盘", saved["ai_reply_style"] == "long",
          saved.get("ai_reply_style"))
finally:
    config_store.CONFIG_PATH = real
    try:
        os.remove(tmp)
    except OSError:
        pass

# ---------- 8. 完整回复留档 ----------
win._save_reply(long_text)
p = os.path.join(__import__("paths").base_dir(), "最近一次回复.txt")
check("完整回复已存文件",
      os.path.isfile(p) and open(p, encoding="utf-8").read() == long_text)

print("\nRESULT:", "ALL PASS" if ok_all else "HAS FAILURE", flush=True)
b.clear_paging()
b.close()
sys.exit(0 if ok_all else 1)
