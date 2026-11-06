"""AI 设置对话框：填 API Key、选模型、测连通性。

桌宠本体是透明穿透的无边框窗口，聊天参数不适合塞在右键菜单里，
所以单独开一个正常的小窗口。
"""

from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFont
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QVBoxLayout, QWidget)

import config_store
import llm_client

KEY_PAGE = "https://platform.deepseek.com/api_keys"

# 预设：名字 -> (provider, base_url, 模型列表, 说明)
PRESETS = [
    ("DeepSeek 官方（推荐）", llm_client.PROVIDER_DEEPSEEK,
     llm_client.DEEPSEEK_BASE_URL,
     ["deepseek-flash", "deepseek-v4-pro"],
     "flash 又快又便宜，桌宠闲聊足够；v4-pro 推理更强但更慢更贵"),
    ("OpenAI 兼容中转 / 其他服务", llm_client.PROVIDER_CUSTOM,
     "https://api.deepseek.com",
     ["deepseek-flash"],
     "支持任何 OpenAI 格式的接口：填对方的 base_url 和模型名即可"),
    ("本地 Ollama（不联网、免费）", llm_client.PROVIDER_OLLAMA,
     llm_client.DEFAULT_HOST,
     ["qwen2.5:3b", "qwen2.5:7b"],
     "需要本机装有 Ollama 并已 pull 模型，不需要 API Key"),
]


class _TestWorker(QThread):
    """后台跑一次最短请求，避免点按钮时卡住界面。"""
    ok = pyqtSignal(str)
    bad = pyqtSignal(str)

    def __init__(self, kw):
        super().__init__()
        self.kw = kw

    def run(self):
        try:
            text = llm_client.chat_once(
                [{"role": "user", "content": "回复两个字：收到"}], **self.kw)
            self.ok.emit((text or "").strip() or "（空回复，但连接正常）")
        except llm_client.LLMError as exc:
            self.bad.emit(str(exc))
        except Exception as exc:            # 兜底，别让线程静默死
            self.bad.emit(f"出错了：{exc}")


class AISettingsDialog(QDialog):
    """返回结果：exec() 为 Accepted 时，self.cfg 是已写盘的新配置。"""

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = dict(cfg)
        self._worker = None
        self.setWindowTitle("AI 设置")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(520)
        self._font = QFont("Microsoft YaHei", 10)
        self.setFont(self._font)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        title = QLabel("桌宠用哪个 AI")
        f = QFont(self._font)
        f.setPointSize(12)
        f.setBold(True)
        title.setFont(f)
        root.addWidget(title)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(9)

        # 1. 服务商
        self.cb_preset = QComboBox()
        for name, *_ in PRESETS:
            self.cb_preset.addItem(name)
        form.addRow("服务商", self.cb_preset)

        # 2. API Key（密码框 + 显示开关）
        key_row = QWidget()
        kl = QHBoxLayout(key_row)
        kl.setContentsMargins(0, 0, 0, 0)
        kl.setSpacing(6)
        self.ed_key = QLineEdit()
        self.ed_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_key.setPlaceholderText("sk-…（在 DeepSeek 控制台创建）")
        self.chk_show = QCheckBox("显示")
        btn_key = QPushButton("获取 Key")
        btn_key.setFixedWidth(78)
        kl.addWidget(self.ed_key, 1)
        kl.addWidget(self.chk_show)
        kl.addWidget(btn_key)
        form.addRow("API Key", key_row)

        # 3. 模型
        self.cb_model = QComboBox()
        self.cb_model.setEditable(True)      # 允许手填中转站的自定义模型名
        form.addRow("模型", self.cb_model)

        # 4. 接口地址
        self.ed_base = QLineEdit()
        form.addRow("接口地址", self.ed_base)

        # 5. 思考模式
        self.chk_think = QCheckBox("开启思考模式（更聪明，但首字会等 2~10 秒）")
        form.addRow("", self.chk_think)

        # 6. 回复长度（会覆盖人设里写死的「50 字以内」）
        self.cb_len = QComboBox()
        for key, conf in llm_client.REPLY_STYLES.items():
            self.cb_len.addItem(conf["label"], key)
        form.addRow("回复长度", self.cb_len)

        root.addLayout(form)

        # 说明 / 状态
        self.lbl_hint = QLabel("")
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet("color:#666;")
        root.addWidget(self.lbl_hint)

        self.lbl_status = QLabel("")
        self.lbl_status.setWordWrap(True)
        root.addWidget(self.lbl_status)

        # 按钮区
        btns = QDialogButtonBox()
        self.btn_test = btns.addButton("测试连接", QDialogButtonBox.ButtonRole.ActionRole)
        btns.addButton("保存", QDialogButtonBox.ButtonRole.AcceptRole)
        btns.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        root.addWidget(btns)

        # 信号
        self.chk_show.toggled.connect(self._toggle_show)
        btn_key.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(KEY_PAGE)))
        self.cb_preset.currentIndexChanged.connect(self._apply_preset)
        self.btn_test.clicked.connect(self._test)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)

        self._load_from_cfg()
        self._apply_preset()

    # ---------- 初始化 ----------
    def _load_from_cfg(self):
        self.ed_key.setText(self.cfg.get("ai_api_key", ""))
        self.chk_think.setChecked(bool(self.cfg.get("ai_thinking", False)))
        self.ed_base.setText(self.cfg.get("ai_base_url", ""))

        style = self.cfg.get("ai_reply_style") or llm_client.DEFAULT_REPLY_STYLE
        pos = self.cb_len.findData(style)
        self.cb_len.setCurrentIndex(pos if pos >= 0 else 1)

        saved_model = self.cfg.get("ai_model", "")
        idx = 0
        for i, (_, prov, *_rest) in enumerate(PRESETS):
            if prov == self.cfg.get("ai_provider", ""):
                idx = i
                break
        self.cb_preset.setCurrentIndex(idx)
        self._fill_models(saved_model)

    def _fill_models(self, current: str = ""):
        _, _, _, models, _ = PRESETS[self.cb_preset.currentIndex()]
        self.cb_model.clear()
        self.cb_model.addItems(models)
        if current:
            pos = self.cb_model.findText(current)
            if pos >= 0:
                self.cb_model.setCurrentIndex(pos)
            else:
                self.cb_model.setEditText(current)

    def _apply_preset(self):
        """切换服务商时，把地址/模型/是否要 Key 一起带过去。"""
        name, prov, base, models, desc = PRESETS[self.cb_preset.currentIndex()]
        self.ed_base.setText(base)
        self._fill_models(models[0])

        is_ollama = prov == llm_client.PROVIDER_OLLAMA
        self.ed_key.setEnabled(not is_ollama)
        self.chk_show.setEnabled(not is_ollama)
        self.chk_think.setVisible(not is_ollama)
        if is_ollama:
            self.ed_key.setPlaceholderText("本地模型不需要 Key")
        else:
            self.ed_key.setPlaceholderText("sk-…（在 DeepSeek 控制台创建）")
        self.lbl_hint.setText(desc)
        self.lbl_status.clear()

    def _toggle_show(self, on: bool):
        self.ed_key.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)

    # ---------- 收集参数 ----------
    def _params(self) -> dict:
        prov = PRESETS[self.cb_preset.currentIndex()][1]
        model = self.cb_model.currentText().strip()
        base = self.ed_base.text().strip()
        return {
            "provider": prov,
            "api_key": self.ed_key.text().strip(),
            "model": model,
            "base_url": base,
            "thinking": self.chk_think.isChecked(),
            "reply_style": self.cb_len.currentData(),
        }

    # ---------- 测试 ----------
    def _test(self):
        p = self._params()
        if p["provider"] != llm_client.PROVIDER_OLLAMA and not p["api_key"]:
            self._say("还没填 API Key", ok=False)
            return
        if not p["model"]:
            self._say("模型名不能为空", ok=False)
            return

        self.btn_test.setEnabled(False)
        self._say("正在连接…", ok=None)

        kw = {
            "provider": p["provider"],
            "model": p["model"],
            "api_key": p["api_key"],
            "base_url": p["base_url"] or llm_client.DEEPSEEK_BASE_URL,
            "thinking": p["thinking"],
        }
        if p["provider"] == llm_client.PROVIDER_OLLAMA:
            kw["host"] = p["base_url"] or llm_client.DEFAULT_HOST

        self._worker = _TestWorker(kw)
        self._worker.ok.connect(lambda t: self._test_done(True, t))
        self._worker.bad.connect(lambda e: self._test_done(False, e))
        self._worker.start()

    def _test_done(self, ok: bool, text: str):
        self.btn_test.setEnabled(True)
        if ok:
            self._say(f"连接成功，模型回了：{text}", ok=True)
        else:
            self._say(text, ok=False)

    def _say(self, text: str, ok):
        color = "#666" if ok is None else ("#137333" if ok else "#b3261e")
        self.lbl_status.setStyleSheet(f"color:{color};")
        self.lbl_status.setText(text)

    # ---------- 保存 ----------
    def _save(self):
        p = self._params()
        if p["provider"] != llm_client.PROVIDER_OLLAMA and not p["api_key"]:
            self._say("API Key 是空的，填一个再保存（或改选本地 Ollama）", ok=False)
            return
        if not p["model"]:
            self._say("模型名不能为空", ok=False)
            return

        self.cfg.update({
            "ai_provider": p["provider"],
            "ai_api_key": p["api_key"],
            "ai_model": p["model"],
            "ai_base_url": p["base_url"] or llm_client.DEEPSEEK_BASE_URL,
            "ai_thinking": p["thinking"],
            "ai_reply_style": p["reply_style"] or llm_client.DEFAULT_REPLY_STYLE,
        })
        if p["provider"] == llm_client.PROVIDER_OLLAMA:
            self.cfg["model"] = p["model"]
        config_store.save(self.cfg)
        self.accept()


def ensure_ready(parent, cfg: dict) -> bool:
    """聊天前的检查：没配 Key 就弹设置框。返回是否可以开聊（cfg 会被就地更新）。"""
    prov = cfg.get("ai_provider", llm_client.PROVIDER_DEEPSEEK)
    if prov == llm_client.PROVIDER_OLLAMA or cfg.get("ai_api_key", "").strip():
        return True
    dlg = AISettingsDialog(cfg, parent)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        cfg.update(dlg.cfg)
        return bool(cfg.get("ai_api_key", "").strip()) or \
            cfg.get("ai_provider") == llm_client.PROVIDER_OLLAMA
    return False
