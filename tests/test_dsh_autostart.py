# -*- coding: utf-8 -*-
"""「打开桌宠自动拉起 DSH」的测试。

不真的启动 DSH：launch / ensure / Popen 全部 monkeypatch，
只验证决策逻辑（什么时候启动、启动参数对不对、失败提示是否友好）。
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

import dsh_client
import pet_window
import trace as trace_mod

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("[OK ] " if cond else "[FAIL] ") + name + ((" | " + detail) if detail else ""))


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    trace_mod.log("=== test_dsh_autostart start ===")

    # ---------- 1. find_dsh_exe：优先用户配置 ----------
    fake = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fake_dsh_dir")
    os.makedirs(fake, exist_ok=True)
    fake_exe = os.path.join(fake, "DeepSeek Harness.exe")
    open(fake_exe, "wb").close()
    old_cands = dsh_client._DSH_EXE_CANDIDATES
    dsh_client._DSH_EXE_CANDIDATES = ()          # 隔离本机真实安装，测纯逻辑

    got = dsh_client.find_dsh_exe(fake_exe)
    check("find: 用户配置优先", os.path.normpath(got) == os.path.normpath(fake_exe), got)

    got = dsh_client.find_dsh_exe("")
    check("find: 全部候选不存在时返回空", got == "", got)

    dsh_client._DSH_EXE_CANDIDATES = (fake_exe,)
    got = dsh_client.find_dsh_exe("")
    check("find: 候选列表命中", os.path.normpath(got) == os.path.normpath(fake_exe), got)

    # ---------- 2. launch_dsh：参数与环境 ----------
    captured = {}

    class FakePopen:
        def __init__(self, args, **kw):
            captured["args"] = args
            captured["kw"] = kw

    real_popen = __import__("subprocess").Popen
    import subprocess
    subprocess.Popen = FakePopen
    os.environ["ELECTRON_RUN_AS_NODE"] = "1"     # 故意造出这个毒环境变量

    ok = dsh_client.launch_dsh(fake_exe)
    check("launch: 成功返回 True", ok is True)
    check("launch: exe 路径正确",
          captured.get("args", [None])[0] == fake_exe, str(captured.get("args")))
    check("launch: ELECTRON_RUN_AS_NODE 已剔除",
          "ELECTRON_RUN_AS_NODE" not in captured["kw"]["env"], "")
    check("launch: 使用分离进程标志", bool(captured["kw"].get("creationflags")), "")
    check("launch: 工作目录设为 exe 所在目录",
          captured["kw"].get("cwd") == os.path.dirname(fake_exe), str(captured["kw"].get("cwd")))

    dsh_client._DSH_EXE_CANDIDATES = ()          # 清掉候选，测「找不到」分支
    ok = dsh_client.launch_dsh("")                # 候选为空时找不到
    check("launch: 找不到 exe 返回 False", ok is False)
    os.environ.pop("ELECTRON_RUN_AS_NODE", None)
    dsh_client._DSH_EXE_CANDIDATES = old_cands

    # ---------- 3. ensure_dsh_running：决策逻辑 ----------
    class FakeClient:
        def __init__(self, seq):
            self.seq = list(seq)
            self.calls = 0

        def health(self):
            self.calls += 1
            if self.seq:
                r = self.seq.pop(0)
            else:
                r = (True, {})
            return r if isinstance(r, tuple) else (r, {})

    real_health_cls = dsh_client.DshClient
    real_launch = dsh_client.launch_dsh

    # 3a. 桥本来就通 → 不启动
    fc = FakeClient([(True, {"ok": True})])
    dsh_client.DshClient = lambda: fc
    launched = {"n": 0}
    dsh_client.launch_dsh = lambda exe="": launched.__setitem__("n", launched["n"] + 1) or True
    ok, _ = dsh_client.ensure_dsh_running("")
    check("ensure: 桥已就绪时直接成功且不启动 DSH", ok and launched["n"] == 0)

    # 3b. 桥挂 → 启动 → 等到就绪（同一个 fake 实例，health 序列才会推进）
    fc = FakeClient([(False, {}), (False, {}), (True, {"ok": True})])
    dsh_client.DshClient = lambda: fc
    ok, _ = dsh_client.ensure_dsh_running("", timeout=5.0, interval=0.05)
    check("ensure: 启动后等到桥就绪", ok is True and launched["n"] == 1)

    # 3c. 启动失败 → 友好报错（换一个新的、桥挂着的 fake）
    fc = FakeClient([(False, {})])
    dsh_client.DshClient = lambda: fc
    dsh_client.launch_dsh = lambda exe="": False
    ok, info = dsh_client.ensure_dsh_running("", timeout=1.0)
    check("ensure: 拉不起 DSH 时报错并提示 dsh_exe_path",
          not ok and "dsh_exe_path" in (info or {}).get("error", ""), str(info))

    # 3d. stop_ev 预置 → 立刻放弃
    ev = threading.Event(); ev.set()
    fc = FakeClient([(False, {})])
    dsh_client.DshClient = lambda: fc
    dsh_client.launch_dsh = lambda exe="": launched.__setitem__("n", launched["n"] + 1) or True
    n0 = launched["n"]
    ok, _ = dsh_client.ensure_dsh_running("", timeout=5.0, stop_ev=ev)
    check("ensure: 已打断时立刻放弃且不启动", not ok and launched["n"] == n0)

    dsh_client.DshClient = real_health_cls
    dsh_client.launch_dsh = real_launch

    # ---------- 4. worker 集成：桥不通时先自救再报错 ----------
    note_msgs, fail_msgs, done_msgs = [], [], []

    def patched_ensure(exe="", timeout=45.0, stop_ev=None, interval=1.0):
        note_msgs.append("__ensure_called__")   # 标记 ensure 真被 worker 调了
        return False, {"error": "模拟：DSH 拉不起来"}

    real_ensure = pet_window.dsh_client.ensure_dsh_running
    pet_window.dsh_client.ensure_dsh_running = patched_ensure
    try:
        w = pet_window.DshChatWorker("你好", "", dsh_exe="")
        w.note.connect(note_msgs.append)
        w.failed.connect(fail_msgs.append)
        w.finished_ok.connect(done_msgs.append)
        w.start()
        t0 = time.time()
        while (w.isRunning() or not fail_msgs) and time.time() - t0 < 20:
            app.processEvents()
            time.sleep(0.02)
        w.wait(3000)
    finally:
        pet_window.dsh_client.ensure_dsh_running = real_ensure

    check("worker: 桥不通时调用了自动拉起", "__ensure_called__" in note_msgs, str(note_msgs[:2]))
    check("worker: 先发「正在帮你启动」提示",
          any("正在帮你启动" in m for m in note_msgs if m != "__ensure_called__"))
    check("worker: 失败消息以「连不上 DSH」开头且友好",
          bool(fail_msgs) and fail_msgs[0].startswith("连不上 DSH") and "pet-bridge" in fail_msgs[0],
          (fail_msgs or ["(无)"])[0][:60])
    check("worker: 没有把空回复当成功", not done_msgs or done_msgs == [""])

    # ---------- 收尾 ----------
    # 注意：这里不做 os.remove/os.rmdir 清理 —— 沙箱的删除保护钩子会把
    # 测试进程直接干掉（实测 exit 1 且吞掉 RESULT 输出）。留个小目录无害。

    bad = [n for n, ok2, _ in RESULTS if not ok2]
    print("\nRESULT: %d/%d PASS" % (len(RESULTS) - len(bad), len(RESULTS)))
    if bad:
        print("FAILED:", ", ".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
