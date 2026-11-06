# -*- coding: utf-8 -*-
"""dsh_client.py 的集成测试：起一个假 DSH（node test_server.mjs），真连它。

不依赖真的 DSH，也不需要 API Key——验证的是「桌宠 <-> 桥插件」这层的协议。
"""
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dsh_client  # noqa: E402

BRIDGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dsh-bridge")
PORT = 47198
ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print(f"[{'OK ' if cond else 'FAIL'}] {name}{(' | ' + str(extra)) if extra else ''}")


# ---------- 1. 帧解析（不联网，纯函数） ----------
check("快照帧不产生事件",
      dsh_client.parse_frame({"type": "snapshot", "records": []}) == [])

ev = dsh_client.parse_frame({"type": "assistant-stream",
                            "frame": {"type": "chunk", "chunk": {"text": "你好"}}})
check("流式增量解析成 delta", len(ev) == 1 and ev[0].kind == "delta" and ev[0].text == "你好",
      [(e.kind, e.text) for e in ev])

# 思考通道：reasoning-delta 绝不能当正文吐进气泡
ev = dsh_client.parse_frame({"type": "assistant-stream", "frame": {
    "type": "chunk", "chunk": {"type": "reasoning-delta", "index": 0, "text": "这里是我的内心独白"}}})
check("思考增量不产生正文 delta",
      not any(e.kind == "delta" for e in ev),
      [(e.kind, e.text) for e in ev])
check("思考增量只留一个「在想」的状态",
      any(e.kind == "status" and e.text == "thinking" for e in ev),
      [(e.kind, e.text) for e in ev])

ev = dsh_client.parse_frame({"type": "assistant-stream", "frame": {
    "type": "chunk", "chunk": {"type": "text-delta", "index": 1, "text": "正话"}}})
check("带 type 的 text-delta 正常显示",
      len(ev) == 1 and ev[0].kind == "delta" and ev[0].text == "正话",
      [(e.kind, e.text) for e in ev])

for ctype in ("block-start", "block-end", "usage", "finish", "tool-call-delta"):
    ev = dsh_client.parse_frame({"type": "assistant-stream", "frame": {
        "type": "chunk", "chunk": {"type": ctype, "index": 0, "text": "不该显示"}}})
    check(f"{ctype} 不进气泡", not any(e.kind == "delta" for e in ev), [(e.kind, e.text) for e in ev])

# 整条消息里 reasoning 与 text 混排：只留 text
ev = dsh_client.parse_frame({"type": "event", "event": {
    "type": "assistant/message", "seq": 1, "time": 0,
    "data": {"content": [
        {"type": "reasoning", "text": "想一想"},
        {"type": "text", "text": "答案"},
        {"type": "tool-call", "name": "bash"},
    ]}}})
check("整条消息剔除思考块与工具块",
      len(ev) == 1 and ev[0].text == "答案", [(e.kind, e.text) for e in ev])

ev = dsh_client.parse_frame({"type": "event", "event": {
    "type": "assistant/message", "seq": 1, "time": 0,
    "data": {"message": {"content": [
        {"type": "reasoning", "text": "内心戏"},
        {"type": "text", "text": "外面的话"},
    ]}}}})
check("套了一层 message 时同样剔除思考块",
      len(ev) == 1 and ev[0].text == "外面的话", [(e.kind, e.text) for e in ev])
check("只有思考没有正文时不出事件",
      dsh_client.parse_frame({"type": "event", "event": {
          "type": "assistant/message", "seq": 1, "time": 0,
          "data": {"content": [{"type": "reasoning", "text": "全是内心戏"}]}}}) == [])
check("_visible_text_of 跳过思考块",
      dsh_client._visible_text_of({"type": "reasoning", "text": "不能看"}) == "")

ev = dsh_client.parse_frame({"type": "event", "event": {
    "type": "assistant/message", "seq": 1, "time": 0,
    "data": {"content": [{"type": "text", "text": "整条回复"}]}}})
check("完整消息解析成 message",
      len(ev) == 1 and ev[0].kind == "message" and ev[0].text == "整条回复",
      [(e.kind, e.text) for e in ev])

ev = dsh_client.parse_frame({"type": "event", "event": {
    "type": "tool/call", "seq": 2, "time": 0, "data": {"name": "bash"}}})
check("工具调用解析成 tool 事件",
      len(ev) == 1 and ev[0].kind == "tool" and "bash" in ev[0].text,
      [(e.kind, e.text) for e in ev])

ev = dsh_client.parse_frame({"type": "event", "event": {
    "type": "turn/end", "seq": 3, "time": 0, "data": {}}})
check("回合结束解析成 status/turn_end",
      len(ev) == 1 and ev[0].kind == "status" and ev[0].text == "turn_end",
      [(e.kind, e.text) for e in ev])

check("自己说的话不回显",
      dsh_client.parse_frame({"type": "event", "event": {
          "type": "user/message", "seq": 0, "time": 0, "data": {}}}) == [])
check("认不出的帧不抛异常", dsh_client.parse_frame({"type": "???", "x": 1}) == [])
check("非 dict 帧不抛异常", dsh_client.parse_frame("oops") == [])

# 各种奇怪形状的文本提取
check("从 choices 里抠文本",
      dsh_client._text_of({"choices": [{"delta": {"content": "嘿"}}]}) == "嘿")
check("从数组里拼接文本",
      dsh_client._text_of([{"text": "a"}, {"text": "b"}]) == "ab")
check("空值返回空串", dsh_client._text_of(None) == "")

# ---------- 2. 起假 DSH ----------
proc = subprocess.Popen(
    ["node", "test_server.mjs", str(PORT)],
    cwd=BRIDGE_DIR,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    encoding="utf-8",
    errors="replace",
)
try:
    client = dsh_client.DshClient("127.0.0.1", PORT)
    ready = False
    for _ in range(40):
        time.sleep(0.25)
        ok, info = client.health()
        if ok:
            ready = True
            break
    check("桥插件起来了（/health 通）", ready, info if not ready else info.get("port"))

    # ---------- 3. 会话 ----------
    sessions = client.sessions()
    check("能列出会话", len(sessions) == 1 and sessions[0]["id"] == "sess-pet-test",
          [s.get("id") for s in sessions])

    sid = client.ensure_session()
    check("ensure_session 拿到会话 id", sid == "sess-pet-test", sid)

    # ---------- 4. 投递一句话 ----------
    good, sid2, err = client.prompt("今天天气怎么样")
    check("prompt 投递成功", good and not err, err)
    check("投递后 session_id 被记住", sid2 == "sess-pet-test", sid2)
    check("空消息被拒", client.prompt("   ")[0] is False)

    # ---------- 5. 读流 ----------
    events = []
    done = client.stream(lambda e: events.append(e))
    kinds = [e.kind for e in events]
    check("流正常收完", done is True, kinds)
    deltas = [e.text for e in events if e.kind == "delta"]
    check("收到两段增量并拼出完整句子",
          "".join(deltas) == "今天天气不错哦～", deltas)
    check("思考过程没有混进气泡",
          not any("先想一下" in t or "再回答" in t
                  for t in deltas + [e.text for e in events if e.kind == "message"]),
          deltas)
    check("思考阶段有「在想」状态",
          any(e.kind == "status" and e.text == "thinking" for e in events), kinds)
    check("收到工具调用事件", any(e.kind == "tool" and "bash" in e.text for e in events),
          [e.text for e in events if e.kind == "tool"])
    check("收到完整消息事件",
          any(e.kind == "message" and e.text == "今天天气不错哦～" for e in events),
          [e.text for e in events if e.kind == "message"])
    check("收到回合结束", kinds and kinds[-1] == "status" and events[-1].text == "turn_end", kinds)
    check("不出现在自己发的话（user/message 被过滤）",
          "sess-pet-test" in str(sessions) and not any(e.kind == "user" for e in events))

    # ---------- 6. 中途喊停 ----------
    stop = threading.Event()
    got = []
    t = threading.Thread(target=lambda: client.stream(lambda e: got.append(e), stop))
    t.start()
    time.sleep(0.15)
    stop.set()
    t.join(timeout=5)
    check("stop_flag 能中断读流", not t.is_alive(), f"收到 {len(got)} 帧后退出")

    # ---------- 7. 端口发现 ----------
    h, p, why = dsh_client.bridge_info()
    check("bridge_info 能读到状态文件（假 DSH 写的临时文件也走同一条逻辑）",
          isinstance(h, str) and isinstance(p, int), f"{h}:{p} {why}")

finally:
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        out = ""
    if "got prompt" in (out or ""):
        print("      （假 DSH 确实收到了 prompt）")

print("\nRESULT: " + ("ALL PASS" if ok_all else "FAIL"))
sys.exit(0 if ok_all else 1)
