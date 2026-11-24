"""微信风格的聊天窗口。

功能对应需求：
1. 右边绿色气泡 = 用户，左边白色气泡 = AI（带头像、带小尾巴）
2. 聊天记录落盘（chat_history.json），重新打开还能翻
3. 表情面板：emoji 直接入输入框；stickers/ 目录下的图片点一下直接发
4. 窗口可拖动、可调大小（右下角拉伸 + 标题栏拖动）
5. 左右屏幕边缘吸附（QQ 老版那种）：贴边后鼠标离开缩成一条小边条，
   悬停边条再弹开；上下边界不吸附

和桌宠的分工：本窗口只管「显示 + 记录」，真正的回复逻辑全在 PetWindow。
PetWindow 通过 append_user / stream_ai / finish_ai / append_note 等方法
把聊天事件同步过来；用户在这里发消息通过 message_sent 信号交回去。
"""

import json
import os
import time

from PyQt6.QtCore import QPointF, QPoint, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QColor, QCursor, QFont, QFontMetrics, QPainter,
                         QPainterPath, QPen, QPixmap, QPolygonF)
from PyQt6.QtWidgets import (QApplication, QGridLayout, QHBoxLayout, QLabel,
                             QPlainTextEdit, QPushButton, QScrollArea,
                             QSizeGrip, QVBoxLayout, QWidget)

import paths

try:
    import text_clean
except Exception:                                    # pragma: no cover
    class _TC:
        @staticmethod
        def clean(t):
            return t
    text_clean = _TC()

# ---- 外观常量（照着微信配色来） ----
BG = QColor("#F5F5F5")          # 聊天区背景
USER_GREEN = QColor("#95EC69")  # 用户气泡（微信绿）
AI_WHITE = QColor("#FFFFFF")    # AI 气泡
BUBBLE_TEXT = QColor("#111111")
NOTE_GRAY = QColor("#9A9A9A")
TIME_GRAY = QColor("#B2B2B2")
TITLE_BG = QColor("#EDEDED")

AVATAR = 36                     # 头像边长
BUBBLE_PAD = 10                 # 气泡内边距
TAIL = 6                        # 气泡小尾巴长度
ROW_SPACING = 6
TIME_GAP = 300                  # 间隔超过 5 分钟补一条时间分隔

SNAP_PX = 28                    # 离左右边这么近就吸住
COLLAPSED_W = 8                 # 吸附收起后剩一条多窄的边条
POLL_MS = 200                   # 悬停检测轮询

MIN_W, MIN_H = 360, 480
DEFAULT_W, DEFAULT_H = 470, 640
VIEW_RATIO = 0.62               # 气泡最宽占可视区的比例

HISTORY_FILE = "chat_history.json"
HISTORY_KEEP = 500              # 落盘上限（条）
SHOW_ON_LOAD = 120              # 打开窗口回显最近多少条

EMOJIS = (
    "😀 😃 😄 😁 😆 😅 🤣 😂 🙂 😉 😊 😍 🥰 😘 😜 🤪 "
    "🤔 🤨 😐 😑 😶 🙄 😏 😣 😥 😮 😯 😴 😌 😔 🙁 😖 "
    "😤 😠 😡 🤬 😯 😳 🥵 🥶 😱 😨 😰 😥 😢 😭 😱 😖 "
    "👍 👏 🙏 💪 ✌️ 🤝 🎉 ✨ 🌹 💖 ❤️ 🎁 🔥 ⭐ 🌙 ☀️ "
    "🐶 🐱 🐟 🍚 🍜 🍰 🍵 ☕ ⚽ 🎮 🎵 🎧 📱 💻 🚗 ✈️"
).split()

STICKER_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


# ============================================================
# 头像
# ============================================================

def _round_pixmap(pix: QPixmap, size: int) -> QPixmap:
    """方图缩放到 size 后裁成圆角方形（微信头像样式）。"""
    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, size, size, size * 0.22, size * 0.22)
    p.setClipPath(path)
    scaled = pix.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation)
    p.drawPixmap((size - scaled.width()) // 2, (size - scaled.height()) // 2,
                 scaled)
    p.end()
    return out


def _text_avatar(bg_hex: str, ch: str) -> QPixmap:
    """兜底头像：圆角方块 + 一个字/表情。"""
    pix = QPixmap(AVATAR, AVATAR)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, AVATAR, AVATAR, 8, 8)
    p.fillPath(path, QColor(bg_hex))
    p.setPen(QPen(QColor("#FFFFFF")))
    f = QFont()
    f.setPixelSize(int(AVATAR * 0.55))
    p.setFont(f)
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, ch)
    p.end()
    return pix


def ai_avatar() -> QPixmap:
    """AI 头像：assets/pet.png，没有就画个蓝方块鱼。"""
    path = os.path.join(paths.resource_dir(), "assets", "pet.png")
    if os.path.isfile(path):
        try:
            pix = QPixmap(path)
            if not pix.isNull():
                return _round_pixmap(pix, AVATAR)
        except Exception:
            pass
    return _text_avatar("#5B8FF9", "🐟")


def user_avatar() -> QPixmap:
    """用户头像：assets/avatar_user.png 可自定义，没有就画默认。"""
    for d in (paths.base_dir(), paths.resource_dir()):
        path = os.path.join(d, "assets", "avatar_user.png")
        if os.path.isfile(path):
            try:
                pix = QPixmap(path)
                if not pix.isNull():
                    return _round_pixmap(pix, AVATAR)
            except Exception:
                pass
    return _text_avatar("#6C6CC4", "我")


# ============================================================
# 气泡：圆角矩形 + 指向头像的小尾巴
# ============================================================

class Bubble(QWidget):
    """自绘气泡（文本 / 图片），tail=False 时不画尾巴。"""

    def __init__(self, text="", green=False, image: QPixmap = None,
                 parent=None):
        super().__init__(parent)
        self.green = green
        self.tail = True
        lay = QHBoxLayout(self)
        lay.setContentsMargins(BUBBLE_PAD + (TAIL if green else TAIL),
                               BUBBLE_PAD,
                               BUBBLE_PAD + (TAIL if not green else TAIL),
                               BUBBLE_PAD)
        self.label = QLabel(self)
        if image is None:
            self.label.setWordWrap(True)
            self.label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            self.label.setText(text)
            self.label.setFont(self._msg_font())
        else:
            self.label.setPixmap(image)
        lay.addWidget(self.label)
        self.set_text_color()

    @staticmethod
    def _msg_font() -> QFont:
        f = QFont()
        f.setPointSize(10)
        return f

    def set_text_color(self):
        # 注意：PyQt6 新版 pixmap() 未设置时也返回空 pixmap（不是 None），
        # 必须用 isNull() 判断。判断写错会让深色文字样式永远不生效，
        # 系统深色模式下文字变成白色 → 白气泡上白字隐形。
        pix = self.label.pixmap()
        if pix is None or pix.isNull():
            self.label.setStyleSheet(
                "color:%s; background:transparent;" % BUBBLE_TEXT.name())

    # ---- 尺寸自适应 ----
    def fit_text(self, text: str, max_w: int):
        """按内容算宽度：短行保持自然宽，长行折行到 max_w。"""
        self.label.setText(text)
        fm = QFontMetrics(self.label.font())
        natural = 0
        for line in text.split("\n"):
            natural = max(natural, fm.horizontalAdvance(line))
        natural += self.label.indent() + 6
        w = max(1, min(natural, max_w))
        self.label.setFixedWidth(w)
        h = self.label.heightForWidth(w)
        if h and h > 0:
            self.label.setFixedHeight(h)
        self.label.adjustSize()
        self.adjustSize()
        self.setFixedHeight(self.sizeHint().height())

    # ---- 绘制 ----
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect().adjusted(1, 1, -1, -1)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(USER_GREEN if self.green else AI_WHITE)
        p.drawRoundedRect(r, 6, 6)
        # 小尾巴：指向头像那一侧（用户头像在右 → 绿气泡尾巴朝右；
        # AI 头像在左 → 白气泡尾巴朝左）。尾巴单独一次 fill，不做路径布尔运算
        if self.tail:
            h = max(12.0, r.height() * 0.28)
            y0 = r.top() + 8.0
            tri = QPolygonF([
                QPointF(r.right() + 0.5, y0) if self.green
                else QPointF(r.left() - 0.5, y0),
                QPointF(r.right() + TAIL + 0.5, y0 + h / 2) if self.green
                else QPointF(r.left() - TAIL - 0.5, y0 + h / 2),
                QPointF(r.right() + 0.5, y0 + h) if self.green
                else QPointF(r.left() - 0.5, y0 + h),
            ])
            p.drawPolygon(tri)


# ============================================================
# 消息行
# ============================================================

def _row(margins=(12, 3, 12, 3)) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setContentsMargins(*margins)
    lay.setSpacing(8)
    return lay


class ChatWindow(QWidget):
    """主窗口。"""

    message_sent = pyqtSignal(str)      # 用户发出一句话（回车 / 发送键）

    def __init__(self, title="聊天", history_path=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._title_text = title
        self._history_path = history_path or os.path.join(
            paths.base_dir(), HISTORY_FILE)
        self._ai_pix = ai_avatar()
        self._user_pix = user_avatar()
        self._dock_side = ""            # "" / "left" / "right"
        self._collapsed = False
        self._dragging = False
        self._stream_bubble = None      # 流式回复中的那条气泡
        self._stream_host = None        # 流式气泡所在整行
        self._last_ts = 0.0
        self._stickers = []
        self._emoji_panel = None
        self._restore_geo = None        # 收起前的几何

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Window)
        self.setMinimumSize(MIN_W, MIN_H)
        self.resize(DEFAULT_W, DEFAULT_H)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(self.backgroundRole(), BG)
        self.setPalette(pal)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_title())
        root.addWidget(self._build_list(), 1)
        self._panel_host = QVBoxLayout()
        self._panel_host.setContentsMargins(0, 0, 0, 0)
        root.addLayout(self._panel_host)
        root.addWidget(self._build_input())
        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 0, 0)
        grip_row.addStretch(1)
        self._grip = QSizeGrip(self)
        grip_row.addWidget(self._grip)
        root.addLayout(grip_row)

        self._dock_timer = QTimer(self)
        self._dock_timer.setInterval(POLL_MS)
        self._dock_timer.timeout.connect(self._poll_dock)
        self._dock_timer.start()

        self.load_history()
        self._scan_stickers()

    # ---------- UI 构建 ----------

    def _build_title(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(34)
        bar.setAutoFillBackground(True)
        pal = bar.palette()
        pal.setColor(bar.backgroundRole(), TITLE_BG)
        bar.setPalette(pal)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 0, 6, 0)
        self._title_label = QLabel(self._title_text)
        self._title_label.setStyleSheet("color:#333; background:transparent;")
        lay.addWidget(self._title_label)
        lay.addStretch(1)
        close = QPushButton("✕")
        close.setFixedSize(34, 34)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(
            "QPushButton{border:none;background:transparent;color:#666;"
            "font-size:14px;}"
            "QPushButton:hover{background:#E81123;color:white;}")
        close.clicked.connect(self.close)
        lay.addWidget(close)
        self._title_bar = bar
        _install_title_drag(self, bar)
        return bar

    def _build_list(self) -> QScrollArea:
        self._msg_holder = QWidget()
        self._msg_lay = QVBoxLayout(self._msg_holder)
        self._msg_lay.setContentsMargins(0, 8, 0, 8)
        self._msg_lay.setSpacing(ROW_SPACING)
        self._msg_lay.addStretch(1)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setWidget(self._msg_holder)
        # 注意：不要给 QScrollArea 挂 QWidget{} 通配规则——它会匹配到所有
        # 子 QLabel，把它们自己的 color 样式吞掉（实测文字全部变白）。
        # 背景透明靠窗口调色板 + viewport 不自填底色实现。
        self._msg_holder.setAutoFillBackground(False)
        area.viewport().setAutoFillBackground(False)
        self._scroll = area
        return area

    def _build_input(self) -> QWidget:
        bar = QWidget()
        bar.setAutoFillBackground(True)
        pal = bar.palette()
        pal.setColor(bar.backgroundRole(), QColor("#F7F7F7"))
        bar.setPalette(pal)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(6)

        self._emoji_btn = QPushButton("😊")
        self._emoji_btn.setFixedSize(30, 30)
        self._emoji_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._emoji_btn.setStyleSheet(
            "QPushButton{border:none;background:transparent;font-size:18px;}"
            "QPushButton:hover{background:#E0E0E0;border-radius:4px;}")
        self._emoji_btn.clicked.connect(self._toggle_emoji_panel)
        lay.addWidget(self._emoji_btn)

        self._input = QPlainTextEdit()
        self._input.setPlaceholderText("说点什么…（Enter 发送，Shift+Enter 换行）")
        self._input.setFixedHeight(56)
        self._input.setStyleSheet(
            "QPlainTextEdit{border:1px solid #DDD;border-radius:6px;"
            "background:white;color:#111111;padding:4px 6px;font-size:10pt;}")
        lay.addWidget(self._input, 1)

        send = QPushButton("发送")
        send.setFixedSize(64, 32)
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(
            "QPushButton{background:#07C160;color:white;border:none;"
            "border-radius:5px;font-size:10pt;}"
            "QPushButton:hover{background:#06AD56;}")
        send.clicked.connect(self._send_input)
        lay.addWidget(send)
        self._send_btn = send

        self._input.installEventFilter(self)
        return bar

    def eventFilter(self, obj, ev):
        if obj is self._input and ev.type() == ev.Type.KeyPress:
            if (ev.key() in (ev.Key.Return, ev.Key_Enter)
                    and not ev.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                self._send_input()
                return True
        return super().eventFilter(obj, ev)

    # ---------- 表情 / 表情包 ----------

    def _scan_stickers(self):
        """扫描表情包目录（默认 exe 旁边 stickers/，可注入自定义路径）。"""
        if not getattr(self, "_stickers_dir", None):
            try:
                d = paths.ensure_external("stickers", seed_from_resource=True)
            except Exception:
                d = os.path.join(paths.base_dir(), "stickers")
            self._stickers_dir = d
        d = self._stickers_dir
        self._stickers = []
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                if name.lower().endswith(STICKER_EXTS):
                    self._stickers.append(os.path.join(d, name))

    def _toggle_emoji_panel(self):
        if self._emoji_panel is None:
            self._emoji_panel = self._build_emoji_panel()
            self._panel_host.addWidget(self._emoji_panel)
        self._emoji_panel.setVisible(not self._emoji_panel.isVisible())
        if self._emoji_panel.isVisible():
            self._refresh_emoji_panel()

    def _build_emoji_panel(self) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet(
            "QWidget{background:#FAFAFA;border-top:1px solid #E3E3E3;}")
        self._emoji_lay = QVBoxLayout(panel)
        self._emoji_lay.setContentsMargins(8, 8, 8, 8)
        self._emoji_lay.setSpacing(6)
        return panel

    def _refresh_emoji_panel(self):
        while self._emoji_lay.count():
            item = self._emoji_lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        # emoji 网格
        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)
        for i, em in enumerate(EMOJIS):
            b = QPushButton(em)
            b.setFixedSize(34, 32)
            b.setToolTip("点击加入输入框")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                "QPushButton{border:none;background:transparent;font-size:16px;}"
                "QPushButton:hover{background:#E8E8E8;border-radius:4px;}")
            b.clicked.connect(lambda _=False, e=em: self._insert_emoji(e))
            grid.addWidget(b, i // 8, i % 8)
        self._emoji_lay.addWidget(grid_host)
        # 表情包区
        if self._stickers:
            cap = QLabel("表情包（stickers/ 目录，往里丢图就能加）")
            cap.setStyleSheet("color:#888; background:transparent;font-size:9pt;")
            self._emoji_lay.addWidget(cap)
            host = QWidget()
            lay = QHBoxLayout(host)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(6)
            for path in self._stickers[:24]:
                pix = QPixmap(path)
                if pix.isNull():
                    continue
                b = QPushButton()
                b.setFixedSize(64, 64)
                b.setIcon(pix.scaled(
                    58, 58, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
                b.setIconSize(QSize(58, 58))
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.setStyleSheet("QPushButton{border:none;background:transparent;}"
                                "QPushButton:hover{background:#E8E8E8;"
                                "border-radius:6px;}")
                b.clicked.connect(lambda _=False, p=path: self.send_sticker(p))
                lay.addWidget(b)
            lay.addStretch(1)
            self._emoji_lay.addWidget(host)
        else:
            tip = QLabel("还没有表情包：往 exe 旁边的 stickers/ 文件夹丢几张图片，"
                         "重新点开这里就能用")
            tip.setWordWrap(True)
            tip.setStyleSheet("color:#AAA; background:transparent;font-size:9pt;")
            self._emoji_lay.addWidget(tip)

    def _insert_emoji(self, em: str):
        cur = self._input.textCursor()
        cur.insertText(em)
        self._input.setFocus()

    def _send_input(self):
        text = self._input.toPlainText().strip()
        if not text:
            return
        self._input.clear()
        self.append_user(text)
        self.message_sent.emit(text)

    def send_sticker(self, path: str):
        """发出一张表情包：只进聊天记录，不打扰 AI。"""
        pix = QPixmap(path)
        if pix.isNull():
            return
        self.append_user_image(path, pix)
        self._save({"role": "user", "kind": "img", "text": path,
                    "ts": time.time()})

    # ---------- 消息追加（PetWindow 调用） ----------

    def _maybe_time_separator(self, ts: float):
        if self._last_ts and ts - self._last_ts >= TIME_GAP:
            self._add_time_label(ts)
        self._last_ts = ts

    def _add_time_label(self, ts: float):
        lbl = QLabel(time.strftime("%m月%d日 %H:%M", time.localtime(ts)))
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("color:%s; background:transparent; font-size:9pt;"
                          % TIME_GRAY.name())
        self._add_row_widget(lbl)

    def _add_row_widget(self, w: QWidget):
        self._msg_lay.insertWidget(self._msg_lay.count() - 1, w)
        QTimer.singleShot(0, self._scroll_bottom)

    def _scroll_bottom(self):
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _bubbles_max_w(self) -> int:
        return max(120, int(self._scroll.viewport().width() * VIEW_RATIO)
                   - BUBBLE_PAD * 2 - TAIL * 2)

    def append_user(self, text: str, ts: float = None):
        ts = ts or time.time()
        self._maybe_time_separator(ts)
        b = Bubble(text_clean.clean(text), green=True)
        b.fit_text(b.label.text(), self._bubbles_max_w())
        lay = _row()
        lay.addStretch(1)
        lay.addWidget(b)
        av = QLabel()
        av.setPixmap(self._user_pix)
        av.setFixedSize(AVATAR, AVATAR)
        lay.addWidget(av)
        host = QWidget()
        host.setLayout(lay)
        self._add_row_widget(host)
        self._save({"role": "user", "kind": "text", "text": text,
                    "ts": ts})

    def append_user_image(self, path: str, pix: QPixmap = None,
                          ts: float = None):
        ts = ts or time.time()
        self._maybe_time_separator(ts)
        pix = pix or QPixmap(path)
        if pix.isNull():
            return
        scaled = pix.scaled(120, 120, Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
        b = Bubble("", green=True, image=scaled)
        b.adjustSize()
        b.setFixedHeight(b.sizeHint().height())
        lay = _row()
        lay.addStretch(1)
        lay.addWidget(b)
        av = QLabel()
        av.setPixmap(self._user_pix)
        av.setFixedSize(AVATAR, AVATAR)
        lay.addWidget(av)
        host = QWidget()
        host.setLayout(lay)
        self._add_row_widget(host)

    def stream_ai_begin(self):
        """AI 开始回复：先占一条流式气泡（内容会边到边刷）。"""
        self._stream_bubble = None
        self._stream_ts = time.time()
        self._maybe_time_separator(self._stream_ts)
        b = Bubble("", green=False)
        b.fit_text("…", self._bubbles_max_w())
        self._stream_bubble = b
        lay = _row()
        av = QLabel()
        av.setPixmap(self._ai_pix)
        av.setFixedSize(AVATAR, AVATAR)
        lay.addWidget(av)
        lay.addWidget(b)
        lay.addStretch(1)
        host = QWidget()
        host.setLayout(lay)
        self._stream_host = host
        self._add_row_widget(host)

    def stream_ai_update(self, text: str):
        b = self._stream_bubble
        if b is None:
            self.stream_ai_begin()
            b = self._stream_bubble
        shown = text_clean.clean(text) or "…"
        b.fit_text(shown, self._bubbles_max_w())
        QTimer.singleShot(0, self._scroll_bottom)

    def stream_ai_finish(self, text: str, ts: float = None):
        """收尾：把流式气泡定型（空文本就补一条占位）。"""
        ts = ts if ts is not None else getattr(self, "_stream_ts",
                                               time.time())
        b = self._stream_bubble
        if b is None:
            self.append_ai(text or "（什么都没说…）", ts=ts)
            return
        shown = text_clean.clean(text) or "（什么都没说…）"
        b.fit_text(shown, self._bubbles_max_w())
        self._stream_bubble = None
        self._stream_host = None
        self._save({"role": "ai", "kind": "text",
                    "text": text if text.strip() else shown, "ts": ts})
        QTimer.singleShot(0, self._scroll_bottom)

    def append_ai(self, text: str, ts: float = None):
        ts = ts or time.time()
        self._maybe_time_separator(ts)
        b = Bubble(text_clean.clean(text), green=False)
        b.fit_text(b.label.text(), self._bubbles_max_w())
        lay = _row()
        av = QLabel()
        av.setPixmap(self._ai_pix)
        av.setFixedSize(AVATAR, AVATAR)
        lay.addWidget(av)
        lay.addWidget(b)
        lay.addStretch(1)
        host = QWidget()
        host.setLayout(lay)
        self._add_row_widget(host)
        self._save({"role": "ai", "kind": "text", "text": text, "ts": ts})

    def stream_ai_abort(self):
        """这轮回复失败了：把流式占位气泡整行撤掉（不落盘）。"""
        b = self._stream_bubble
        if b is None:
            return
        self._stream_bubble = None
        self._stream_host = None
        b.deleteLater()

    def append_note(self, text: str):
        """过程提示（正在用 xx / 权限请求）：灰色居中小字，不落盘。"""
        lbl = QLabel(text_clean.clean(text))
        lbl.setWordWrap(True)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("color:%s; background:transparent; font-size:9pt;"
                          % NOTE_GRAY.name())
        self._add_row_widget(lbl)

    def append_error(self, text: str):
        lbl = QLabel(text_clean.clean(text))
        lbl.setWordWrap(True)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("color:#C25B5B; background:transparent;"
                          "font-size:9pt;")
        self._add_row_widget(lbl)

    def set_title(self, title: str):
        self._title_text = title
        self._title_label.setText(title)

    # ---------- 落盘 / 回显 ----------

    def _save(self, item: dict):
        if getattr(self, "_loading", False):
            return
        try:
            data = self._read_history()
            data.append(item)
            if len(data) > HISTORY_KEEP:
                data = data[-HISTORY_KEEP:]
            tmp = self._history_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self._history_path)
        except Exception:
            pass                      # 记录失败不影响聊天

    def _read_history(self) -> list:
        try:
            with open(self._history_path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def load_history(self):
        self._loading = True
        try:
            data = self._read_history()[-SHOW_ON_LOAD:]
            for item in data:
                ts = float(item.get("ts") or 0) or time.time()
                kind = item.get("kind") or "text"
                role = item.get("role")
                text = str(item.get("text") or "")
                if kind == "img":
                    if role == "user":
                        self.append_user_image(text, ts=ts)
                    continue
                if role == "user":
                    self.append_user(text, ts=ts)
                elif role == "ai":
                    self.append_ai(text, ts=ts)
        finally:
            self._loading = False
        self._scroll_bottom()

    # ---------- 左右边缘吸附（老 QQ 那种） ----------

    def _screen(self) -> QRect:
        scr = (self.screen() or QApplication.primaryScreen())
        return scr.availableGeometry()

    def moveEvent(self, ev):
        super().moveEvent(ev)
        if self._dragging:
            self._check_snap()

    def _check_snap(self):
        scr = self._screen()
        x, w = self.x(), self.width()
        if x <= scr.left() + SNAP_PX:
            self._dock_side = "left"
        elif x + w >= scr.right() - SNAP_PX:
            self._dock_side = "right"
        else:
            self._dock_side = ""
        if self._dock_side:
            self._snap_flush()

    def _snap_flush(self):
        scr = self._screen()
        if self._dock_side == "left":
            self.move(scr.left(), self.y())
        else:
            self.move(scr.right() + 1 - self.width(), self.y())

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self._dock_side and not self._collapsed:
            self._snap_flush()
        # 气泡宽度跟窗口走：已渲染的不再重排（避免每帧全列表重算），
        # 流式中的那条跟随即可
        if self._stream_bubble is not None:
            self._stream_bubble.fit_text(self._stream_bubble.label.text(),
                                         self._bubbles_max_w())

    # ---- 收起 / 弹出 ----

    def _poll_dock(self, cursor=None):
        """定时看鼠标：贴边且鼠标离开 → 缩成边条；悬停边条 → 弹回。

        cursor 可注入（测试用）；不传就读真实光标。
        """
        if self._dragging:
            return
        cursor = cursor or QCursor.pos()
        scr = self._screen()
        if self._collapsed:
            strip = self.geometry().adjusted(-6, -6, 6, 6)
            if strip.contains(cursor):
                self._undock_restore()
            return
        if not self._dock_side:
            return
        inside = self.geometry().adjusted(-2, -2, 2, 2).contains(cursor)
        if not inside:
            self._dock_collapse()

    def _dock_collapse(self):
        if self._collapsed or not self._dock_side:
            return
        self._restore_geo = self.geometry()
        h = self.height()
        y = self._restore_geo.y()
        scr = self._screen()
        y = max(scr.top(), min(y, scr.bottom() - h))
        self._collapsed = True
        # 收成细边条要突破常规最小宽度，弹回时再恢复
        self.setMinimumWidth(2)
        if self._dock_side == "left":
            self.setGeometry(scr.left(), y, COLLAPSED_W, h)
        else:
            self.setGeometry(scr.right() + 1 - COLLAPSED_W, y, COLLAPSED_W, h)

    def _undock_restore(self):
        if not self._collapsed:
            return
        g = self._restore_geo or QRect(self.x(), self.y(),
                                       DEFAULT_W, DEFAULT_H)
        self._collapsed = False
        self.setMinimumWidth(MIN_W)
        h = min(g.height(), self._screen().height())
        w = min(max(g.width(), MIN_W), self._screen().width())
        y = max(self._screen().top(),
                min(g.y(), self._screen().bottom() - h))
        if self._dock_side == "left":
            self.setGeometry(self._screen().left(), y, w, h)
        else:
            self.setGeometry(self._screen().right() + 1 - w, y, w, h)

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if self._collapsed:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            col = QColor("#07C160")
            w = self.width()
            x = 0 if self._dock_side == "left" else w - 3
            p.fillRect(QRect(x, self.height() // 4, 3, self.height() // 2),
                       col)

    # ---------- 标题栏拖动 ----------

    def _title_press(self, gpos: QPoint):
        self._dragging = True
        self._drag_off = gpos - self.pos()
        if self._collapsed:
            self._undock_restore()
            self._drag_off = gpos - self.pos()

    def _title_move(self, gpos: QPoint):
        if not self._dragging:
            return
        self.move(gpos - self._drag_off)

    def _title_release(self):
        self._dragging = False

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self._collapsed:
            # 点边条也能弹开
            self._undock_restore()
        super().mousePressEvent(ev)

    def closeEvent(self, ev):
        if self._collapsed:
            self._undock_restore()
        super().closeEvent(ev)


# 标题栏拖动绑定（挂到 _build_title 生成的 bar 上）
def _install_title_drag(win: ChatWindow, bar: QWidget):
    bar.mousePressEvent = lambda ev: (
        win._title_press(ev.globalPosition().toPoint())
        if ev.button() == Qt.MouseButton.LeftButton else None)
    bar.mouseMoveEvent = lambda ev: win._title_move(
        ev.globalPosition().toPoint())
    bar.mouseReleaseEvent = lambda ev: win._title_release()
    bar.mouseDoubleClickEvent = lambda ev: None
