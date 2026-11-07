"""桌宠主窗口：透明无边框窗口 + 拖拽 + 聊天气泡。

关键点：
1. 透明窗口需要 WA_TranslucentBackground + FramelessWindowHint
2. 推理放 QThread，主线程只负责刷新 UI，否则拖不动、卡死
3. 流式返回用 pyqtSignal 逐字推给界面
"""

import os
import random
import threading
import time

from PyQt6.QtCore import (QFileSystemWatcher, QPoint, QPointF, QRectF, Qt,
                          QThread, QTimer, QUrl, pyqtSignal)
from PyQt6.QtGui import (QAction, QBrush, QColor, QDesktopServices, QFont,
                         QFontMetrics, QPainter, QPainterPath, QPen)
from PyQt6.QtWidgets import (QApplication, QDialog, QInputDialog, QLineEdit,
                             QMenu, QMessageBox, QWidget)

import ai_settings
import config_store
import live2d_view
import llm_client
import memory
import memory_dialog
import paths
import persona
import text_clean
import trace
from music_player import MUSIC_DIR, MusicPlayer, scan_music
from pet_widget import load_renderer, PetAnimRenderer

try:
    import dsh_client          # DSH 桥客户端；缺失时自动退回独立 AI 模式
except Exception as _dsh_exc:   # pragma: no cover
    dsh_client = None
    print("[dsh] 桥客户端不可用：%r" % (_dsh_exc,))

MODE_STANDALONE = "standalone"   # 桌宠自己调模型（DeepSeek / 中转 / Ollama）
MODE_DSH = "dsh"                 # 把话交给 DSH 的会话，记忆和工具都用 DSH 的

SYSTEM_PROMPT = persona.DEFAULT_PERSONA   # 兜底用；实际人设在 __init__ 里按预设加载

WINDOW_W = 220          # 角色区宽度（序列帧 / 立绘模式）
WINDOW_H = 220
LIVE2D_W = 250          # Live2D 模式用 2:3 竖幅，贴合模型画布 1024x1536
LIVE2D_H = 375
BUBBLE_MAX_W = 360      # 气泡最大宽度（加宽后长回复排版更舒服）
BUBBLE_MAX_H = 620      # 单页文字最大高度上限（实际还会按屏幕高度打折）

# 短期上下文：条数 + 字数双上限。超出的部分不是丢掉，而是被压成长期记忆。
MAX_HISTORY = 16        # 最多带 16 条（8 轮）
HISTORY_CHAR_BUDGET = 3200   # 这些消息的总字数上限（防止一条超长回复吃满上下文）
PAINT_INTERVAL = 0.033  # 流式刷气泡的最小间隔（约 30fps，别每个 token 都改窗口几何）
SUMMARY_BATCH = 4       # 攒够这么多条旧消息才调一次模型压缩（省钱、省等待）
SUMMARY_MIN_CHARS = 300      # 或者累计字数到了也压一次

# 桌宠整体缩放（右键菜单 / 鼠标滚轮）
SCALE_MIN = 0.5         # 最小 50%
SCALE_MAX = 2.0         # 最大 200%
SCALE_WHEEL_STEP = 0.05     # 滚轮一格
SCALE_BUTTON_STEP = 0.10    # 菜单「放大/缩小」一次
SCALE_PRESETS = (60, 75, 100, 125, 150, 180)


# ============================================================
# 推理线程
# ============================================================
class ChatWorker(QThread):
    chunk = pyqtSignal(str)
    failed = pyqtSignal(str)
    finished_ok = pyqtSignal(str)

    def __init__(self, messages, ai: dict):
        super().__init__()
        self.messages = messages
        self.ai = ai          # 一次性的 AI 参数快照，避免线程里读配置
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        buf = []
        try:
            for piece in llm_client.chat_stream(
                self.messages,
                model=self.ai.get("model", ""),
                host=self.ai.get("host", llm_client.DEFAULT_HOST),
                provider=self.ai.get("provider", llm_client.PROVIDER_DEEPSEEK),
                api_key=self.ai.get("api_key", ""),
                base_url=self.ai.get("base_url", llm_client.DEEPSEEK_BASE_URL),
                thinking=bool(self.ai.get("thinking", False)),
                max_tokens=int(self.ai.get("max_tokens", 4096)),
            ):
                if self._stop:
                    return
                buf.append(piece)
                self.chunk.emit(piece)
            self.finished_ok.emit("".join(buf))
        except llm_client.LLMError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # 兜底，避免线程静默死掉
            self.failed.emit(f"出错了：{exc}")


class DshChatWorker(QThread):
    """把一句话交给 DSH 的会话，再把回复流式取回来。

    和 ChatWorker 的区别：这里不碰模型，也不拼 system——记忆、工具、上下文
    全在 DSH 里（包括它装的记忆插件）。桌宠只负责「传话」和「显示」。

    帧的来源有两种：流式增量（delta）和提交后的整条消息（message）。
    两者都有时取更长的那个，避免模型只吐了半句就结束导致气泡缺字。
    """

    chunk = pyqtSignal(str)
    note = pyqtSignal(str)        # 「正在用 xxx」这类过程提示
    failed = pyqtSignal(str)
    finished_ok = pyqtSignal(str)
    session_ready = pyqtSignal(str)

    def __init__(self, text: str, session_id: str = "", dsh_exe: str = ""):
        super().__init__()
        self.text = text
        self.session_id = session_id or ""
        self.dsh_exe = dsh_exe or ""
        self._stop_ev = threading.Event()
        self.client = None

    def stop(self):
        self._stop_ev.set()
        try:
            if self.client is not None:
                self.client.cancel()
        except Exception:
            pass

    def run(self):
        buf = []
        whole = []
        try:
            self.client = dsh_client.DshClient()
            if self._stop_ev.is_set():
                # 打断来得比线程起步还早：话还没送进 DSH，直接收工
                self.finished_ok.emit("")
                return
            if self.session_id:
                self.client.attach(self.session_id)

            ok, info = self.client.health()
            if not ok and not self._stop_ev.is_set():
                # 桥没在监听 = DSH 大概率没开。自救：帮用户把 DSH 拉起来再等就绪
                self.note.emit("DSH 桌面版没开着，正在帮你启动…（首次启动要等一会儿）")
                ok, info = dsh_client.ensure_dsh_running(
                    self.dsh_exe, timeout=45.0, stop_ev=self._stop_ev)
                if ok:
                    self.note.emit("DSH 已就绪，继续～")
            if not ok:
                why = (info or {}).get("error") or "桥插件没在监听"
                self.failed.emit(
                    f"连不上 DSH：{why}\n"
                    f"（先打开 DSH 桌面版试试；还不行就检查 pet-bridge 插件是否装好）")
                return

            good, sid, err = self.client.prompt(self.text)
            if not good:
                self.failed.emit(err or "投递失败")
                return
            if sid:
                self.client.attach(sid)
                self.session_ready.emit(sid)

            def on_event(ev):
                if self._stop_ev.is_set():
                    return
                if ev.kind == "delta":
                    buf.append(ev.text)
                    self.chunk.emit(ev.text)
                elif ev.kind == "message":
                    whole.append(ev.text)
                elif ev.kind == "tool":
                    self.note.emit(ev.text)
                elif ev.kind == "error":
                    self.failed.emit(ev.text)

            self.client.stream(on_event, self._stop_ev, session_id=sid)
            if self._stop_ev.is_set():
                # 线程刚起步那会儿被打断的话，stop() 里的 cancel 可能抢在
                # prompt 前发（甚至没发）。这里再补一发，确保 DSH 侧这轮真的停
                try:
                    self.client.cancel()
                except Exception:
                    pass

            # 增量拼出来的和提交的整条消息取更完整的那个
            streamed = "".join(buf)
            committed = "".join(whole)
            final = committed if len(committed) > len(streamed) else streamed
            if final and not streamed:
                self.chunk.emit(final)      # 没有增量时补一次，让气泡有内容
            self.finished_ok.emit(final)
        except Exception as exc:      # 兜底：线程不能静默死
            self.failed.emit(f"出错：{exc}")
        finally:
            try:
                if self.client is not None:
                    self.client.close()
            except Exception:
                pass


class SummaryWorker(QThread):
    """把「被挤出上下文的旧对话」压成一段长期摘要。

    和聊天用同一套 AI 参数，但温度调低、max_tokens 限死——
    这一步是后台静默做的，失败也不该影响用户聊天。
    """

    ok = pyqtSignal(str)
    bad = pyqtSignal(str)

    def __init__(self, prev: str, lines, ai: dict):
        super().__init__()
        self.prev = prev
        self.lines = list(lines)
        self.ai = ai

    def run(self):
        try:
            text = llm_client.chat_once(
                memory.summary_prompt(self.prev, self.lines),
                provider=self.ai.get("provider", llm_client.PROVIDER_DEEPSEEK),
                model=self.ai.get("model", ""),
                host=self.ai.get("host", llm_client.DEFAULT_HOST),
                api_key=self.ai.get("api_key", ""),
                base_url=self.ai.get("base_url", llm_client.DEEPSEEK_BASE_URL),
                thinking=False,
                max_tokens=700,
                temperature=0.3,
            )
            self.ok.emit((text or "").strip())
        except Exception as exc:
            self.bad.emit(str(exc))


# ============================================================
# 气泡（独立窗口，跟着桌宠走）
# ============================================================
TAIL = 12          # 小尾巴占用的尺寸


class Bubble(QWidget):
    """对话气泡。尾巴方向可切换，保证永远不与桌宠窗口重叠。

    长回复：单页放不下时自动切成多页轮播（窗口是鼠标穿透的，没法滚动）。
    """

    size_changed = pyqtSignal()      # 尺寸变了，外层要重新摆位

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.text = ""
        self.font = QFont("Microsoft YaHei", 10)
        self._pad = 14
        self.tail_side = "bottom"   # bottom / top / left / right

        self.pages = []             # 多页内容（空 = 单页模式）
        self.page_index = 0
        self._page_timer = QTimer(self)
        self._page_timer.timeout.connect(self._next_page)

    # ---------- 对外 ----------
    def set_text(self, text: str):
        """单页显示。文字超出一页时只留最后能放下的部分（适合流式输出）。"""
        self._stop_paging()
        self.text = text
        self.pages = []
        self.page_index = 0
        self._resize_to_fit()
        if not self.isVisible():
            self.show()
        self.raise_()

    def set_pages(self, pages):
        """多页轮播。pages 为 [str, ...]，首页立即显示。"""
        self._stop_paging()
        self.pages = [p for p in (pages or []) if p.strip()]
        if not self.pages:
            self.set_text("")
            return
        self.page_index = 0
        self.text = self.pages[0]
        self._resize_to_fit()
        if not self.isVisible():
            self.show()
        self.raise_()
        if len(self.pages) > 1:
            self._page_timer.start(self._page_delay(self.pages[0]))

    def clear_paging(self):
        self._stop_paging()

    def max_text_height(self) -> int:
        """单页文字可用高度（随屏幕大小变，避免长回复顶出屏幕）。"""
        scr = QApplication.primaryScreen()
        h = scr.availableGeometry().height() if scr else 1080
        return int(min(BUBBLE_MAX_H, max(240, h * 0.55)))

    def paginate(self, text: str):
        """把长文按「一页放得下」切分。按句子边界切，尽量不切断词。"""
        text = (text or "").strip()
        if not text:
            return [""]
        avail_w = BUBBLE_MAX_W - self._pad * 2
        limit = self.max_text_height()

        if self._text_height(text, avail_w) <= limit:
            return [text]

        import re
        parts = re.split(r"(?<=[。！？!?；;\n])", text)
        pages, cur = [], ""
        for part in parts:
            cand = cur + part
            if cur and self._text_height(cand, avail_w) > limit:
                pages.append(cur)
                cur = part
            else:
                cur = cand
            # 单个句子就超过一页（长段无标点）→ 硬切
            while self._text_height(cur, avail_w) > limit:
                cut = self._cut_for_height(cur, avail_w, limit)
                if cut <= 0:
                    break
                pages.append(cur[:cut])
                cur = cur[cut:]
        if cur.strip():
            pages.append(cur)
        return pages or [text]

    def set_tail_side(self, side: str):
        if side != self.tail_side:
            self.tail_side = side
            self._resize_to_fit()

    # ---------- 分页内部 ----------
    @staticmethod
    def _page_delay(text: str) -> int:
        """阅读时间：基础 2.6 秒 + 每字 25ms，4~9 秒封顶。"""
        return int(min(9000, max(4000, 2600 + len(text) * 25)))

    def _next_page(self):
        if len(self.pages) <= 1:
            self._stop_paging()
            return
        self.page_index = (self.page_index + 1) % len(self.pages)
        self.text = self.pages[self.page_index]
        self._resize_to_fit()
        self._page_timer.start(self._page_delay(self.text))

    def _stop_paging(self):
        if self._page_timer.isActive():
            self._page_timer.stop()

    # ---------- 测量 ----------
    def _text_height(self, text: str, avail_w: int) -> int:
        fm = QFontMetrics(self.font)
        rect = fm.boundingRect(
            0, 0, avail_w, 100000,
            int(Qt.TextFlag.TextWordWrap), text or " ")
        return rect.height()

    def _cut_for_height(self, text: str, avail_w: int, limit: int) -> int:
        """二分找「多长的前缀还能放进一页」，返回字符下标。"""
        lo, hi = 1, len(text)
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if self._text_height(text[:mid], avail_w) <= limit:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def _clip_to_page(self, text: str) -> str:
        """流式阶段用：超出一页就只显示尾部（最新内容）。"""
        avail_w = BUBBLE_MAX_W - self._pad * 2
        limit = self.max_text_height()
        if self._text_height(text, avail_w) <= limit:
            return text
        # 先按容量粗裁到一页附近，避免二分时反复给全文排版（流式每 chunk 一次）
        fm = QFontMetrics(self.font)
        lines = max(1, limit // max(1, fm.height()))
        per_line = max(1, avail_w // max(1, fm.averageCharWidth()))
        budget = int(lines * per_line * 1.2) + 20
        if len(text) > budget:
            text = text[-budget:]
            if self._text_height(text, avail_w) <= limit:
                return "…" + text.lstrip("\n")
        # 细调：二分找「尾部一页能放下」的起点
        lo, hi = 0, len(text)
        best = len(text)
        while lo <= hi:
            mid = (lo + hi) // 2
            if self._text_height(text[mid:], avail_w) <= limit:
                best = mid
                hi = mid - 1
            else:
                lo = mid + 1
        return "…" + text[best:].lstrip("\n")

    def _display_text(self) -> str:
        """当前实际绘制的文字：多页模式用当前页，单页模式超长则裁到尾部。"""
        if self.pages:
            return self.text
        return self._clip_to_page(self.text)

    def _resize_to_fit(self):
        fm = QFontMetrics(self.font)
        shown = self._display_text()
        # 先按「水平方向可用宽度」量一次文字高度
        avail_w = BUBBLE_MAX_W - self._pad * 2
        rect = fm.boundingRect(
            0, 0, avail_w, 100000,
            int(Qt.TextFlag.TextWordWrap), shown or " "
        )
        # 加 8px 宽度 / 4px 高度余量，抵消度量与实际绘制的微小出入，避免意外折行
        w = rect.width() + self._pad * 2 + 8
        h = rect.height() + self._pad * 2 + 4
        if len(self.pages) > 1:
            h += 18          # 右下角页码占位
        # 尾巴占哪一边，就给哪一边留空间（与 paintEvent 完全一致）
        if self.tail_side in ("bottom", "top"):
            h += TAIL
        else:
            w += TAIL
        new_size = (max(80, w), max(40, h))
        if new_size != (self.width(), self.height()):
            self.resize(*new_size)
            self.size_changed.emit()

    def _body_rect(self) -> QRectF:
        w, h = self.width(), self.height()
        if self.tail_side == "bottom":
            return QRectF(1, 1, w - 2, h - 1 - TAIL)
        if self.tail_side == "top":
            return QRectF(1, TAIL, w - 2, h - 1 - TAIL)
        if self.tail_side == "left":
            return QRectF(TAIL, 1, w - 1 - TAIL, h - 2)
        return QRectF(1, 1, w - 1 - TAIL, h - 2)   # right

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self._body_rect()
        w, h = self.width(), self.height()

        path = QPainterPath()
        path.addRoundedRect(r, 12, 12)

        # 小尾巴
        tail = QPainterPath()
        if self.tail_side == "bottom":
            cx = w / 2
            tail.moveTo(cx - 8, r.bottom() - 1)
            tail.lineTo(cx, h - 2)
            tail.lineTo(cx + 8, r.bottom() - 1)
        elif self.tail_side == "top":
            cx = w / 2
            tail.moveTo(cx - 8, r.top() + 1)
            tail.lineTo(cx, 2)
            tail.lineTo(cx + 8, r.top() + 1)
        elif self.tail_side == "left":
            cy = h / 2
            tail.moveTo(r.left() + 1, cy - 8)
            tail.lineTo(2, cy)
            tail.lineTo(r.left() + 1, cy + 8)
        else:  # right
            cy = h / 2
            tail.moveTo(r.right() - 1, cy - 8)
            tail.lineTo(w - 2, cy)
            tail.lineTo(r.right() - 1, cy + 8)
        tail.closeSubpath()
        path = path.united(tail)

        p.setBrush(QColor(255, 255, 255, 246))
        p.setPen(QPen(QColor(90, 143, 219, 200), 1.4))
        p.drawPath(path)

        p.setPen(QColor("#1f2d3d"))
        p.setFont(self.font)
        text_rect = r.adjusted(self._pad, self._pad, -self._pad, -self._pad)
        if len(self.pages) > 1:
            text_rect = text_rect.adjusted(0, 0, 0, -14)   # 给页码留位
        p.drawText(
            text_rect,
            int(Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft
                | Qt.AlignmentFlag.AlignVCenter),
            self._display_text(),
        )

        # 页码：长回复分页时提示「第几页 / 共几页」
        if len(self.pages) > 1:
            f = QFont(self.font)
            f.setPointSize(max(7, self.font.pointSize() - 2))
            p.setFont(f)
            p.setPen(QColor(120, 132, 150, 220))
            p.drawText(
                r.adjusted(0, 0, -self._pad, -6),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom),
                f"{self.page_index + 1} / {len(self.pages)}",
            )

    def hideEvent(self, e):
        self._stop_paging()
        super().hideEvent(e)


# ============================================================
# 主窗口
# ============================================================
class PetWindow(QWidget):
    def __init__(self, model=None, host=None):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # 配置必须在建窗口之前读：大小倍率决定窗口尺寸
        self.cfg = config_store.load()
        self.pet_scale = self._clamp_scale(self.cfg.get("pet_scale") or 1.0)
        self.renderer = load_renderer()

        # Live2D 角色层：能起来就用 GL 画，起不来自动回退到序列帧/立绘
        self.gl_view = None
        self.live2d_mode = self._setup_live2d()
        self.setFixedSize(*self._scaled_size())
        self.setWindowTitle("桌宠")

        # AI 后端参数：命令行 --model / --host 只在显式给出时覆盖配置
        self._cli_model = model
        self._cli_host = host
        self.bubble = Bubble()
        self.bubble.size_changed.connect(self._position_bubble)
        self.persona_name = persona.active_name() or "大肥鱼"
        self.persona_text = persona.load(self.persona_name)
        self.setWindowTitle(self.persona_name)
        self.history = [{"role": "system", "content": self.persona_text}]
        self.worker = None
        # 还没真正结束的线程都留在这里。QThread 一旦在「仍在运行」时被 Python
        # 回收，Qt 会 qFatal 直接 abort（表现为闪退、无报错），必须自己保活。
        self._live_workers = []
        self._last_paint = 0.0

        # DSH 模式：把话交给 DSH 的会话，会话 id 记在配置里（重启后接着聊）
        self._dsh_session_id = self.cfg.get("dsh_session_id") or ""
        self._dsh_last_error = ""
        # 开机预热：DSH 模式下桥没在监听（多半是 DSH 没开）→ 后台静默拉起，
        # 等用户真正开口时 DSH 基本已经就绪，不会再看到「连不上」
        if self.ai_mode() == MODE_DSH:
            threading.Thread(target=self._prewarm_dsh, daemon=True).start()

        # 长期记忆：每句话都落一份到 memory.db，提问时按相关度把旧片段捞回来
        self.memory = memory.MemoryStore()
        self.memory_session = time.strftime("%Y%m%d-%H%M%S")
        self._summary = self.memory.latest_summary(self.persona_name)
        self._recall = []            # 本轮召回结果（拼 system 用）
        self._pending_summary = []   # 等待压缩的、刚被挤出上下文的消息
        self._sum_worker = None
        print("[memory] %s | 本记忆库 %d 条 | 摘要 %d 字" % (
            self.memory.path if self.memory.ok else "不可用",
            self.memory.count(self.persona_name), len(self._summary)))
        self._drag_offset = None
        self._blink_timer = 0.0
        self._next_blink = random.uniform(2.5, 5.0)

        # 音乐：播放器 + 歌名气泡的显示计时
        self.music = MusicPlayer(MUSIC_DIR, parent=self)
        self.music.track_changed.connect(self._on_track_changed)
        self.music.error.connect(self._on_music_error)
        self._music_bubble_timer = 0.0

        # 盯住 music/ 目录：往里丢歌就自动重扫，不用手动点「重新扫描」
        self._music_watcher = QFileSystemWatcher(self)
        self._watch_music_dir()
        self._music_watcher.directoryChanged.connect(self._on_music_dir_changed)
        self._music_rescan_timer = QTimer(self)
        self._music_rescan_timer.setSingleShot(True)
        self._music_rescan_timer.setInterval(600)   # 防抖：复制文件会连发信号
        self._music_rescan_timer.timeout.connect(self._auto_rescan_music)
        self._chatting = False   # 聊天推理进行中，此时气泡归聊天所有
        self._interrupted = False  # 这一轮是被「打断」结束的（收尾时加提示）

        # 应用上次的偏好（音量 / 播放模式 / 窗口位置 / 待机动作）
        self.music.set_volume(self.cfg["volume"])
        self.music.shuffle = bool(self.cfg["shuffle"])
        self.music.loop_one = bool(self.cfg["loop_one"])
        saved_pose = self.cfg.get("anim_pose")
        if saved_pose and isinstance(self.renderer, PetAnimRenderer):
            self.renderer.set_idle_pose(saved_pose)

        # 动效时钟 30fps
        self.clock = QTimer(self)
        self.clock.timeout.connect(self._tick)
        self.clock.start(33)

        # 缩放落盘防抖：滚轮一格 5%，连滚时不每格都写磁盘
        self._scale_save_timer = QTimer(self)
        self._scale_save_timer.setSingleShot(True)
        self._scale_save_timer.setInterval(800)
        self._scale_save_timer.timeout.connect(
            lambda: config_store.save(self.cfg))

        # 先把窗口放到右下角
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.width() - self.width() - 40,
                  screen.height() - self.height() - 20)

        # 再恢复上次保存的位置（必须放在默认定位之后，否则会被覆盖）
        if self.cfg.get("x") is not None and self.cfg.get("y") is not None:
            self._restore_pos(self.cfg["x"], self.cfg["y"])

    # ---------- Live2D 角色层 ----------
    def _setup_live2d(self) -> bool:
        """挂上 Live2D GL 层。任何一步不成就返回 False，桌宠照常走序列帧。"""
        if not live2d_view.available():
            print("[live2d] 依赖不可用，回退：", live2d_view.import_error())
            return False
        mj = live2d_view.find_model_json()
        if not mj:
            print("[live2d] 没找到模型文件，回退")
            return False
        try:
            if not live2d_view.ensure_init():
                return False
            v = live2d_view.Live2DView(self, mj, size=self._scaled_size(live2d=True))
            v.failed.connect(self._on_live2d_failed)
            v.move(0, 0)
            v.show()
            self.gl_view = v
            print("[live2d] 角色层已启用：", mj)
            return True
        except Exception as exc:
            print("[live2d] 无法启用，回退：%r" % (exc,))
            # 构造中途失败时 Qt 侧可能已经把这个 child 挂上了，得摘干净
            for kid in self.findChildren(live2d_view.Live2DView):
                kid.setParent(None)
                kid.deleteLater()
            self.gl_view = None
            return False

    def _on_live2d_failed(self, msg: str):
        """模型加载失败：隐藏 GL 层、窗口恢复原尺寸、切回序列帧。

        信号是从 initializeGL 里发出来的，不能在绘制过程中改部件状态，
        所以用 singleShot 推到下一个事件循环再收拾。
        """
        print("[live2d] 角色层失败，回退：", msg)
        QTimer.singleShot(0, self._disable_live2d)

    def _disable_live2d(self):
        if self.gl_view is not None:
            self.gl_view.setParent(None)
            self.gl_view.deleteLater()
        self.gl_view = None
        self.live2d_mode = False
        self.setFixedSize(*self._scaled_size(live2d=False))
        self.update()

    # ---------- 大小缩放 ----------
    @staticmethod
    def _clamp_scale(s) -> float:
        try:
            s = float(s)
        except (TypeError, ValueError):
            return 1.0
        return round(max(SCALE_MIN, min(SCALE_MAX, s)), 3)

    def _base_size(self, live2d=None):
        """基准窗口尺寸（倍率 1.0 时的像素）；Live2D 与序列帧两套不同。"""
        if live2d is None:
            live2d = getattr(self, "live2d_mode", False)
        return (LIVE2D_W, LIVE2D_H) if live2d else (WINDOW_W, WINDOW_H)

    def _scaled_size(self, live2d=None, scale=None):
        bw, bh = self._base_size(live2d)
        s = self.pet_scale if scale is None else scale
        return max(140, round(bw * s)), max(140, round(bh * s))

    def set_pet_scale(self, scale, persist=True):
        """统一缩放入口：窗口 + Live2D 层同步，锚点取「底部中心」。

        锚底部中心是为了角色脚底不会跳：放大时往上长、缩小时往下收，
        位置不会被推跑。
        """
        new_scale = self._clamp_scale(scale)
        if abs(new_scale - self.pet_scale) < 1e-4:
            return
        old_w, old_h = self.width(), self.height()
        cx = self.x() + old_w // 2
        bottom = self.y() + old_h

        self.pet_scale = new_scale
        w, h = self._scaled_size()
        self.setFixedSize(w, h)
        if self.gl_view is not None:
            self.gl_view.set_view_size(w, h)
        self.move(cx - w // 2, bottom - h)
        self._keep_on_screen()

        if persist:
            self.cfg["pet_scale"] = round(new_scale, 2)
            self._scale_save_timer.start()   # 800ms 内没有后续调整才落盘
        self.update()

    def _keep_on_screen(self):
        scr = QApplication.primaryScreen().availableGeometry()
        w, h = self.width(), self.height()
        x = max(scr.left(), min(self.x(), scr.right() - w))
        y = max(scr.top(), min(self.y(), scr.bottom() - h))
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def zoom_in(self):
        self.set_pet_scale(self.pet_scale + SCALE_BUTTON_STEP)

    def zoom_out(self):
        self.set_pet_scale(self.pet_scale - SCALE_BUTTON_STEP)

    def zoom_reset(self):
        self.set_pet_scale(1.0)

    def _restore_pos(self, x: int, y: int):
        """恢复保存的位置；若因换显示器/分辨率掉到屏幕外，则回退到右下角。"""
        screen = QApplication.primaryScreen().availableGeometry()
        margin = 80  # 至少留这么大的可见区域
        in_x = screen.x() - self.width() + margin <= x <= screen.right() - margin
        in_y = screen.y() <= y <= screen.bottom() - margin
        if in_x and in_y:
            self.move(x, y)
        else:
            self.move(screen.width() - self.width() - 40,
                      screen.height() - self.height() - 20)

    # ---------- 动画 ----------
    def _tick(self):
        dt = 0.033
        self.renderer.t += dt
        self._blink_timer += dt
        if self._blink_timer > self._next_blink:
            self._blink_timer = 0.0
            self._next_blink = random.uniform(2.5, 6.0)
            self.renderer.blink = 1.0
        if self.renderer.blink > 0:
            self.renderer.blink = max(0.0, self.renderer.blink - dt * 7)

        # 歌名气泡倒计时：到点收起，并把角色恢复成待机
        if self._music_bubble_timer > 0:
            self._music_bubble_timer -= dt
            if self._music_bubble_timer <= 0 and not self._chatting:
                self.bubble.hide()
                if self.renderer.state == "talking":
                    self.renderer.state = "idle"

        self.update()
        if self.live2d_mode and self.gl_view is not None:
            self.gl_view.update()   # 角色由 GL 层自己驱动
        self._position_bubble()

    def _position_bubble(self):
        """把气泡摆到不与桌宠重叠的位置。

        切换尾巴方向会改变气泡尺寸（差一个 TAIL），所以要走两趟：
        第一趟定方向，第二趟用最终尺寸定坐标。
        """
        if not self.bubble.isVisible():
            return
        self._place_bubble()
        self._place_bubble()

    def _place_bubble(self):
        b = self.bubble
        scr = QApplication.primaryScreen().availableGeometry()
        bw, bh = b.width(), b.height()
        gap = 2                     # 尾巴长度 + 一点空隙，视觉上指向桌宠
        px, py = self.x(), self.y()
        pw, ph = self.width(), self.height()

        side = "bottom"
        bx = px + (pw - bw) // 2
        by = py - bh - gap

        if by < scr.top():
            # 上方空间不够 -> 左侧
            if px - bw - gap >= scr.left():
                side = "right"
                bx = px - bw - gap
                by = py + ph - bh
            # 右侧
            elif px + pw + gap + bw <= scr.right():
                side = "left"
                bx = px + pw + gap
                by = py + ph - bh
            else:
                # 都不行：贴顶部，允许压在桌宠上
                by = scr.top() + 2

        # 夹到屏幕内，避免文字被屏幕边缘切掉
        bx = max(scr.left() + 2, min(bx, scr.right() - bw - 2))
        by = max(scr.top() + 2, min(by, scr.bottom() - bh - 2))

        b.set_tail_side("right" if side == "left" else
                        ("left" if side == "right" else "bottom"))
        if (b.x(), b.y()) != (bx, by):
            b.move(bx, by)

    def paintEvent(self, _):
        if self.live2d_mode:
            return          # 角色由 Live2D GL 层绘制，这里保持完全透明
        p = QPainter(self)
        self.renderer.paint(p, self.width(), self.height())

    # ---------- 鼠标 ----------
    def mousePressEvent(self, e):
        # 点击桌宠会把它提到最前，这里把气泡重新压回顶层，避免遮住文字
        if self.bubble.isVisible():
            self.bubble.raise_()
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = e.globalPosition().toPoint() - self.pos()
        elif e.button() == Qt.MouseButton.RightButton:
            self._show_menu(e.globalPosition().toPoint())

    def mouseMoveEvent(self, e):
        if self._drag_offset is not None:
            self.move(e.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, e):
        if self._drag_offset is not None:
            self._drag_offset = None
            # 拖动结束就存一次，避免进程被强杀时丢失位置
            self.cfg["x"], self.cfg["y"] = self.x(), self.y()
            config_store.save(self.cfg)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.start_chat()

    def wheelEvent(self, e):
        """滚轮直接调大小：桌宠窗口没有滚动语义，滚一下放大/缩小最顺手。"""
        d = e.angleDelta().y()
        if not d:
            return
        self.set_pet_scale(self.pet_scale + (SCALE_WHEEL_STEP if d > 0
                                           else -SCALE_WHEEL_STEP))
        e.accept()

    def _show_menu(self, pos: QPoint):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#1e2530;color:#e6edf3;border:1px solid #33415c;"
            "padding:4px;border-radius:6px;}"
            "QMenu::item{padding:6px 18px;border-radius:4px;}"
            "QMenu::item:selected{background:#2f4a75;}"
            "QMenu::item:disabled{color:#6b7280;}"
            "QMenu::separator{height:1px;background:#33415c;margin:4px 8px;}"
        )

        menu.addAction("聊聊天", self.start_chat)

        # 正在跑任务时给一个醒目的打断入口（平时不显示，不占菜单）
        if self._chatting:
            a_stop = menu.addAction("停！打断当前任务")
            a_stop.triggered.connect(self.interrupt_chat)
            menu.addSeparator()

        # ---- 音乐 ----
        menu.addSeparator()
        has_music = bool(self.music.playlist)
        playing = self.music.is_playing

        act_toggle = menu.addAction("暂停音乐" if playing else "播放音乐")
        act_toggle.setEnabled(has_music)
        act_toggle.triggered.connect(self.music.play_pause)

        sub = menu.addMenu("下一首 / 上一首")
        sub.setEnabled(has_music)
        sub.addAction("下一首", self.music.next)
        sub.addAction("上一首", self.music.prev)

        act_shuffle = menu.addAction("随机播放")
        act_shuffle.setCheckable(True)
        act_shuffle.setChecked(self.music.shuffle)
        act_shuffle.triggered.connect(self._toggle_shuffle)

        act_loop = menu.addAction("单曲循环")
        act_loop.setCheckable(True)
        act_loop.setChecked(self.music.loop_one)
        act_loop.triggered.connect(self._toggle_loop)

        # 曲目列表
        list_menu = menu.addMenu(f"选择曲目（{len(self.music.playlist)} 首）")
        list_menu.setEnabled(has_music)
        if has_music:
            for i, name in enumerate(self.music.track_names()):
                mark = "▶ " if i == self.music.index else ""
                a = list_menu.addAction(f"{mark}{i + 1}. {name}")
                a.triggered.connect(lambda _=False, idx=i: self.music.play_index(idx))
        else:
            list_menu.addAction("（把音乐放进 music/ 目录）").setEnabled(False)

        act_open_dir = menu.addAction("打开 music 目录（往这里放歌）")
        act_open_dir.triggered.connect(self.open_music_dir)

        act_refresh = menu.addAction("重新扫描 music/")
        act_refresh.triggered.connect(self._refresh_music)

        # ---- 其他 ----
        menu.addSeparator()
        if self.live2d_mode and self.gl_view is not None and self.gl_view.ok:
            motions = self.gl_view.motions()
            if motions:
                sub_a = menu.addMenu("播放动作")
                for key, label in motions:
                    act = sub_a.addAction(label)
                    act.triggered.connect(
                        lambda _=False, k=key: self.gl_view.play_motion(k))
            exprs = self.gl_view.expressions()
            if exprs:
                sub_e = menu.addMenu("换表情")
                for key, label in exprs:
                    act = sub_e.addAction(label)
                    act.triggered.connect(
                        lambda _=False, k=key: self.gl_view.set_expression(k))
        elif isinstance(self.renderer, PetAnimRenderer):
            poses = self.renderer.poses()
            if poses:
                sub_a = menu.addMenu(
                    f"切换动作（当前：{self.renderer.current_pose_label()}）")
                for key, label in poses:
                    mark = "✓ " if key == self.renderer.idle_key else "    "
                    act = sub_a.addAction(mark + label)
                    act.triggered.connect(
                        lambda _=False, k=key: self._switch_pose(k))
        # ---- 大小 ----
        sub_s = menu.addMenu(f"大小（当前 {int(round(self.pet_scale * 100))}%）")
        sub_s.addAction("放大一点  ＋", self.zoom_in)
        sub_s.addAction("缩小一点  －", self.zoom_out)
        sub_s.addSeparator()
        cur_pct = int(round(self.pet_scale * 100))
        for pct in SCALE_PRESETS:
            act = sub_s.addAction(("✓ " if abs(cur_pct - pct) < 2 else "    ") + f"{pct}%")
            act.triggered.connect(
                lambda _=False, s=pct / 100.0: self.set_pet_scale(s))
        sub_s.addSeparator()
        sub_s.addAction(f"重置为 100%（{LIVE2D_W if self.live2d_mode else WINDOW_W}"
                        f"×{LIVE2D_H if self.live2d_mode else WINDOW_H}）",
                        self.zoom_reset)

        sub_p = menu.addMenu(f"切换人设（当前：{self.persona_name}）")
        for name in persona.list_presets():
            act = sub_p.addAction(("✓ " if name == self.persona_name else "    ") + name)
            act.triggered.connect(lambda _=False, n=name: self.switch_persona(n))
        sub_p.addSeparator()
        sub_p.addAction("新建人设…", self._new_persona)
        sub_p.addAction("编辑当前人设…", self._edit_persona)
        sub_p.addAction("重载当前人设", self.reload_persona)
        # ---- 回复长度 ----
        cur_style = self.cfg.get("ai_reply_style") or llm_client.DEFAULT_REPLY_STYLE
        cur_label = llm_client.style_of(cur_style)["label"]
        sub_len = menu.addMenu(f"回复长度（当前：{cur_label.split('（')[0]}）")
        for key, conf in llm_client.REPLY_STYLES.items():
            act = sub_len.addAction(("✓ " if key == cur_style else "    ") + conf["label"])
            act.triggered.connect(
                lambda _=False, k=key: self._set_reply_style(k))
        sub_len.addSeparator()
        sub_len.addAction("查看最近一次完整回复…", self.open_last_reply)

        # ---- 记忆 ----
        n_mem = self.memory.count(self.persona_name)
        sub_mem = menu.addMenu(f"记忆（{n_mem} 条）")
        act_mem = sub_mem.addAction("启用长期记忆")
        act_mem.setCheckable(True)
        act_mem.setChecked(bool(self.cfg.get("memory_enabled", True)))
        act_mem.triggered.connect(self._toggle_memory)

        sub_k = sub_mem.addMenu("每次回忆几条")
        cur_k = int(self.cfg.get("memory_recall") or 0)
        for kn in (0, 1, 3, 5):
            label = "不回忆" if kn == 0 else f"{kn} 条"
            act = sub_k.addAction(("✓ " if cur_k == kn else "    ") + label)
            act.triggered.connect(lambda _=False, v=kn: self._set_recall(v))

        sub_mem.addSeparator()
        sub_mem.addAction("立即整理记忆（压成摘要）", self.summarize_now)
        sub_mem.addAction("查看 / 搜索记忆…", self.open_memory)
        sub_mem.addSeparator()
        sub_mem.addAction("清空长期记忆…", self.clear_memory)

        menu.addSeparator()
        sub_mode = menu.addMenu(f"聊天模式（当前：{self._mode_label()}）")
        act_sa = sub_mode.addAction(
            ("✓ " if self.ai_mode() == MODE_STANDALONE else "    ") + "独立 AI（桌宠自己调模型）")
        act_sa.triggered.connect(lambda: self._set_mode(MODE_STANDALONE))
        act_dsh = sub_mode.addAction(
            ("✓ " if self.ai_mode() == MODE_DSH else "    ") + "DSH 会话（记忆/工具用 DSH 的）")
        act_dsh.triggered.connect(lambda: self._set_mode(MODE_DSH))
        act_dsh.setEnabled(dsh_client is not None)
        sub_mode.addSeparator()
        sub_mode.addAction("检查 DSH 连接…", self.check_dsh)
        sub_mode.addAction("DSH 里换个会话…", self.pick_dsh_session)

        menu.addAction(f"AI 设置（当前：{self._ai_label()}）…", self.open_ai_settings)
        menu.addSeparator()
        menu.addAction("清空当前对话（记忆保留）", self.clear_history)
        menu.addAction("打开运行日志（排查闪退用）", self.open_trace_log)
        menu.addAction("退出", self.quit_app)
        menu.exec(pos)

    # ---------- 音乐回调 ----------
    def _on_track_changed(self, name: str):
        self.bubble.set_text(name)
        self._music_bubble_timer = 3.0   # 显示 3 秒后自动收起
        if self.music.is_playing:
            self.renderer.state = "talking"   # 唱歌：嘴巴动 + 音符气泡

    def _on_music_error(self, msg: str):
        self.bubble.set_text(msg)
        self._music_bubble_timer = 3.0

    def _switch_pose(self, key: str):
        """切换待机动作（挥手 / 抱鲸鱼 / 坐下…），并记住选择。"""
        if not isinstance(self.renderer, PetAnimRenderer):
            return
        self.renderer.set_idle_pose(key)
        self.cfg["anim_pose"] = key
        config_store.save(self.cfg)

    def _toggle_shuffle(self, checked: bool):
        self.music.shuffle = checked

    def _toggle_loop(self, checked: bool):
        self.music.loop_one = checked

    def _refresh_music(self):
        n = self.music.refresh()
        if n:
            self.bubble.set_text(f"找到 {n} 首歌，要听哪首？")
        else:
            self.bubble.set_text("music/ 里还是空的，先放几首进去吧")
        self._music_bubble_timer = 3.0

    # ---------- music 目录 ----------
    def open_music_dir(self):
        """在资源管理器里打开 music 目录，直接往里拖歌就行。"""
        d = str(MUSIC_DIR)
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    def _watch_music_dir(self):
        """确保监听 music 目录（目录可能被删了或监听被系统掉了）。"""
        d = str(MUSIC_DIR)
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            return
        if d not in self._music_watcher.directories():
            self._music_watcher.addPath(d)

    def _on_music_dir_changed(self, _path: str):
        self._music_rescan_timer.start()

    def _auto_rescan_music(self):
        """目录变动后自动重扫，用气泡告诉用户结果。"""
        before = len(self.music.playlist)
        n = self.music.refresh()
        self._watch_music_dir()      # 有的系统在目录变动后会丢监听，补回来
        if n == before:
            return
        if n > before:
            self.bubble.set_text(f"music/ 里发现 {n} 首歌")
        else:
            self.bubble.set_text(f"music/ 里还剩 {n} 首")
        self._music_bubble_timer = 2.5

    # ---------- AI 设置 ----------
    def ai_params(self) -> dict:
        """把配置整理成 chat_stream 需要的参数。"""
        prov = self.cfg.get("ai_provider") or llm_client.PROVIDER_DEEPSEEK
        model = self.cfg.get("ai_model") or llm_client.DEEPSEEK_MODEL
        base = self.cfg.get("ai_base_url") or llm_client.DEEPSEEK_BASE_URL
        host = base if prov == llm_client.PROVIDER_OLLAMA else llm_client.DEFAULT_HOST

        # 命令行显式覆盖（调试用）
        if self._cli_model:
            model = self._cli_model
        if self._cli_host:
            host = base = self._cli_host

        style = llm_client.style_of(self.cfg.get("ai_reply_style"))
        return {
            "provider": prov,
            "model": model,
            "api_key": (self.cfg.get("ai_api_key") or "").strip(),
            "base_url": base,
            "host": host,
            "thinking": bool(self.cfg.get("ai_thinking", False)),
            "max_tokens": style["max_tokens"],
        }

    def _ai_label(self) -> str:
        p = self.ai_params()
        model = p["model"]
        if p["provider"] == llm_client.PROVIDER_OLLAMA:
            return f"本地 Ollama · {model}"
        if p["provider"] == llm_client.PROVIDER_CUSTOM:
            return f"自定义接口 · {model}"
        return f"DeepSeek · {model}"

    def open_ai_settings(self):
        """打开 AI 设置窗口（填 Key / 换模型 / 测连接）。"""
        dlg = ai_settings.AISettingsDialog(self.cfg, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.cfg.update(dlg.cfg)
            self.bubble.set_text(f"AI 现在是：{self._ai_label()}")
            self._music_bubble_timer = 5.0

    def _set_reply_style(self, key: str):
        """切换回复长度档位（会覆盖人设里的字数限制）。"""
        if key not in llm_client.REPLY_STYLES:
            return
        self.cfg["ai_reply_style"] = key
        config_store.save(self.cfg)
        label = llm_client.REPLY_STYLES[key]["label"]
        self.bubble.set_text(f"回复长度：{label}")
        self._music_bubble_timer = 4.0

    # ---------- 运行模式（独立 AI / DSH 会话）----------
    def _mode_label(self) -> str:
        return "DSH 会话" if self.ai_mode() == MODE_DSH else "独立 AI"

    def _set_mode(self, mode: str):
        if mode == MODE_DSH and dsh_client is None:
            self.bubble.set_text("这个版本没带 DSH 客户端，只能用独立 AI")
            self._music_bubble_timer = 6.0
            return
        self.cfg["ai_mode"] = mode
        config_store.save(self.cfg)
        if mode == MODE_DSH:
            ok, info = dsh_client.DshClient().health()
            if ok:
                self.bubble.set_text("改用 DSH 会话了：记忆和工具都交给 DSH")
            else:
                self.bubble.set_text(f"切到 DSH 了，但没连上：{info.get('error') or '桥插件没启动'}")
        else:
            self.bubble.set_text("改回独立 AI 了")
        self._music_bubble_timer = 7.0

    def check_dsh(self):
        if dsh_client is None:
            QMessageBox.warning(self, "DSH", "当前版本没带 DSH 客户端模块。")
            return
        host, port, why = dsh_client.bridge_info()
        ok, info = dsh_client.DshClient(host, port).health()
        if ok:
            QMessageBox.information(
                self, "DSH 连接正常",
                "桥插件在线\n"
                f"地址：{host}:{info.get('port')}\n"
                f"DSH 当前会话：{info.get('lastSessionId') or '（还没选中）'}\n"
                f"桌宠用的会话：{self._dsh_session_id or '（下次说话时新建）'}")
        else:
            QMessageBox.warning(
                self, "DSH 没连上",
                f"{info.get('error') or why}\n\n"
                "排查顺序：\n"
                "1. DSH 桌面版开着吗\n"
                "2. pet-bridge 插件装了吗（装完必须重启 DSH 才加载）\n"
                f"3. 状态文件在不在：{dsh_client.STATUS_FILE}")

    @staticmethod
    def _fmt_session(item: dict) -> str:
        """会话在菜单里的显示名：标题（没有就用目录名）+ 更新时间 + id 尾巴。"""
        when = ""
        ts = item.get("updatedAt")
        if ts:
            try:
                when = time.strftime("%m-%d %H:%M", time.localtime(float(ts) / 1000.0))
            except (ValueError, OSError, OverflowError):
                when = ""
        cwd = (item.get("cwd") or "").rstrip("/\\")
        name = item.get("title") or (os.path.basename(cwd) if cwd else "") or "会话"
        tail = str(item.get("id") or "")[-6:]
        parts = [name]
        if when:
            parts.append(when)
        if tail:
            parts.append(tail)
        if item.get("running"):
            parts.append("进行中")
        return "  ·  ".join(parts)

    def pick_dsh_session(self):
        if dsh_client is None:
            return
        client = dsh_client.DshClient()
        items = client.sessions()
        if not items:
            self.bubble.set_text("DSH 里还没有会话，先在那里开一个")
            self._music_bubble_timer = 5.0
            return
        labels = [self._fmt_session(i) for i in items]
        labels.insert(0, "＋ 新建一个会话")
        label, ok = QInputDialog.getItem(self, "选择 DSH 会话", "接着哪个会话聊：",
                                         labels, 0, False)
        if not ok:
            return
        if labels.index(label) == 0:
            sid = client.new_session()
            if not sid:
                self.bubble.set_text("新建会话失败，看看 DSH 是否正常")
                self._music_bubble_timer = 5.0
                return
            self._dsh_session_id = sid
            self.cfg["dsh_session_id"] = sid
            config_store.save(self.cfg)
            self.bubble.set_text("开了一个新会话")
            self._music_bubble_timer = 4.0
            return
        item = items[labels.index(label) - 1]
        sid = item.get("id")
        self._dsh_session_id = sid
        self.cfg["dsh_session_id"] = sid
        config_store.save(self.cfg)
        self.bubble.set_text(f"接上「{item.get('title') or sid}」了")
        self._music_bubble_timer = 6.0

    # ---------- 线程生命周期 ----------
    def _retire_worker(self):
        """把当前 worker 摘下来：断开信号、催它收尾，但**保留引用**到线程真结束。

        绝对不能直接覆盖或置空 self.worker：QThread 在 isRunning() 时被 Python
        回收，Qt 会 qFatal 直接 abort（0xc0000409 闪退、没有任何 Python 报错）。
        所以这里只断信号 + 尽力 stop，对象交给 _live_workers 保活，
        等 QThread.finished 才 deleteLater。
        """
        w = self.worker
        self.worker = None
        if w is None:
            return
        try:
            running = w.isRunning()
        except RuntimeError:     # 底层 C++ 对象已经被 deleteLater 收走了
            trace.log("retire worker 已失效（线程早已结束）")
            return
        trace.log(f"retire worker {type(w).__name__} running={running}")
        for name in ("chunk", "note", "failed", "finished_ok", "session_ready"):
            sig = getattr(w, name, None)
            if sig is None:
                continue
            try:
                sig.disconnect()
            except (TypeError, RuntimeError):
                pass
        try:
            if w.isRunning() and hasattr(w, "stop"):
                w.stop()      # 催一下；不 wait，免得界面卡住
        except RuntimeError:  # 底层 C++ 对象已经没了
            pass

    def _spawn_worker(self, worker, slots):
        """开一轮新线程：先退役旧的，再把新线程登记进保活列表。

        slots: [(信号名, 槽函数)]，在 start 之前连接，避免漏掉开头几个信号。
        """
        self._retire_worker()
        self._live_workers.append(worker)
        worker.finished.connect(lambda w=worker: self._forget_worker(w))
        for sig_name, handler in slots:
            getattr(worker, sig_name).connect(handler)
        self.worker = worker
        trace.log(f"worker start {type(worker).__name__} "
                  f"live={len(self._live_workers)}")
        worker.start()

    def _forget_worker(self, worker):
        """线程真正结束后才放手（此时析构 QThread 是安全的）。"""
        trace.log(f"enter _forget_worker {type(worker).__name__}")
        if self.worker is worker:
            self.worker = None       # 别留一个已删对象给下一轮
        try:
            self._live_workers.remove(worker)
        except ValueError:
            pass
        trace.log(f"worker finished {type(worker).__name__} "
                  f"live={len(self._live_workers)}")
        worker.deleteLater()
        trace.log("exit _forget_worker")

    def _from_current(self) -> bool:
        """这个回调是不是当前这轮 worker 发来的？

        被掐掉的旧线程可能还有信号在队列里，迟到送达时不能去改新气泡，
        否则上一轮的半截回复会把这一轮盖掉。
        """
        try:
            s = self.sender()
        except RuntimeError:
            return False
        return s is None or s is self.worker

    # ---------- 聊天 ----------
    def start_chat(self):
        trace.log("start_chat 打开输入框")
        if self.worker and self.worker.isRunning():
            # 正在回复时再触发，就掐掉重新问
            self.set_state("idle")
        # DSH 模式不需要桌宠自己的 Key（Key 在 DSH 里配）
        if self.ai_mode() != MODE_DSH and not ai_settings.ensure_ready(self, self.cfg):
            self.bubble.set_text("还没填 API Key，右键 →「AI 设置」填一下就能聊啦")
            self._music_bubble_timer = 7.0
            return
        text, ok = QInputDialog.getText(
            self, f"和{self.persona_name}说话", "", QLineEdit.EchoMode.Normal, ""
        )
        if not ok or not text.strip():
            return
        self.send(text.strip())

    def _messages_for_api(self) -> list:
        """拼一次请求用的消息。

        system 里依次叠四层：
        1. 人设（personas/*.md）
        2. 长期摘要（以前聊过的事被压成的一段话）
        3. 本轮召回（这条消息可能相关的旧片段）
        4. 回复长度指令（覆盖人设里写死的「50 字以内」）

        人设文件里的字数限制是硬约束，光调 max_tokens 没用，必须在末尾覆盖。
        只改副本，不动 self.history，切人设/重载时不会串味。
        """
        msgs = [dict(m) for m in self.history]
        if not msgs or msgs[0].get("role") != "system":
            return msgs

        parts = [msgs[0]["content"]]
        if self._summary:
            parts.append("【长期记忆（以前聊过的事）】\n" + self._summary)
        recall_block = memory.format_recall(self._recall)
        if recall_block:
            parts.append(recall_block)
        hint = llm_client.style_of(self.cfg.get("ai_reply_style"))["hint"]
        if hint:
            parts.append(hint)

        msgs[0] = {"role": "system", "content": "\n\n".join(parts)}
        return msgs

    def ai_mode(self) -> str:
        """当前运行模式。没装 dsh_client 或配置是 standalone 时一律走独立 AI。"""
        if dsh_client is None:
            return MODE_STANDALONE
        return MODE_DSH if self.cfg.get("ai_mode") == MODE_DSH else MODE_STANDALONE

    def _prewarm_dsh(self):
        """后台线程：DSH 模式下桥没在监听时，静默把 DSH 桌面版拉起来。

        开机预热用，不弹气泡——等用户真正开口时 DSH 基本已就绪；
        就绪失败也不打扰，等真发消息时 worker 会再试并给出完整提示。
        """
        try:
            ok, _ = dsh_client.DshClient().health()
            if ok:
                return
            trace.log("prewarm: bridge down, launching DSH")
            ok, info = dsh_client.ensure_dsh_running(
                config_store.load().get("dsh_exe_path", ""), timeout=40.0)
            trace.log("prewarm: %s" % ("ok" if ok else (info or {}).get("error", "?")))
        except Exception as exc:  # 后台线程，绝不能拖垮启动流程
            trace.log("prewarm error: %r" % exc)

    def _send_via_dsh(self, text: str):
        """DSH 模式：不建自己的上下文，直接把话送进 DSH 的会话。"""
        self.bubble.set_text("...")
        self.renderer.state = "thinking"
        self._pending_reply = ""
        self._chatting = True
        self._interrupted = False
        self._music_bubble_timer = 0.0

        self._spawn_worker(DshChatWorker(
            text, self._dsh_session_id,
            dsh_exe=config_store.load().get("dsh_exe_path", "")), [
            ("chunk", self._on_chunk),
            ("note", self._on_dsh_note),
            ("failed", self._on_failed),
            ("finished_ok", self._on_done),
            ("session_ready", self._on_dsh_session),
        ])

    def interrupt_chat(self):
        """打断当前回合。

        DSH 模式下这是真取消：通知 DSH 中止正在跑的任务（不只是桌宠这边
        不听了）；独立 AI 模式就是掐掉流。任务进度都在 DSH 会话的历史里，
        所以打断后想接着做，说一句「继续」就行。
        """
        w = self.worker
        trace.log(f"interrupt_chat worker={type(w).__name__ if w else '无'} "
                  f"chatting={self._chatting}")
        if w is None or not self._chatting:
            self.bubble.set_text("现在没有正在做的事哦")
            self._music_bubble_timer = 3.0
            return
        self._interrupted = True
        self.renderer.state = "idle"
        try:
            w.stop()          # DSH worker 会顺带调桥的 /cancel
        except RuntimeError:
            pass
        # 后续收尾交给 _on_done / _on_failed（流被取消后会正常结束）


    def _on_dsh_session(self, sid: str):
        """DSH 新建/复用了会话，记下来，下次接着聊同一个。"""
        self._dsh_session_id = sid
        self._dsh_last_error = ""
        if self.cfg.get("dsh_session_id") != sid:
            self.cfg["dsh_session_id"] = sid
            try:
                config_store.save(self.cfg)
            except Exception:
                pass

    def _on_dsh_note(self, text: str):
        """工具调用的过程提示。正文还没开始流时占用气泡，开始了就不打扰。"""
        if not self._from_current():
            return
        if not self._pending_reply:
            self.bubble.set_text(f"（{text}）")
            self._music_bubble_timer = 3.0

    def send(self, text: str):
        trace.log(f"send [{self.ai_mode()}] {len(text)} 字：{text[:24]!r}")
        self._interrupted = False
        if self.ai_mode() == MODE_DSH:
            self._send_via_dsh(text)
            return

        self.history.append({"role": "user", "content": text})

        # 先落库，再召回（排除刚说的这条，不然会自己回忆自己）
        self._recall = []
        if self._memory_on():
            mid = self.memory.add("user", text, persona=self.persona_name,
                                  session=self.memory_session)
            k = int(self.cfg.get("memory_recall") or 0)
            if k > 0:
                try:
                    self._recall = self.memory.search(
                        text, k=k, persona=self.persona_name,
                        exclude_ids=[mid] if mid else [])
                except Exception as exc:      # 召回失败不该挡住聊天
                    print("[memory] 召回失败：%r" % (exc,))
                    self._recall = []

        self._trim_history()

        self.bubble.set_text("...")
        self.renderer.state = "thinking"
        self._pending_reply = ""
        self._chatting = True
        self._music_bubble_timer = 0.0

        # 快照一份，避免线程运行中 history 被改
        messages = self._messages_for_api()

        self._spawn_worker(ChatWorker(messages, self.ai_params()), [
            ("chunk", self._on_chunk),
            ("failed", self._on_failed),
            ("finished_ok", self._on_done),
        ])

    def _on_chunk(self, piece: str):
        if not self._from_current():
            return
        if self.renderer.state != "talking":
            self.renderer.state = "talking"
            self._pending_reply = ""
            trace.log(f"enter _on_chunk 首帧 ({len(piece)} 字) mode={self.ai_mode()}")
        self._pending_reply += piece
        # 流式阶段也清洗，避免反引号/星号一闪而过。
        # 但不要每个 token 都改气泡：一次长回答可能有几百个 chunk，每个都
        # setFixedSize + move + raise_ 会在 Windows 上引发窗口几何消息风暴。
        # 攒够 ~33ms 再刷一次（30fps 足够肉眼流畅）。
        now = time.monotonic()
        if now - self._last_paint >= PAINT_INTERVAL:
            self._last_paint = now
            self.bubble.set_text(text_clean.clean(self._pending_reply))

    def _flush_reply_paint(self):
        """把攒下的增量补刷一次（收尾/兜底用）。"""
        if self._pending_reply:
            self.bubble.set_text(text_clean.clean(self._pending_reply))

    def _on_done(self, full: str):
        if not self._from_current():
            trace.log("drop stale _on_done（上一轮迟到的回复）")
            return            # 上一轮被掐掉后迟到的回复，别覆盖这一轮
        trace.log(f"enter _on_done {len(full or '')} 字")
        self._chatting = False
        self.renderer.state = "idle"
        clean_text = text_clean.clean(full)
        if self._interrupted:
            self._interrupted = False
            # 被打断的收尾：有半截话就带着，没有就直接说停了；
            # 任务进度在 DSH 会话历史里，说「继续」就能接着做
            clean_text = (clean_text + "\n（已打断，想接着做就说「继续」）"
                          if clean_text else "（已停下。想接着做就说「继续」）")
        if clean_text:
            # 长回复分页轮播，短回复单页显示
            pages = self.bubble.paginate(clean_text)
            if len(pages) > 1:
                self.bubble.set_pages(pages)
                # 气泡停留时间 = 各页阅读时长之和 + 5 秒余量，别没翻完就收起来
                read_time = sum(Bubble._page_delay(p) for p in pages) / 1000.0
                self._music_bubble_timer = read_time + 5.0
            else:
                self.bubble.set_text(clean_text)
                self._music_bubble_timer = 8.0
            self.history.append({"role": "assistant", "content": clean_text})
            # DSH 模式下记忆由 DSH 的记忆插件负责，桌宠这边不重复存一份
            if self._memory_on() and self.ai_mode() != MODE_DSH:
                self.memory.add("assistant", clean_text,
                                persona=self.persona_name,
                                session=self.memory_session)
            self._trim_history()
            self._save_reply(clean_text)
        else:
            # 空回复（模型只给了标点/洗涤后为空）：别让气泡一直挂着
            self.bubble.set_text("(什么都没说…)")
            self._music_bubble_timer = 4.0
        trace.log("exit _on_done")

    # ---------- 完整回复留档 ----------
    def _save_reply(self, text: str) -> None:
        """把最近一次回复写到 exe 旁，气泡翻页看不全时可以打开翻。"""
        path = os.path.join(paths.base_dir(), "最近一次回复.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass

    def open_last_reply(self):
        path = os.path.join(paths.base_dir(), "最近一次回复.txt")
        if not os.path.isfile(path):
            self.bubble.set_text("还没有回复记录，先聊两句吧")
            self._music_bubble_timer = 4.0
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def open_trace_log(self):
        """打开运行日志：闪退后看最后几行就知道它当时在做什么。"""
        path = os.path.join(paths.base_dir(), "_trace.log")
        if not os.path.isfile(path):
            self.bubble.set_text("还没有日志文件")
            self._music_bubble_timer = 4.0
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _on_failed(self, msg: str):
        if not self._from_current():
            return
        trace.log(f"enter _on_failed: {msg}")
        if self._interrupted:
            # 被打断时 DSH 常会报一个「取消」类错误，这不是故障
            self._interrupted = False
            msg = "已停下。想接着做就说「继续」"
        self._chatting = False
        self.renderer.state = "idle"
        self.bubble.set_text(f"({msg})")
        self._music_bubble_timer = 6.0
        trace.log("exit _on_failed")

    def _trim_history(self):
        """短期上下文裁剪：条数 + 字数双上限，超出的部分转去压摘要。

        以前是硬砍（砍掉就永远忘了），现在砍之前先把这批旧消息排进
        摘要队列，由后台线程压成一段话留在长期记忆里。
        """
        sys_msgs = [m for m in self.history if m["role"] == "system"]
        rest = [m for m in self.history if m["role"] != "system"]

        kept, chars = [], 0
        for m in reversed(rest):
            if kept and (len(kept) >= MAX_HISTORY
                         or chars + len(m["content"]) > HISTORY_CHAR_BUDGET):
                break
            kept.append(m)
            chars += len(m["content"])
        kept.reverse()

        dropped = rest[:len(rest) - len(kept)]
        self.history = sys_msgs + kept
        if dropped:
            self._queue_summary(dropped)

    # ---------- 长期记忆 ----------
    def _memory_on(self) -> bool:
        return bool(self.cfg.get("memory_enabled", True)) and self.memory.ok

    def _queue_summary(self, dropped, force: bool = False):
        """把被挤出上下文的消息攒起来，交给后台线程压缩。

        上下文满了之后每来一句就掉一条旧的，如果每条都调一次模型太浪费，
        所以攒够 SUMMARY_BATCH 条（或字数够了）才真的压一次。
        """
        if not dropped or not self._memory_on():
            return
        if not self.cfg.get("memory_summary", True):
            return
        self._pending_summary.extend(dropped)
        if self._sum_worker is not None and self._sum_worker.isRunning():
            return          # 上一个还在跑，跑完会自动接着处理
        if not force:
            chars = sum(len(m["content"]) for m in self._pending_summary)
            if (len(self._pending_summary) < SUMMARY_BATCH
                    and chars < SUMMARY_MIN_CHARS):
                return
        self._start_summary()

    def _start_summary(self):
        lines = [f"{'用户' if m['role'] == 'user' else self.persona_name}："
                 f"{m['content']}" for m in self._pending_summary]
        if not lines:
            return
        ai = self.ai_params()
        if ai["provider"] != llm_client.PROVIDER_OLLAMA and not ai["api_key"]:
            self._pending_summary = []
            return          # 没配 Key，别白等
        self._pending_summary = []

        self._sum_worker = SummaryWorker(self._summary, lines, ai)
        self._sum_worker.ok.connect(self._on_summary_ok)
        self._sum_worker.bad.connect(self._on_summary_bad)
        self._sum_worker.finished.connect(self._on_summary_finished)
        self._sum_worker.start()

    def _on_summary_ok(self, text: str):
        text = (text or "").strip()
        if len(text) < 8:
            return
        self._summary = text
        self.memory.add_summary(text, persona=self.persona_name)
        print("[memory] 摘要已更新（%d 字）：%s" % (len(text), text[:60]))

    def _on_summary_bad(self, msg: str):
        print("[memory] 摘要失败：%s" % msg)

    def _on_summary_finished(self):
        self._sum_worker = None
        if self._pending_summary:
            QTimer.singleShot(0, self._start_summary)

    def summarize_now(self):
        """手动整理：把当前上下文里较早的部分也压进摘要（聊天记录不删）。"""
        if not self._memory_on():
            self.bubble.set_text("长期记忆现在是关着的，先打开再来整理吧")
            self._music_bubble_timer = 5.0
            return
        rest = [m for m in self.history if m["role"] != "system"]
        if len(rest) < 2:
            self.bubble.set_text("还没聊几句呢，等会儿再整理吧")
            self._music_bubble_timer = 4.0
            return
        self._queue_summary(rest[:-2], force=True)
        self.bubble.set_text("我把刚才聊的整理一下…")
        self._music_bubble_timer = 4.0

    def open_memory(self):
        dlg = memory_dialog.MemoryDialog(self.memory, self.persona_name, self)
        dlg.exec()
        if dlg.cleared:
            self._summary = ""
            self.bubble.set_text("记忆清空啦，重新认识一下？")
            self._music_bubble_timer = 5.0

    def clear_memory(self):
        n = self.memory.count(self.persona_name)
        if n == 0:
            self.bubble.set_text("本来就没记住什么")
            self._music_bubble_timer = 3.5
            return
        if QMessageBox.question(
                self, "清空长期记忆",
                f"要忘掉「{self.persona_name}」记住的 {n} 条内容吗？删了找不回来。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self.memory.clear(self.persona_name)
        self.memory.vacuum()
        self._summary = ""
        self._pending_summary = []
        self.bubble.set_text("嗯…我把那些都忘了")
        self._music_bubble_timer = 4.0

    def _toggle_memory(self, on: bool):
        self.cfg["memory_enabled"] = bool(on)
        config_store.save(self.cfg)
        self.bubble.set_text("长期记忆已打开，我会慢慢记住你" if on
                             else "长期记忆关掉了，聊完就忘")
        self._music_bubble_timer = 4.0

    def _set_recall(self, k: int):
        self.cfg["memory_recall"] = int(k)
        config_store.save(self.cfg)
        self.bubble.set_text("不主动回忆旧事了" if k == 0
                             else f"每次聊天会回忆 {k} 条旧片段")
        self._music_bubble_timer = 3.5

    def _edit_persona(self):
        """用系统默认编辑器打开当前人设文件。"""
        ok = persona.open_in_editor(self.persona_name)
        self.bubble.set_text(
            f"已打开「{self.persona_name}」的人设文件，改完点「重载当前人设」"
            if ok else "打不开人设文件…手动去 personas 文件夹改吧"
        )
        self._music_bubble_timer = 6.0

    def _new_persona(self):
        """新建一份人设，基于默认模板，并立即切换过去。"""
        name, ok = QInputDialog.getText(
            self, "新建人设", "给人设起个名字（会生成 personas/名字.md）",
            QLineEdit.EchoMode.Normal, "",
        )
        if not ok:
            return
        name = persona.sanitize_name(name)
        if not name:
            self.bubble.set_text("名字不能为空哦")
            self._music_bubble_timer = 4.0
            return
        if not persona.create_preset(name):
            self.bubble.set_text(f"「{name}」已经存在，先换个名字吧")
            self._music_bubble_timer = 4.0
            return
        self.switch_persona(name)
        persona.open_in_editor(name)

    def switch_persona(self, name: str):
        """切到另一套人设：换 system 提示 + 记住选择。"""
        if not persona.set_active(name):
            return
        self.persona_name = name
        self.persona_text = persona.load(name)
        rest = [m for m in self.history if m["role"] != "system"]
        self.history = [{"role": "system", "content": self.persona_text}] + rest
        # 换人设 = 换一套记忆（避免把「小蓝」记得的事安在「大肥鱼」头上）
        self._summary = self.memory.latest_summary(name)
        self._recall = []
        self._pending_summary = []
        self.setWindowTitle(name)
        self.bubble.set_text(f"人设换成「{name}」了，重新认识一下？")
        self._music_bubble_timer = 5.0

    def reload_persona(self):
        """重新读取当前人设文件。"""
        new_text = persona.load(self.persona_name)
        changed = new_text.strip() != self.persona_text.strip()
        self.persona_text = new_text
        # 保留对话历史，只换 system 提示（人设变了但记忆还在）
        rest = [m for m in self.history if m["role"] != "system"]
        self.history = [{"role": "system", "content": new_text}] + rest
        self.bubble.set_text(
            "人设更新好啦，我好像不一样了～" if changed else "人设没变化哦"
        )
        self._music_bubble_timer = 5.0

    def clear_history(self):
        self.history = [{"role": "system", "content": self.persona_text}]
        self._recall = []
        self._pending_summary = []
        self.bubble.set_text("诶，我怎么什么都想不起来了…（长期记忆还在）")

    def quit_app(self):
        trace.log("quit_app 开始")
        # 先把所有还在跑的线程催停并等住——退出时析构运行中的 QThread 一样会 abort
        for w in list(self._live_workers):
            try:
                if w.isRunning() and hasattr(w, "stop"):
                    w.stop()
            except RuntimeError:
                pass
        for w in list(self._live_workers):
            try:
                w.wait(1500)
            except RuntimeError:
                pass
        if self.worker is not None and self.worker.isRunning():
            self.worker.wait(1500)
        if self._sum_worker is not None and self._sum_worker.isRunning():
            self._sum_worker.wait(1200)   # 摘要就快跑完了，给它一点时间落盘
        self.music.stop()
        # 保存偏好，下次启动自动恢复
        self.cfg.update({
            "x": self.x(),
            "y": self.y(),
            "volume": self.music.audio.volume(),
            "shuffle": self.music.shuffle,
            "loop_one": self.music.loop_one,
        })
        config_store.save(self.cfg)
        self.bubble.close()
        self.memory.close()
        live2d_view.dispose()   # 释放 Live2D 的 GL / 核心资源
        QApplication.quit()
