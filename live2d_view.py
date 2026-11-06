# -*- coding: utf-8 -*-
"""Live2D 角色渲染层。

给桌宠用的 QOpenGLWidget 子类：加载 converter 导出的 .moc3，自动眨眼/呼吸 +
循环待机动作，30~60fps 重绘。

设计原则：**永远不能拖垮桌宠**。
- 依赖缺失 / 模型文件不存在 / GL 初始化失败，都只等价于「这个功能不存在」，
  由 `find_model_json()` 返回 None 或 `failed` 信号告知调用方，主程序照常跑
  assets/anim/ 的序列帧方案。
- 本模块被 import 时会设置全局 QSurfaceFormat（alpha=8 + 4x MSAA），
  所以必须在 QApplication 创建之前完成 import。

用法：
    import live2d_view
    json_path = live2d_view.find_model_json()      # None 表示不可用
    live2d_view.ensure_init()                      # QApplication 之后调用
    view = live2d_view.Live2DView(parent, json_path, size=(250, 375))
    view.failed.connect(on_fail)                   # 失败时隐藏它、走回退
"""

import os

_FOUND = True
_IMPORT_ERR = ""
try:
    from PyQt6.QtCore import Qt, pyqtSignal
    from PyQt6.QtGui import QSurfaceFormat
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget

    import live2d
    from live2d import Model
except Exception as _exc:          # 缺 PyQt6.QtOpenGLWidgets / live2d-py 时整块禁用
    _FOUND = False
    _IMPORT_ERR = "%s: %s" % (type(_exc).__name__, _exc)

if _FOUND:
    # 带 alpha 的 GL 表面：透明桌宠窗口全靠它
    _fmt = QSurfaceFormat()
    _fmt.setAlphaBufferSize(8)
    _fmt.setSamples(4)
    QSurfaceFormat.setDefaultFormat(_fmt)

# 模型候选（相对 <根目录>/）：优先 see-through 1280 重制版，找不到再回退旧模型
_MODEL_CANDIDATES = (
    ("live2d", "model_st1280", "feiyu.model3.json"),
    ("live2d", "model", "feiyu_live2d.model3.json"),
)

# 动作组 -> 菜单里显示的中文名（PSD2Live 自动生成的组名）
MOTION_LABELS = {
    "Idle": "呼吸待机",
    "Blink": "眨眼",
    "Nod": "点头",
    "Shake": "摇头",
}

# 表情 -> 菜单中文名
EXPRESSION_LABELS = {
    "smile": "微笑",
    "sad": "委屈",
    "angry": "生气",
    "surprised": "惊讶",
}

# 角色在窗口里的取景：新模型画布 1280x1280 正方形，250x375 窗口实测 0.85 最合适
# （1.0 会把头顶呆毛和脚裁掉，0.7 偏小；见 live2d/_scale_sweep.png）
DEFAULT_SCALE = 0.85
DEFAULT_OFFSET_Y = 0.0

_INITIALIZED = False


def available() -> bool:
    """这套渲染在本机是否具备运行条件（依赖层面）。"""
    return _FOUND


def import_error() -> str:
    return _IMPORT_ERR if _FOUND else _IMPORT_ERR


def _roots():
    """候选根目录：exe 旁的外部目录优先，其次包内只读资源。"""
    try:
        import paths
        return [paths.base_dir(), paths.resource_dir()]
    except Exception:
        return [os.path.dirname(os.path.abspath(__file__))]


def find_model_json():
    """找 model3.json；找不到返回 None（调用方应回退到序列帧）。"""
    for base in _roots():
        for rel in _MODEL_CANDIDATES:
            p = os.path.join(base, *rel)
            if os.path.isfile(p):
                return p
    return None


def ensure_init():
    """初始化 Cubism 核心。必须在 QApplication 创建之后调用，幂等。"""
    global _INITIALIZED
    if not _FOUND or _INITIALIZED:
        return _INITIALIZED
    try:
        live2d.init()
        _INITIALIZED = True
    except Exception as exc:
        print("[live2d] init 失败：%r" % (exc,))
        _INITIALIZED = False
    return _INITIALIZED


def dispose():
    global _INITIALIZED
    if _INITIALIZED:
        try:
            live2d.dispose()
        except Exception:
            pass
        _INITIALIZED = False


class Live2DView(QOpenGLWidget):
    """桌宠的 Live2D 角色层。

    鼠标事件穿透到父窗口（父窗口负责拖拽和右键菜单），角色本身只负责画。
    """

    failed = pyqtSignal(str)      # 模型加载失败，调用方请回退

    def __init__(self, parent=None, model_json=None, size=(250, 375),
                 scale=DEFAULT_SCALE, offset_y=DEFAULT_OFFSET_Y):
        super().__init__(parent)
        # PyQt6 里叫 WA_TransparentForMouseEvents（Qt5 是 WA_TransparentForMouse）
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(size[0], size[1])

        self.model_json = model_json
        self._scale = scale
        self._offset_y = offset_y
        self.model = None
        self.ok = False
        self.err = None

    # ---------- GL ----------
    def initializeGL(self):
        try:
            ensure_init()
            live2d.glInit()
        except Exception as exc:
            self.err = "GL 初始化失败：%r" % (exc,)
            print("[live2d]", self.err)
            self.failed.emit(self.err)
            return

        try:
            # 注意：必须无参构造 + LoadModelJson。
            # Model(path) 会拿到一个无效对象，后续任何方法调用直接段错误。
            m = Model()
            m.LoadModelJson(self.model_json)
        except Exception as exc:
            self.err = "模型加载失败：%r" % (exc,)
            print("[live2d]", self.err)
            self.failed.emit(self.err)
            return

        self.model = m
        try:
            m.Resize(self.width(), self.height())
            m.SetScale(self._scale)
            m.SetOffset(0.0, self._offset_y)
            m.SetAutoBlink(True)
            m.SetAutoBreath(True)
            self._start_idle()
        except Exception as exc:
            print("[live2d] 初始化参数失败：%r" % (exc,))
        self.ok = True

    def resizeGL(self, w, h):
        if self.model:
            try:
                self.model.Resize(w, h)
            except Exception:
                pass

    def set_view_size(self, w, h):
        """改变角色层像素尺寸（跟随桌宠整体缩放）。

        窗口等比缩放时 SetScale 保持不变即可让角色跟着等比放大/缩小，
        这里只同步部件尺寸和模型渲染视口。
        """
        w, h = max(64, int(w)), max(64, int(h))
        if (w, h) == (self.width(), self.height()):
            return
        self.setFixedSize(w, h)
        if self.model:
            try:
                self.model.Resize(w, h)
            except Exception:
                pass
        self.update()

    def paintGL(self):
        live2d.clearBuffer()
        if not self.model:
            return
        self.model.Update()
        self.model.Draw()

    # ---------- 对外：动作 / 表情 ----------
    def _start_idle(self):
        try:
            self.model.StartRandomMotion("Idle", live2d.MotionPriority.FORCE)
        except Exception:
            pass

    def motions(self):
        """[(组名, 中文名)]，供右键菜单列动作。"""
        if not self.model:
            return []
        out = []
        try:
            for g in self.model.GetMotionGroups():
                out.append((g, MOTION_LABELS.get(g, g)))
        except Exception:
            pass
        return out

    def expressions(self):
        """[(表情名, 中文名)]。"""
        if not self.model:
            return []
        out = []
        try:
            for e in self.model.GetExpressions():
                out.append((e, EXPRESSION_LABELS.get(e, e)))
        except Exception:
            pass
        return out

    def play_motion(self, group: str):
        if not self.model:
            return
        try:
            self.model.StartMotion(group, 0, live2d.MotionPriority.FORCE)
        except Exception:
            pass

    def set_expression(self, name: str):
        if not self.model:
            return
        try:
            self.model.SetExpression(name)
        except Exception:
            pass
