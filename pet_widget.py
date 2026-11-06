"""桌宠角色绘制。

用 QPainter 纯代码画一只圆滚滚的小角色，带三种状态：
    idle     待机：轻微呼吸起伏，眼睛偶尔眨
    thinking 思考：眼睛向上看，头顶冒小圆点
    talking  说话：嘴巴开合

想换成自己的立绘，见文件末尾的替换说明。
"""

import json
import math
import os

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush, QColor, QLinearGradient, QMovie, QPainter, QPainterPath, QPen,
    QPixmap,
)

# ---------- 配色（换成你喜欢的颜色即可） ----------
BODY_TOP = QColor("#8ab6f0")
BODY_BOTTOM = QColor("#5a8fdb")
BELLY = QColor("#eaf3ff")
OUTLINE = QColor("#2c4a72")
BLUSH = QColor("#f59ab0")
EYE = QColor("#1e2f45")
MOUTH = QColor("#20344d")


def draw_overlays(painter: QPainter, w: int, h: int, state: str, t: float):
    """状态特效：思考气泡点 / 唱歌音符。立绘模式和序列帧模式共用。"""
    if state == "thinking":
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(3):
            phase = (t * 2.4 - i * 0.42) % (math.pi * 2)
            alpha = int(110 + 120 * (0.5 + 0.5 * math.sin(phase)))
            r = 5 + i * 2
            painter.setBrush(QColor(120, 160, 230, alpha))
            painter.drawEllipse(QRectF(w * 0.72 + i * 14, h * 0.06 - i * 8, r, r))
        painter.restore()

    if state == "talking":
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for i in range(3):
            phase = (t * 1.6 + i * 0.7) % 1.0
            alpha = int(200 * (1 - phase))
            if alpha <= 4:
                continue
            x = w * 0.70 + math.sin(phase * 6 + i) * 8 + i * 10
            y = h * 0.42 - phase * h * 0.34
            size = 5 + i * 1.5
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(150, 200, 255, alpha))
            painter.save()
            painter.translate(x, y)
            painter.rotate(-20)
            painter.drawEllipse(QRectF(-size / 2, -size / 2, size, size * 0.7))
            painter.restore()
            painter.setPen(QPen(QColor(150, 200, 255, alpha), 1.6,
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(QPointF(x + size / 2 - 1, y - 1),
                             QPointF(x + size / 2 - 1, y - size * 2.2))
            painter.setBrush(QColor(150, 200, 255, alpha))
            painter.drawEllipse(
                QRectF(x + size / 2 - 1, y - size * 2.4, size * 0.8, size * 0.6)
            )
        painter.restore()


def _fit_and_draw(painter: QPainter, pm: QPixmap, w: int, h: int,
                  breathe: float = 0.0, tilt: float = 0.0):
    """等比缩放把一帧画到窗口底部居中。"""
    if pm.isNull():
        return
    target_h = int(h * 0.92)
    target_w = int(pm.width() * target_h / pm.height())
    target_w = min(target_w, w)

    painter.save()
    painter.translate(w / 2.0, h / 2.0)
    painter.rotate(tilt)
    painter.translate(0, breathe - target_h / 2.0)
    painter.drawPixmap(
        int(-target_w / 2), -int(target_h * 0.08),
        pm.scaled(target_w, target_h,
                  Qt.AspectRatioMode.KeepAspectRatio,
                  Qt.TransformationMode.SmoothTransformation),
    )
    painter.restore()


class PetAnimRenderer:
    """序列帧 / GIF 动画模式。

    素材约定（放在 assets/anim/ 下，一个子目录一个动作）：
        assets/anim/idle/            00.png 01.png ...（按文件名排序）
        assets/anim/idle/meta.json   可选：label（菜单里显示的中文名）、
                                     fps、timeline（播放顺序）
        assets/anim/wave/ hug/ sit/ ...  其余动作，右键菜单可切换
        assets/anim/idle.gif         或者直接一个 gif

    meta.json 用来把「眨眼」这类瞬间动作塞进长时间循环里，避免每 0.5 秒
    眨一次的鬼畜效果：
        {"label": "站立待机", "fps": 10, "timeline": "00112233"}
    timeline 一个字符一帧，数字是帧序号，重复写就是让这一帧多停几拍。
    不写 timeline 就按顺序循环所有帧。

    聊天时的 thinking / talking 状态没有专门素材，会自动用当前待机动作
    的帧再叠加思考点 / 音符特效。
    """

    FPS = 8.0   # 序列帧默认帧率（meta.json 可覆盖）

    def __init__(self, anim_dir: str, fallback=None):
        self.anim_dir = anim_dir
        self.fallback = fallback
        self.state = "idle"
        self.t = 0.0
        self.blink = 0.0
        self._frames = {}   # state -> [QPixmap]
        self._seq = {}      # state -> (fps, [帧序号...])
        self._movies = {}   # state -> QMovie
        self._labels = {}   # state -> 中文动作名（meta.json 的 label）
        self.idle_key = "idle"
        self._load()

    # ---------- 素材加载 ----------
    def _load(self):
        if not os.path.isdir(self.anim_dir):
            return
        # anim/ 下每个含序列帧的子目录都算一个动作（idle/wave/hug/...）
        poses = sorted(
            d for d in os.listdir(self.anim_dir)
            if os.path.isdir(os.path.join(self.anim_dir, d))
        )
        for state in poses:
            loaded = self._load_frames(state)
            if loaded:
                frames, (fps, indices, label) = loaded
                self._frames[state] = frames
                self._seq[state] = (fps, indices)
                if label:
                    self._labels[state] = label
                continue
            movie = self._load_gif(state)
            if movie:
                self._movies[state] = movie
        # 待机动作可以从菜单里换成别的姿势
        self.idle_key = "idle" if "idle" in self._frames else (
            next(iter(self._frames), "idle"))

    def _load_frames(self, state: str):
        d = os.path.join(self.anim_dir, state)
        if not os.path.isdir(d):
            return None
        files = sorted(
            f for f in os.listdir(d)
            if f.lower().endswith((".png", ".webp", ".jpg", ".jpeg"))
        )
        frames = []
        for f in files:
            pm = QPixmap(os.path.join(d, f))
            if not pm.isNull():
                frames.append(pm)
        if not frames:
            return None
        fps, indices, label = self._load_meta(d, len(frames))
        return frames, (fps, indices, label)

    def _load_meta(self, state_dir: str, n_frames: int):
        """读 meta.json；坏了就当没有，用默认帧率顺序循环。"""
        fps, indices, label = self.FPS, list(range(n_frames)), None
        path = os.path.join(state_dir, "meta.json")
        if not os.path.isfile(path):
            return fps, indices, label
        try:
            with open(path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            return fps, indices, label
        if not isinstance(meta, dict):
            return fps, indices, label

        if isinstance(meta.get("label"), str):
            label = meta["label"]

        try:
            fps = float(meta.get("fps", fps))
        except (TypeError, ValueError):
            fps = self.FPS
        if fps <= 0.1 or fps > 60:
            fps = self.FPS

        timeline = meta.get("timeline")
        if isinstance(timeline, str):
            seq = [int(c) for c in timeline if c.isdigit()]
            seq = [i for i in seq if i < n_frames]
            if seq:
                indices = seq
        return fps, indices, label

    def _load_gif(self, state: str):
        path = os.path.join(self.anim_dir, f"{state}.gif")
        if not os.path.isfile(path):
            return None
        movie = QMovie(path)
        if not movie.isValid():
            return None
        movie.setCacheMode(QMovie.CacheMode.CacheAll)
        movie.start()
        return movie

    @property
    def has_assets(self) -> bool:
        return bool(self._frames or self._movies)

    def poses(self):
        """可用动作：[(key, 中文名)]，菜单用。"""
        out = []
        for key in sorted(set(self._frames) | set(self._movies)):
            out.append((key, self._labels.get(key, key)))
        return out

    def set_idle_pose(self, key: str):
        """把待机动画换成某个姿势（key 必须是 poses() 里的）。"""
        if key in self._frames or key in self._movies:
            self.idle_key = key

    def current_pose_label(self) -> str:
        return self._labels.get(self.idle_key, self.idle_key)

    def _current_pixmap(self) -> QPixmap:
        state = self.state
        if state == "idle":
            state = self.idle_key
        # 该状态没素材就退回待机姿势
        if state not in self._frames and state not in self._movies:
            state = self.idle_key
        seg = self._seq.get(state)
        if seg:
            fps, indices = seg
            return self._frames[state][indices[int(self.t * fps) % len(indices)]]
        movie = self._movies.get(state)
        if movie:
            pm = movie.currentPixmap()
            if not pm.isNull():
                return pm
        return QPixmap()

    # ---------- 绘制 ----------
    def paint(self, painter: QPainter, w: int, h: int):
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        pm = self._current_pixmap()

        if pm.isNull():
            # 素材异常时退回静态立绘
            if self.fallback:
                self.fallback.state = self.state
                self.fallback.t = self.t
                self.fallback.paint(painter, w, h)
                return

        # 轻微的呼吸浮动，让动画本身不至于太死板
        breathe = math.sin(self.t * 2.2) * 1.5
        _fit_and_draw(painter, pm, w, h, breathe)
        draw_overlays(painter, w, h, self.state, self.t)


class PetImageRenderer:
    """立绘模式：直接渲染 assets/pet.png，带呼吸浮动。

    立绘是静态图，状态区别用轻微变化表达：
    - thinking：角色轻微倾斜 + 头顶画思考点
    - talking ：角色轻微上下跳动
    """

    def __init__(self, image_path: str):
        self.state = "idle"
        self.t = 0.0
        self.blink = 0.0  # 立绘模式不眨眼，仅为接口兼容

        pm = QPixmap(image_path)
        if pm.isNull():
            raise FileNotFoundError(f"立绘加载失败：{image_path}")
        self.pixmap = pm
        self.base_scale = None  # 首帧根据窗口算

    def paint(self, painter: QPainter, w: int, h: int):
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        breathe = math.sin(self.t * 2.2) * 2.0
        if self.state == "talking":
            breathe += math.sin(self.t * 12) * 1.5   # 说话时轻微蹦跳
        tilt = 2.0 if self.state == "thinking" else 0.0

        _fit_and_draw(painter, self.pixmap, w, h, breathe, tilt)
        draw_overlays(painter, w, h, self.state, self.t)


# ============================================================
# 想换成自己的立绘（PNG 序列帧 / GIF）怎么办
# ============================================================
# 方案 A：立绘 + 手动浮动
#   1. 把 idle.png / thinking.png / talking.png 放进 assets/
#   2. __init__ 里 self.frames = {
#          "idle": QPixmap("assets/idle.png"),
#          "thinking": QPixmap("assets/thinking.png"),
#          "talking": QPixmap("assets/talking.png"),
#      }
#   3. paint() 里改成：
#          pm = self.frames[self.state]
#          pm = pm.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatio,
#                         Qt.TransformationMode.SmoothTransformation)
#          painter.drawPixmap((w - pm.width()) // 2, (h - pm.height()) // 2, pm)
#
# 方案 B：GIF / APNG 动图
#   用 QMediaPlayer + QVideoWidget 或 QMovie 叠在透明窗口上，
#   QMovie 更简单：self.movie = QMovie("assets/idle.gif")，
#   然后把 QLabel 设成 movie 的容器、背景透明即可。


class PetRenderer:
    """备用绘制器（代码画的团子）：立绘缺失时使用。"""

    def __init__(self):
        self.state = "idle"
        self.t = 0.0          # 累计时间（秒）
        self.blink = 0.0      # 0=睁眼 1=闭眼

    def paint(self, painter, w, h):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.save()
        scale = min(w, h) / 200.0
        painter.translate(w / 2.0, h / 2.0)
        painter.scale(scale, scale)
        painter.translate(-100, -100)

        breathe = math.sin(self.t * 2.2) * 2.0
        squash = 1.0 + math.sin(self.t * 2.2) * 0.015
        painter.translate(0, breathe)

        self._draw_shadow(painter, breathe)
        self._draw_body(painter, squash)
        self._draw_face(painter, squash)
        if self.state == "thinking":
            self._draw_thinking(painter)
        if self.state == "talking":
            self._draw_talk_spark(painter)
        painter.restore()

    def _draw_shadow(self, p, breathe):
        p.save()
        p.translate(0, 62 - breathe)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(20, 35, 55, 45))
        p.drawEllipse(QRectF(-42, -8, 84, 16))
        p.restore()

    def _draw_body(self, p, squash):
        p.save()
        p.scale(1.0, squash)
        path = QPainterPath()
        path.addEllipse(QRectF(-58, -58, 116, 116))
        grad = QLinearGradient(0, -58, 0, 58)
        grad.setColorAt(0.0, BODY_TOP)
        grad.setColorAt(1.0, BODY_BOTTOM)
        p.setBrush(QBrush(grad))
        p.setPen(QPen(OUTLINE, 2.6))
        p.drawPath(path)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(BELLY.red(), BELLY.green(), BELLY.blue(), 210))
        p.drawEllipse(QRectF(-30, 2, 60, 46))
        p.setBrush(QColor(255, 255, 255, 90))
        p.drawEllipse(QRectF(-34, -46, 30, 18))
        p.setBrush(QBrush(grad))
        p.setPen(QPen(OUTLINE, 2.4))
        for dx, tilt in ((-40, -14), (40, 14)):
            p.save()
            p.translate(dx, -48)
            p.rotate(tilt)
            ear = QPainterPath()
            ear.addEllipse(QRectF(-12, -14, 24, 28))
            p.drawPath(ear)
            p.restore()
        p.restore()

    def _draw_face(self, p, squash):
        p.save()
        p.scale(1.0, squash)
        eye_open = 1.0 - self.blink
        look_x, look_y = 0.0, 0.0
        if self.state == "thinking":
            look_x, look_y = 2.4, -2.6
        for cx in (-20, 20):
            if eye_open > 0.12:
                eh = 15 * eye_open
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(EYE)
                p.drawEllipse(QRectF(cx - 7.5 + look_x, -14 + look_y, 15, eh))
                p.setBrush(QColor(255, 255, 255, 215))
                p.drawEllipse(QRectF(cx - 4.5 + look_x, -11 + look_y, 5.5, 5.5))
            else:
                p.setPen(QPen(EYE, 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawArc(QRectF(cx - 8, -12, 16, 10), 200 * 16, 140 * 16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(BLUSH.red(), BLUSH.green(), BLUSH.blue(), 110))
        p.drawEllipse(QRectF(-36, 2, 15, 9))
        p.drawEllipse(QRectF(21, 2, 15, 9))
        p.setPen(QPen(MOUTH, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self.state == "talking":
            openness = 2.0 + 4.5 * (0.5 + 0.5 * math.sin(self.t * 12))
            p.setBrush(QColor(MOUTH))
            p.drawEllipse(QRectF(-6, 6, 12, openness + 2))
        else:
            p.drawArc(QRectF(-9, 2, 18, 14), 200 * 16, 140 * 16)
        p.restore()

    def _draw_thinking(self, p):
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(3):
            phase = (self.t * 2.4 - i * 0.42) % (math.pi * 2)
            alpha = int(90 + 120 * (0.5 + 0.5 * math.sin(phase)))
            r = 4 + i * 1.6
            p.setBrush(QColor(255, 255, 255, alpha))
            p.drawEllipse(QRectF(34 + i * 12, -62 - i * 10, r, r))

    def _draw_talk_spark(self, p):
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(2):
            phase = self.t * 3.2 + i * 1.6
            alpha = int(70 + 100 * (0.5 + 0.5 * math.sin(phase)))
            p.setBrush(QColor(255, 235, 160, alpha))
            cx = -62 - i * 8
            cy = -20 - math.sin(phase) * 8
            p.drawEllipse(QRectF(cx, cy, 5, 5))


def load_renderer():
    """按优先级选渲染器：

    1. assets/anim/ 下有 idle/thinking/talking 素材（序列帧或 gif）-> 动画模式
    2. assets/pet.png 存在 -> 静态立绘 + 程序动画（呼吸/倾斜/音符）
    3. 都没有 -> 代码绘制的团子
    """
    import paths

    def find(*rel):
        """优先 exe 旁边的 assets，其次包内自带。"""
        for base in (paths.base_dir(), paths.resource_dir()):
            p = os.path.join(base, *rel)
            if os.path.exists(p):
                return p
        return None

    static = None
    img = find("assets", "pet.png")
    if img:
        try:
            static = PetImageRenderer(img)
        except FileNotFoundError:
            static = None

    anim_dir = find("assets", "anim")
    if anim_dir:
        anim = PetAnimRenderer(anim_dir, fallback=static)
        if anim.has_assets:
            return anim

    if static:
        return static
    return PetRenderer()
