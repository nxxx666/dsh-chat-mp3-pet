# -*- coding: utf-8 -*-
"""接 DSH（DeepSeek Harness）的客户端。

桌宠在「DSH 模式」下不再自己调模型：
- 说的话交给 DSH 的当前会话 → 历史、记忆插件、工具全部由 DSH 负责
- DSH 的回复通过桥插件的 SSE 流回气泡

桥插件（dsh-pet-bridge）跑在 DSH 进程里，在 127.0.0.1 上开一个小 HTTP 服务，
端口写在 ~/.dsh/pet-bridge.json，这里读它来发现。

设计的容错点：
- 端口发现失败 → 依次试默认端口
- 流里的帧结构以后可能变 → 解析尽量宽松，认不出的帧不抛异常，只丢日志
- 拿不到流式增量时，退化成「整条消息一次性显示」（assistant/message 事件）
"""

import json
import os
import threading
import time

import requests

STATUS_FILE = os.path.join(os.path.expanduser("~"), ".dsh", "pet-bridge.json")
DEFAULT_PORT = 47110
TIMEOUT = 6.0

# 模型流 chunk 的类型标签（dsh-llm 的 StreamChunk）
# 正文和思考是两个独立通道，不看类型就会把思考当正文念出来。
CHUNK_TEXT = "text-delta"          # 正文增量，要显示
CHUNK_REASONING = "reasoning-delta"  # 思考增量，不显示
# 内容块里属于「不是给人看的话」的类型（assistant/message 的 content 数组会混着它们）
BLOCK_HIDDEN = ("reasoning", "thinking", "tool-call", "tool_use",
                "tool-result", "tool_result", "image", "file")

# 关心的持久事件（其余忽略）
EVENT_ASSISTANT = "assistant/message"
EVENT_USER = "user/message"
EVENT_TOOL_CALL = "tool/call"
EVENT_TOOL_RESULT = "tool/result"
EVENT_TURN_END = "turn/end"
EVENT_TURN_START = "turn/start"


def bridge_info():
    """读桥插件写的状态文件，返回 (host, port, error)。"""
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        host = data.get("host") or "127.0.0.1"
        port = int(data.get("port") or DEFAULT_PORT)
        return host, port, ""
    except FileNotFoundError:
        return "127.0.0.1", DEFAULT_PORT, "找不到 ~/.dsh/pet-bridge.json（DSH 没开，或桥插件没装）"
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return "127.0.0.1", DEFAULT_PORT, f"状态文件读不了：{exc}"


def _text_of(value):
    """从各种形状里抠出文本：字符串 / {text} / {delta} / [{...}] / {content}

    只做形状兜底，不区分思考与正文；要区分用 _visible_text_of。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text_of(v) for v in value)
    if isinstance(value, dict):
        for key in ("text", "delta", "content", "value", "output_text"):
            if key in value:
                got = _text_of(value[key])
                if got:
                    return got
        # OpenAI 风格：{"type":"text-delta","text":"..."} 已在上面覆盖；
        # 再兜一层 {"choices":[{"delta":{"content":"..."}}]}
        if "choices" in value:
            return _text_of(value["choices"])
        return ""
    return ""


def _visible_text_of(value):
    """只取「人该看到的话」，跳过思考块和工具块。

    assistant/message 的 content 是 ContentBlock 数组，形如
    [{"type": "reasoning", "text": "..."}, {"type": "text", "text": "..."}]。
    直接用 _text_of 会把两段拼在一起，答案前面就多出一段内心独白。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_visible_text_of(v) for v in value)
    if isinstance(value, dict):
        btype = value.get("type")
        if isinstance(btype, str) and btype in BLOCK_HIDDEN:
            return ""
        # 先走容器键（content / message / blocks 里可能有多个块）
        for key in ("content", "message", "blocks", "parts"):
            if key in value:
                return _visible_text_of(value[key])
        if "text" in value:
            return _text_of(value["text"])
        if "output_text" in value:
            return _text_of(value["output_text"])
        if "choices" in value:
            return _visible_text_of(value["choices"])
        return ""
    return ""


class StreamEvent:
    """归一化后的流事件。

    kind:
      delta     — 流式增量（text 是要追加的片段）
      message   — 一整条 assistant 消息（text 是完整内容）
      tool      — 工具调用/结果（text 是描述）
      status    — 状态变化（thinking / turn_end）
      error     — 出错
    """

    __slots__ = ("kind", "text", "raw")

    def __init__(self, kind, text="", raw=None):
        self.kind = kind
        self.text = text
        self.raw = raw

    def __repr__(self):
        return f"StreamEvent({self.kind!r}, {self.text[:30]!r})"


def parse_frame(frame):
    """把一帧 SessionFollowFrame 转成零个或多个 StreamEvent。"""
    if not isinstance(frame, dict):
        return []
    ftype = frame.get("type")

    if ftype == "assistant-stream":
        inner = frame.get("frame") or {}
        itype = inner.get("type")
        if itype == "chunk":
            chunk = inner.get("chunk")
            ctype = chunk.get("type") if isinstance(chunk, dict) else None
            if ctype == CHUNK_REASONING:
                # 思考过程：气泡里只提示「在想」，不念内容
                return [StreamEvent("status", "thinking", frame)]
            if ctype == CHUNK_TEXT:
                text = chunk.get("text") or ""
                return [StreamEvent("delta", text, frame)] if text else []
            if isinstance(ctype, str):
                # block-start / block-end / usage / finish / tool-call-delta 都不进气泡
                return []
            # 没有类型标签的老形状：退回宽松提取（thinking 模式下可能带思考，
            # 但此路径只在新旧协议不一致时才走到）
            text = _text_of(chunk)
            return [StreamEvent("delta", text, frame)] if text else []
        if itype == "end":
            return []          # 结束标记，等 turn/end 统一收尾
        return []

    if ftype == "event":
        ev = frame.get("event") or {}
        etype = ev.get("type")
        data = ev.get("data") or {}
        if etype == EVENT_ASSISTANT:
            # 实测 data 有时是 {content:[...]}，有时外面还套一层 {message:{...}}
            # content 里混着 reasoning 块，必须只取 text 块
            inner = data.get("message") if isinstance(data, dict) else None
            text = _visible_text_of(data) or _visible_text_of(inner)
            return [StreamEvent("message", text, frame)] if text else []
        if etype == EVENT_USER:
            return []          # 自己刚说的话，不重复显示
        if etype == EVENT_TOOL_CALL:
            name = data.get("name") or data.get("tool") or "工具"
            return [StreamEvent("tool", f"正在用 {name}", frame)]
        if etype == EVENT_TOOL_RESULT:
            return []
        if etype == EVENT_TURN_START:
            return [StreamEvent("status", "thinking", frame)]
        if etype == EVENT_TURN_END:
            return [StreamEvent("status", "turn_end", frame)]
        return []

    if ftype == "snapshot":
        return []              # 开场快照（历史），桌宠不回溯显示

    if ftype == "bridge/error":
        return [StreamEvent("error", str(frame.get("error") or "桥插件报错"), frame)]

    return []


class DshClient:
    """桥插件的 HTTP 客户端（同步调用，调用方负责放线程里）。"""

    def __init__(self, host=None, port=None):
        if host is None or port is None:
            h, p, _err = bridge_info()
            host = host or h
            port = port or p
        self.host = host
        self.port = port
        self.base = f"http://{host}:{port}"
        self.session_id = None
        self._closed = False

    # ---------- 基础 ----------
    def health(self):
        """返回 (ok, info)。DSH 没开时 ok=False，info 里带原因。"""
        try:
            r = requests.get(f"{self.base}/health", timeout=TIMEOUT)
            data = r.json()
            return bool(data.get("ok")), data
        except requests.RequestException as exc:
            _, _, why = bridge_info()
            return False, {"error": why or str(exc)}

    def sessions(self):
        try:
            r = requests.get(f"{self.base}/sessions", timeout=TIMEOUT)
            data = r.json()
            return data.get("items") or []
        except (requests.RequestException, ValueError):
            return []

    def ensure_session(self, cwd=None):
        """确保有一个可用的会话 id：客户端记着的 → 最近一个真会话 → 新建。"""
        if self.session_id:
            return self.session_id
        # 桥按更新时间倒序给，第一个就是「上次聊的」
        for item in self.sessions():
            sid = item.get("id")
            if sid and not item.get("blank") and not item.get("origin"):
                self.session_id = sid
                return sid
        return self.new_session(cwd=cwd)

    def new_session(self, cwd=None, agent_preset=None):
        body = {}
        if cwd:
            body["cwd"] = cwd
        if agent_preset:
            body["agentPreset"] = agent_preset
        r = requests.post(f"{self.base}/session/new", json=body, timeout=TIMEOUT)
        data = r.json()
        sid = data.get("sessionId")
        if sid:
            self.session_id = sid
        return sid

    def attach(self, session_id):
        self.session_id = session_id

    def prompt(self, text):
        """投递一句话。返回 (ok, session_id, error)。"""
        if not (text or "").strip():
            return False, self.session_id, "空消息"
        body = {"text": text}
        if self.session_id:
            body["sessionId"] = self.session_id
        try:
            r = requests.post(f"{self.base}/prompt", json=body, timeout=20.0)
            data = r.json()
            if not data.get("ok"):
                return False, self.session_id, data.get("error") or "投递失败"
            sid = data.get("sessionId")
            if sid:
                self.session_id = sid
            return True, sid, ""
        except requests.RequestException as exc:
            return False, self.session_id, f"连不上桥插件：{exc}"

    def cancel(self):
        """取消当前回合。

        没有 sessionId 也照样发：桥会用「最近一次对话的会话」兜底
        （第一轮的 session id 是回复开始后才异步拿到的，正好覆盖）。
        """
        try:
            body = {"sessionId": self.session_id} if self.session_id else {}
            r = requests.post(f"{self.base}/cancel",
                              json=body, timeout=TIMEOUT)
            return bool(r.json().get("ok"))
        except (requests.RequestException, ValueError):
            return False

    # ---------- 流 ----------
    def stream(self, on_event, stop_flag=None, session_id=None):
        """阻塞地读 SSE，把归一化事件喂给 on_event。

        stop_flag 是个 threading.Event；置位后尽快退出（桌宠点「停」时用）。
        返回 True 表示正常收完一轮（收到 turn_end 或流结束）。
        """
        sid = session_id or self.session_id
        if not sid:
            on_event(StreamEvent("error", "还没有会话"))
            return False

        stop_flag = stop_flag or threading.Event()
        url = f"{self.base}/stream?sessionId={sid}"
        try:
            with requests.get(url, stream=True, timeout=(TIMEOUT, None)) as resp:
                if resp.status_code != 200:
                    on_event(StreamEvent("error", f"流打不开（HTTP {resp.status_code}）"))
                    return False
                buf = b""
                for raw in resp.iter_lines(decode_unicode=False):
                    if stop_flag.is_set() or self._closed:
                        break
                    if raw is None:
                        continue
                    if not raw:
                        continue
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    try:
                        frame = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    for ev in parse_frame(frame):
                        on_event(ev)
                        if ev.kind == "status" and ev.text == "turn_end":
                            return True
                return True
        except requests.RequestException as exc:
            if not stop_flag.is_set() and not self._closed:
                on_event(StreamEvent("error", f"流断了：{exc}"))
            return False

    def close(self):
        self._closed = True


def wait_for_bridge(seconds=8.0, interval=0.5):
    """等桥插件起来（DSH 刚启动时插件可能还没监听）。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        ok, info = DshClient().health()
        if ok:
            return True, info
        time.sleep(interval)
    ok, info = DshClient().health()
    return ok, info


# ---------- 自动拉起 DSH 桌面版 ----------
# 桥插件活在 DSH 进程里：DSH 没开 → 47110 没人听 → DSH 模式没法聊。
# 这里负责「找到 DSH 装在哪」和「把它拉起来」，供桌宠在连不上桥时自救。

_DSH_EXE_CANDIDATES = (
    r"D:\dsh\新建文件夹\DeepSeek Harness.exe",          # 本机实际安装位置
    r"%LOCALAPPDATA%\Programs\DeepSeek Harness\DeepSeek Harness.exe",
    r"%LOCALAPPDATA%\Programs\dsh\DeepSeek Harness.exe",
    r"C:\Program Files\DeepSeek Harness\DeepSeek Harness.exe",
)


def find_dsh_exe(prefer=""):
    """按 用户配置 → 常见安装位置 → Programs 目录扫描 的顺序找 DSH exe。

    返回存在的那个路径；都找不到返回 ""。
    """
    import glob

    tries = [prefer]
    tries += _DSH_EXE_CANDIDATES
    for t in tries:
        if t and os.path.isfile(os.path.expandvars(t)):
            return os.path.expandvars(t)
    # 最后一搏：用户级 Electron 安装目录通常在 %LOCALAPPDATA%\Programs\<名字>\
    for pat in (
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\*") + os.sep + "DeepSeek Harness.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\*\*") + os.sep + "DeepSeek Harness.exe",
    ):
        for hit in sorted(glob.glob(pat))[:1]:
            if os.path.isfile(hit):
                return hit
    return ""


def launch_dsh(exe=""):
    """启动 DSH 桌面版（分离进程）。返回是否成功拉起。

    坑：若环境里有 ELECTRON_RUN_AS_NODE=1，Electron 会降级成纯 node，
    桌面版起不来——启动子进程前必须把它从环境里剔掉。
    """
    path = find_dsh_exe(exe)
    if not path:
        return False
    env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
    flags = 0
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        import subprocess

        subprocess.Popen(
            [path],
            cwd=os.path.dirname(path),
            env=env,
            creationflags=flags,
            close_fds=True,
        )
        return True
    except OSError:
        return False


def ensure_dsh_running(exe="", timeout=45.0, stop_ev=None, interval=1.0):
    """确保 DSH 在跑：桥能连上就直接成功；连不上就拉起 DSH 并等桥就绪。

    返回 (ok, info)。stop_ev 置位时立刻放弃。
    """
    ok, info = DshClient().health()
    if ok:
        return True, info
    if stop_ev is not None and stop_ev.is_set():
        return False, info
    if not launch_dsh(exe):
        return False, {"error": "没找到 DSH 程序（可在 config.json 的 dsh_exe_path 里指定位置）"}
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_ev is not None and stop_ev.is_set():
            return False, {"error": "已取消"}
        time.sleep(interval)
        ok, info = DshClient().health()
        if ok:
            return True, info
    return False, {"error": "DSH 启动后桥仍未就绪（等待 %.0fs 超时）" % timeout}
