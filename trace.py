# -*- coding: utf-8 -*-
"""极轻量的行为轨迹日志。

为什么需要它：Qt 的原生崩溃（Qt6Core 的 0xc0000409 abort）不会走 Python 异常，
也来不及刷 stderr。唯一能定位的办法是「崩溃前最后做了什么」，所以关键动作
先落盘再执行，写文件立刻 flush。

日志位置：exe 旁 `_trace.log`（只保留最近一次运行，启动时清空）。
"""
import os
import sys
import threading
import time
import traceback

import paths

_LOCK = threading.Lock()
_PATH = None
_ENABLED = True

# ── 主线程看门狗 ─────────────────────────────────────────────
# 「未响应」= 主线程停止泵消息，且往往卡在 Qt/GL 原生层，
# Python 异常钩子和 Qt 日志都看不到。办法：主线程一个 QTimer
# 定期摸心跳，后台线程盯着心跳，一旦停了立刻把全线程的
# Python 栈写进日志——卡死瞬间它在哪一行，一目了然。
_LAST_BEAT = time.time()
_STALL_REPORTED = False
_STALL_SINCE = 0.0
_STALL_SECS = 3.0      # 超过 3 秒没心跳算卡死（弹窗阈值是 5 秒，抢在前头）
_STALL_ECHO = 10.0     # 卡死期间每 10 秒再补一帧栈，看有没有动静
_WATCH_STARTED = False


def touch():
    """主线程心跳（QTimer 调）。"""
    global _LAST_BEAT
    _LAST_BEAT = time.time()


def _stacks_text() -> str:
    """抓当前所有线程的 Python 栈（不要求对方配合，卡死也能抓）。"""
    frames = sys._current_frames()
    names = {t.ident: t.name for t in threading.enumerate()}
    out = []
    for ident, frame in frames.items():
        out.append(f"--- thread {ident} ({names.get(ident, '?')}) ---")
        try:
            out.extend(traceback.format_stack(frame))
        except Exception:
            out.append("(栈不可读)")
    return "".join(out)


def _path():
    global _PATH
    if _PATH is None:
        _PATH = os.path.join(paths.base_dir(), "_trace.log")
    return _PATH


def start():
    """开一次运行：清空旧日志，写个启动标记。"""
    global _ENABLED
    try:
        with open(_path(), "w", encoding="utf-8") as f:
            f.write("")
    except OSError:
        _ENABLED = False      # 目录不可写就彻底闭嘴，不能因为日志把自己搞挂
        return
    log("=== start === pid=%d py=%s" % (os.getpid(), time.strftime("%H:%M:%S")))


def start_watchdog():
    """后台线程盯主线程心跳，卡死时自动落盘全线程栈。"""
    global _WATCH_STARTED
    if _WATCH_STARTED:
        return
    _WATCH_STARTED = True

    def _watch():
        global _STALL_REPORTED, _STALL_SINCE
        while True:
            time.sleep(1.0)
            stalled = (time.time() - _LAST_BEAT) > _STALL_SECS
            if stalled and not _STALL_REPORTED:
                _STALL_REPORTED = True
                _STALL_SINCE = time.time()
                log(f"!!! 主线程疑似卡死（>{_STALL_SECS:g}s 无心跳），全线程栈：")
                log(_stacks_text())
            elif stalled and _STALL_REPORTED:
                if time.time() - _STALL_SINCE >= _STALL_ECHO:
                    log(f"!!! 仍然卡死（已 {time.time() - _STALL_SINCE:.0f}s），再抓一帧：")
                    log(_stacks_text())
                    _STALL_SINCE = time.time()
            elif not stalled and _STALL_REPORTED:
                _STALL_REPORTED = False
                log("!!! 主线程恢复响应")

    threading.Thread(target=_watch, name="trace-watchdog", daemon=True).start()


def log(msg: str):
    """记一行。任何异常都吞掉——日志不该影响主流程。"""
    if not _ENABLED:
        return
    try:
        t = time.strftime("%H:%M:%S")
        name = threading.current_thread().name
        with _LOCK:
            with open(_path(), "a", encoding="utf-8") as f:
                f.write(f"[{t}] [{name}] {msg}\n")
    except OSError:
        pass


def dump_tail(lines: int = 40) -> str:
    """读最后几行（诊断窗口/手动排查用）。"""
    try:
        with open(_path(), "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except OSError:
        return "(没有日志)"
