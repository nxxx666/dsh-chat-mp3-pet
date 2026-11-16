# -*- coding: utf-8 -*-
"""审批确认泡的测试。

覆盖：
  1. 流帧解析：approval / approval/decided / approval/cancel
  2. 气泡进入确认模式：尺寸留出按钮位、按钮在气泡内
  3. 点「允许一次」/「拒绝」发 answered 信号
  4. 确认模式下点空白处不关闭（避免误关导致 DSH 那轮卡死）
  5. DshClient.approve 真的把 id 和决定发到了桥上

跑法：QT_QPA_PLATFORM=offscreen python test_approval_bubble.py
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from PyQt6.QtCore import QPointF
from PyQt6.QtWidgets import QApplication

import dsh_client
import pet_window

PASS = 0
FAIL = 0


def check(name, ok, extra=""):
    global PASS, FAIL
    print(f"[{'OK' if ok else 'FAIL'}] {name}{('  ' + extra) if extra else ''}")
    if ok:
        PASS += 1
    else:
        FAIL += 1


class FakeEvent:
    """只实现气泡用到的那一个方法。"""

    def __init__(self, x, y):
        self._p = QPointF(x, y)

    def position(self):
        return self._p


def test_parse():
    frames = [
        ({"type": "approval", "id": "a1", "toolName": "pwsh", "reason": "写桌面文件"}, "approval"),
        ({"type": "approval/decided", "id": "a1", "outcome": "allowed-once"}, "approval_done"),
        ({"type": "approval/timeout", "id": "a1"}, "approval_cancel"),
        ({"type": "approval/cancel", "id": "a1"}, "approval_cancel"),
    ]
    for frame, kind in frames:
        evs = dsh_client.parse_frame(frame)
        check(f"解析 {frame['type']}", len(evs) == 1 and evs[0].kind == kind,
              evs[0].kind if evs else "无事件")
    ev = dsh_client.parse_frame({"type": "approval", "id": "a1", "toolName": "pwsh",
                                 "reason": "写桌面文件"})[0]
    check("审批事件带出理由", "写桌面文件" in ev.text, ev.text)
    check("审批帧原始数据完整", (ev.raw or {}).get("id") == "a1")


def test_bubble(app):
    b = pet_window.Bubble()
    b.set_text("普通消息")
    w0, h0 = b.width(), b.height()
    b.show_ask("req-1", "🔐 要用「pwsh」越权操作\n写文件\n\n允许这一次吗？")
    w1, h1 = b.width(), b.height()
    check("确认模式把气泡撑高（留按钮位）", h1 > h0, f"({h0} -> {h1})")
    check("确认模式标记生效", b.ask_id == "req-1")
    check("按钮落在气泡内",
          b._btn_allow.bottom() <= h1 and b._btn_deny.right() <= w1
          and b._btn_allow.width() > 0,
          f"allow={b._btn_allow}")

    got = []
    b.answered.connect(lambda allow: got.append(allow))
    closed = []
    b.closed_by_user.connect(lambda: closed.append(1))

    # 点空白处：确认模式不该关闭
    b.mousePressEvent(FakeEvent(3, 3))
    check("确认模式点空白不关闭", not closed and not got)

    b.mousePressEvent(FakeEvent(b._btn_allow.center().x(), b._btn_allow.center().y()))
    check("点「允许一次」发出 True", got == [True], str(got))

    b.mousePressEvent(FakeEvent(b._btn_deny.center().x(), b._btn_deny.center().y()))
    check("点「拒绝」发出 False", got == [True, False], str(got))
    check("点按钮不会误触关闭", not closed)

    b.clear_ask()
    h2 = b.height()
    check("退出确认模式后高度回落", h2 < h1, f"({h1} -> {h2})")
    b.mousePressEvent(FakeEvent(2, 2))
    check("普通模式点一下仍可关闭", bool(closed))


class Handler(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        Handler.seen.append((self.path, body))
        payload = json.dumps({"ok": True, "outcome": "allowed-once"}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


def test_approve_http():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = dsh_client.DshClient(host="127.0.0.1", port=port)
    ok, err = c.approve("req-9", True)
    check("approve(allow) 调用成功", ok and not err, err)
    ok2, _ = c.approve("req-9", False)
    check("approve(deny) 调用成功", ok2)
    allow_body = [b for p, b in Handler.seen if b.get("id") == "req-9"]
    check("请求体带 id 与决定",
          len(allow_body) == 2 and allow_body[0]["decision"] == "allow"
          and allow_body[1]["decision"] == "deny",
          str(allow_body))
    srv.shutdown()


def main():
    app = QApplication(sys.argv)
    test_parse()
    test_bubble(app)
    test_approve_http()
    print(f"\nRESULT {PASS}/{PASS + FAIL} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
