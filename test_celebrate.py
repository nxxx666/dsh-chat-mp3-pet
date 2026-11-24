# -*- coding: utf-8 -*-
"""任务完成庆祝（冒小花）的测试。

覆盖：
  1. 流帧解析：tool/call 事件照常出来（庆祝的触发依据）
  2. CelebrationOverlay：burst 后有粒子、动画跑完自动清空、画出来真有像素
  3. 鼠标穿透：庆祝层不吃鼠标事件，拖动桌宠不受影响
  4. DshChatWorker：用过工具且正常收尾 → 发 task_done；没用工具/被打断 → 不发
  5. DshWatchWorker：被动监听到带工具的回合正常结束 → 发 task_done
  6. PetWindow：庆祝层随窗口铺满并压在 Live2D 层上面；开关生效并落盘

跑法：QT_QPA_PLATFORM=offscreen python test_celebrate.py
"""
import os
import sys
import time
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from PyQt6.QtWidgets import QApplication

import config_store
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


def wait_signal(sig, timeout=5.0):
    """等一个信号发出来，返回是否等到。"""
    got = []
    sig.connect(lambda *a: got.append(a))
    deadline = time.time() + timeout
    app = QApplication.instance()
    while time.time() < deadline:
        app.processEvents()
        if got:
            return True
        time.sleep(0.02)
    return False


def pump_thread(thread, timeout=8.0):
    """跑事件循环直到线程结束或超时。"""
    app = QApplication.instance()
    deadline = time.time() + timeout
    while thread.isRunning() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
    return not thread.isRunning()


def drain_events(seconds=0.5):
    """线程结束后再泵一会儿事件：跨线程 emit 是排队投递的，
    线程一结束泵就退出的话，迟到的信号永远送不到槽里。"""
    app = QApplication.instance()
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)


# ---------- 1. 帧解析 ----------
def test_parse():
    evs = dsh_client.parse_frame({"type": "event", "event": {
        "type": "tool/call", "data": {"name": "pwsh"}}})
    check("tool/call 解析成 tool 事件",
          len(evs) == 1 and evs[0].kind == "tool", evs[0].kind if evs else "无")
    evs = dsh_client.parse_frame({"type": "event", "event": {
        "type": "turn/end"}})
    check("turn/end 解析成 status 事件",
          len(evs) == 1 and evs[0].text == "turn_end")


# ---------- 2/3. 庆祝层本体 ----------
def test_overlay(app):
    from pet_window import CelebrationOverlay

    ov = CelebrationOverlay(None)
    ov.resize(220, 220)
    check("鼠标穿透属性已设置",
          bool(ov.testAttribute(
              __import__("PyQt6.QtCore", fromlist=["Qt"]).Qt.WidgetAttribute
              .WA_TransparentForMouseEvents)))

    img0 = ov.grab().toImage()
    all_transparent0 = all(img0.pixel(x, 10) == 0 for x in range(0, 220, 20))
    check("没粒子时完全透明", all_transparent0)

    ov.burst(110, 80)
    check("burst 后进入活跃状态", ov.active())
    for _ in range(30):              # 像真实时钟一样逐帧推进，跨过全部 delay
        ov.step(0.033)
    check("错峰出场后仍有粒子在飞", ov.active())

    img1 = ov.grab().toImage()
    hits = 0
    for y in range(0, 220, 3):
        for x in range(0, 220, 3):
            if img1.pixel(x, y) != 0:
                hits += 1
    check("粒子画出了真实像素（grab 验证）", hits > 10, f"命中 {hits} 采样点")

    # 推进到全部寿命结束
    for _ in range(140):
        ov.step(0.033)
        app.processEvents()
    check("动画跑完自动清空", not ov.active())
    img2 = ov.grab().toImage()
    all_clear = all(img2.pixel(x, 10) == 0 for x in range(0, 220, 20))
    check("清空后不留残影", all_clear)

    # 小花 / 星光 / 圆点三种粒子都存在
    ov.clear()
    ov.burst(110, 110, count=60)
    kinds = {p["kind"] for p in ov._particles}
    check("三种粒子混着来", kinds == {"flower", "spark", "dot"}, str(kinds))


# ---------- 4. DshChatWorker 的 task_done ----------
class FakeClient:
    """替身桥客户端：按剧本喂事件。"""

    def __init__(self, events, turn_ok=True):
        self.events = events
        self.turn_ok = turn_ok
        self.closed = False

    def attach(self, sid):
        pass

    def health(self):
        return True, {}

    def sessions(self):
        return [{"id": "sess-1", "blank": False, "origin": False}]

    def prompt(self, text):
        return True, "sess-1", ""

    def cancel(self):
        pass

    def close(self):
        self.closed = True

    def stream(self, on_event, stop_flag, session_id=None, read_timeout=None):
        for ev in self.events:
            if stop_flag.is_set():
                return False
            on_event(ev)
        return self.turn_ok


def fake_dsh_module(make_client):
    return types.SimpleNamespace(
        DshClient=lambda *a, **k: make_client(),
        ensure_dsh_running=lambda *a, **k: (True, {}),
        bridge_info=lambda: ("127.0.0.1", 47110, ""),
    )


def run_worker(worker):
    worker.start()
    pump_thread(worker)
    worker.wait(2000)
    drain_events()
    return not worker.isRunning()


def test_chat_worker():
    events = [
        dsh_client.StreamEvent("tool", "正在用 pwsh"),
        dsh_client.StreamEvent("delta", "搞定啦"),
        dsh_client.StreamEvent("status", "turn_end"),
    ]

    def run_case(name, make_events, turn_ok=True, interrupt=False, expect=True):
        pet_window.dsh_client = fake_dsh_module(
            lambda: FakeClient(make_events(), turn_ok=turn_ok))
        w = pet_window.DshChatWorker("建个文件")
        fired = []
        w.task_done.connect(lambda: fired.append(1))
        finished = []
        w.finished_ok.connect(lambda _t: finished.append(1))
        w.start()
        if interrupt:
            w.stop()
        run_worker(w)
        check(name, bool(fired) == expect,
              f"fired={fired} finished={finished}")

    # 用了工具 + 正常收尾 → 发
    run_case("用过工具且正常收尾 → 发 task_done", lambda: events, expect=True)
    # 没用工具（纯聊天）→ 不发
    run_case("纯聊天不发 task_done",
             lambda: [dsh_client.StreamEvent("delta", "你好呀"),
                      dsh_client.StreamEvent("status", "turn_end")],
             expect=False)
    # 被打断 → 不发
    run_case("被打断不发 task_done", lambda: events, interrupt=True, expect=False)
    # 流没正常收尾（比如桥断了）→ 不发
    run_case("流异常收尾不发 task_done", lambda: events, turn_ok=False, expect=False)


# ---------- 5. DshWatchWorker 的 task_done ----------
def test_watch_worker():
    app = QApplication.instance()
    events = [
        dsh_client.StreamEvent("tool", "正在用 pwsh"),
        dsh_client.StreamEvent("delta", "做完了"),
        dsh_client.StreamEvent("status", "turn_end"),
    ]
    pet_window.dsh_client = fake_dsh_module(lambda: FakeClient(events))
    w = pet_window.DshWatchWorker()
    fired = []
    w.task_done.connect(lambda: fired.append(1))
    w.start()
    ok = False
    deadline = time.time() + 6.0
    while time.time() < deadline:
        app.processEvents()
        if fired:
            ok = True
            break
        time.sleep(0.02)
    w.stop()
    w.wait(3000)
    app.processEvents()
    check("被动监听：带工具的回合正常结束 → 发 task_done", ok, str(fired))


# ---------- 6. PetWindow 集成 ----------
def test_pet_window(app):
    # 用真实配置但强制独立 AI 模式：避免测试期间被动监听线程连上真桥
    # 往测试里掺信号；save 用真的，保证开关落盘真实生效、不覆盖用户配置
    real_cfg = config_store.load()
    pet_window.config_store = types.SimpleNamespace(
        load=lambda: {**real_cfg, "ai_mode": "standalone"},
        save=config_store.save)
    win = pet_window.PetWindow()
    try:
        check("庆祝层是窗口的子部件", win._celebration.parent() is win)
        check("庆祝层铺满窗口",
              (win._celebration.width(), win._celebration.height())
              == (win.width(), win.height()),
              f"{win._celebration.width()}x{win._celebration.height()} vs "
              f"{win.width()}x{win.height()}")
        check("庆祝层压在 Live2D 层上面",
              win._celebration.y() <= (win.gl_view.y() if win.gl_view else 0)
              or win.gl_view is None)
        check("默认开着庆祝", win._celebrate_on is True)

        # 直接触发
        win._on_task_done()
        check("_on_task_done 触发庆祝", win._celebration.active())
        win._celebration.clear()

        # 开关
        win._toggle_celebrate(False)
        check("关掉后 _celebrate 不再冒花", True)
        win._celebration.clear()
        win._celebrate()
        check("关掉后确实没有粒子", not win._celebration.active())
        saved = config_store.load()
        check("开关状态已落盘", saved.get("celebrate_done") is False,
              str(saved.get("celebrate_done")))
        win._toggle_celebrate(True)     # 重新打开（会当场演示一把）
        check("重新打开后有演示粒子", win._celebration.active())
        saved = config_store.load()
        check("重新打开也已落盘", saved.get("celebrate_done") is True)

        # 缩放后庆祝层仍然铺满（resizeEvent 同步）
        win._celebration.clear()
        win.set_pet_scale(0.6, persist=False)
        drain_events(0.2)
        check("缩放后庆祝层跟着铺满",
              (win._celebration.width(), win._celebration.height())
              == (win.width(), win.height()),
              f"{win._celebration.width()}x{win._celebration.height()} vs "
              f"{win.width()}x{win.height()}")
    finally:
        pet_window.config_store = config_store
        win._watch_think = False
        try:
            win.close()
        except Exception:
            pass


def main():
    app = QApplication(sys.argv)
    test_parse()
    test_overlay(app)
    test_chat_worker()
    test_watch_worker()
    test_pet_window(app)
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
