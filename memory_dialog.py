"""记忆查看 / 搜索窗口。

桌宠本体是穿透窗口，没法在气泡里翻列表，所以记忆内容单独开窗口看：
- 上面是当前人设的长期摘要
- 下面是记忆条目，支持关键词搜索（走的就是主动召回那套打分）
"""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                             QLineEdit, QListWidget, QListWidgetItem,
                             QPushButton, QVBoxLayout)

ROLE_LABEL = {"user": "用户", "assistant": "桌宠", "summary": "摘要"}


class MemoryDialog(QDialog):
    """store: memory.MemoryStore；persona: 只显示这个人设的记忆（None = 全部）。"""

    def __init__(self, store, persona: str = "", parent=None):
        super().__init__(parent)
        self.store = store
        self.persona = persona
        self.cleared = False

        self.setWindowTitle(f"记忆库 · {persona or '全部'}")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.resize(560, 520)
        self._font = QFont("Microsoft YaHei", 10)
        self.setFont(self._font)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 12)
        root.setSpacing(9)

        # ---- 摘要 ----
        self.lbl_sum = QLabel("")
        self.lbl_sum.setWordWrap(True)
        self.lbl_sum.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_sum.setStyleSheet(
            "background:#f3f6fb;border:1px solid #d7e0ee;border-radius:6px;"
            "padding:8px;color:#243447;")
        root.addWidget(self.lbl_sum)

        # ---- 搜索 ----
        row = QHBoxLayout()
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜点什么…（例如：生日 / 桌宠 / 咖啡）")
        btn_search = QPushButton("搜索")
        btn_search.setFixedWidth(70)
        btn_all = QPushButton("最近")
        btn_all.setFixedWidth(70)
        row.addWidget(self.ed_search, 1)
        row.addWidget(btn_search)
        row.addWidget(btn_all)
        root.addLayout(row)

        # ---- 列表 ----
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setAlternatingRowColors(True)
        self.list.setStyleSheet("QListWidget{background:#ffffff;border:1px solid #d7e0ee;}")
        root.addWidget(self.list, 1)

        self.lbl_stat = QLabel("")
        self.lbl_stat.setStyleSheet("color:#666;")
        root.addWidget(self.lbl_stat)

        # ---- 按钮 ----
        btns = QDialogButtonBox()
        self.btn_clear = btns.addButton("清空这批记忆",
                                        QDialogButtonBox.ButtonRole.DestructiveRole)
        btns.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        root.addWidget(btns)

        btn_search.clicked.connect(self._search)
        self.ed_search.returnPressed.connect(self._search)
        btn_all.clicked.connect(self._recent)
        self.btn_clear.clicked.connect(self._clear)
        btns.rejected.connect(self.reject)

        self._recent()

    # ---------- 数据 ----------
    def _recent(self):
        self.ed_search.clear()
        items = self.store.recent(80, persona=self.persona)
        self._fill(items, "最近 %d 条" % len(items))

    def _search(self):
        kw = self.ed_search.text().strip()
        if not kw:
            self._recent()
            return
        hits = self.store.search(kw, k=30, persona=self.persona)
        self._fill(hits, f"「{kw}」命中 {len(hits)} 条（按相关度排序）")
        self.ed_search.setText(kw)

    def _fill(self, items, stat: str):
        self.list.clear()
        for it in items:
            role = ROLE_LABEL.get(it["role"], it["role"])
            text = it["content"].replace("\n", " ")
            line = f"{it['time']}  [{role}]  {text}"
            item = QListWidgetItem(line)
            item.setToolTip(it["content"])
            item.setData(Qt.ItemDataRole.UserRole, it)
            self.list.addItem(item)
        total = self.store.count(self.persona)
        self.lbl_stat.setText(f"{stat} · 本记忆库共 {total} 条")
        self.lbl_sum.setText("【长期摘要】" +
                             (self.store.latest_summary(self.persona)
                              or "还没有摘要——聊多一点会自动整理出来"))

    def _clear(self):
        from PyQt6.QtWidgets import QMessageBox
        n = self.store.count(self.persona)
        if n == 0:
            return
        who = f"「{self.persona}」这个" if self.persona else "全部"
        if QMessageBox.question(
                self, "清空记忆",
                f"确定要清空{who}记忆吗？共 {n} 条，删了就找不回来了。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self.store.clear(self.persona)
        self.store.vacuum()
        self.cleared = True
        self._recent()
