"""桌宠启动入口。

默认走 DeepSeek 云端：检查配置里有没有 API Key，没有就弹设置框让人填。
如果配置里切换成了本地 Ollama，则沿用原来的「服务在线 / 模型已下载」检查。

用法：
    python main.py
    python main.py --model deepseek-v4-pro      # 临时换模型
"""

import argparse
import os
import sys

from PyQt6.QtWidgets import (QApplication, QMessageBox, QSystemTrayIcon,
                             QMenu)
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor

import ai_settings
import config_store
import trace
import llm_client
from pet_window import PetWindow

HERE = os.path.dirname(os.path.abspath(__file__))


def make_tray_icon() -> QIcon:
    """用代码画个托盘图标，省得准备 ico 文件。"""
    pm = QPixmap(64, 64)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor("#2c4a72"))
    p.setBrush(QColor("#6f9fe0"))
    p.drawEllipse(6, 6, 52, 52)
    p.setBrush(QColor("#1e2f45"))
    p.drawEllipse(22, 26, 7, 9)
    p.drawEllipse(36, 26, 7, 9)
    p.end()
    return QIcon(pm)


def check_ollama(model: str) -> bool:
    """本地 Ollama 后端的启动前检查（保持原有行为）。"""
    if not llm_client.is_available(llm_client.DEFAULT_HOST):
        QMessageBox.critical(
            None, "Ollama 没有响应",
            f"连不上 {llm_client.DEFAULT_HOST}\n\n"
            "请先在终端执行：\n"
            "    ollama serve\n\n"
            "如果还没装 Ollama，去 https://ollama.com/download 下载安装。\n"
            "（也可以在右键菜单 →「AI 设置」里切回 DeepSeek 云端）"
        )
        return False
    try:
        models = llm_client.list_models()
    except llm_client.LLMError as exc:
        QMessageBox.critical(None, "读取模型失败", str(exc))
        return False
    if not any(m.startswith(model) for m in models):
        QMessageBox.warning(
            None, "模型还没下载",
            f"本地找不到模型 {model}\n\n"
            f"当前已有：{', '.join(models) if models else '（空）'}\n\n"
            f"请在终端执行：\n    ollama pull {model}"
        )
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="AI 桌宠（DeepSeek / 本地 Ollama）")
    parser.add_argument("--model", default=None,
                        help="临时覆盖模型名（不写入配置）")
    parser.add_argument("--host", default=None,
                        help="临时覆盖接口地址（Ollama 或 OpenAI 兼容中转）")
    parser.add_argument("--skip-check", action="store_true", help="跳过启动检查")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # 关掉气泡不退出

    if not args.skip_check:
        cfg = config_store.load()
        prov = cfg.get("ai_provider") or llm_client.PROVIDER_DEEPSEEK
        model = args.model or cfg.get("ai_model") or llm_client.DEEPSEEK_MODEL

        if prov == llm_client.PROVIDER_OLLAMA:
            if not check_ollama(model):
                return 1
        # DeepSeek 模式的 Key 检查在 run_app 里统一做（打包入口不走这里）

    return run_app(args.model, args.host, app)


def _install_qt_log():
    """把 Qt 自己的日志（尤其是 QtFatalMsg）也写进行为轨迹。

    Qt 的 qFatal 会直接 abort 进程（Windows 上表现为 0xc0000409 闪退），
    Python 的 excepthook 完全看不到。但 abort 之前 Qt 会先调 message handler，
    所以这里能抓到「它到底为什么自杀」。
    """
    try:
        from PyQt6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:
        return
    label = {
        QtMsgType.QtDebugMsg: "debug",
        QtMsgType.QtInfoMsg: "info",
        QtMsgType.QtWarningMsg: "warn",
        QtMsgType.QtCriticalMsg: "critical",
        QtMsgType.QtFatalMsg: "FATAL",
    }

    def handler(mode, _ctx, message):
        try:
            trace.log(f"[qt:{label.get(mode, '?')}] {message}")
        except Exception:
            pass

    qInstallMessageHandler(handler)


def run_app(model: str = None, host: str = None, app: QApplication = None) -> int:
    """真正启动桌宠（打包后由 setup_and_run 直接调用，不再走子进程）。"""
    trace.start()
    _install_qt_log()
    # 崩溃诊断：任何线程未捕获异常 + 段错误栈都写进日志，便于打包版排查
    import faulthandler
    import threading
    log_path = os.path.join(HERE, "_crash.log")
    try:
        _fh = open(log_path, "a", encoding="utf-8", buffering=1)
        faulthandler.enable(_fh)
        sys.stderr = _fh
        sys.stdout = _fh
        _excepthook = threading.excepthook

        def _hook(a):
            import traceback
            _fh.write("\n=== thread exception ===\n")
            traceback.print_exception(a.exc_type, a.exc_value, a.exc_traceback, file=_fh)
            _excepthook(a)

        threading.excepthook = _hook

        # 主线程兜底：Qt 槽里抛出的 Python 异常不会刷屏，但也不该静默消失
        def _main_hook(exc_type, exc_value, exc_tb):
            try:
                import traceback
                _fh.write("\n=== main thread exception ===\n")
                traceback.print_exception(exc_type, exc_value, exc_tb, file=_fh)
                trace.log(f"main exception: {exc_type.__name__}: {exc_value}")
            except Exception:
                pass

        sys.excepthook = _main_hook
        print("\n=== run_app start ===", flush=True)
    except Exception:
        pass

    args_model, args_host = model, host
    app = app or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # 关掉气泡不退出

    # ── 单实例保护 ──────────────────────────────────────────
    # 两个实例同时跑会共用 config.json / memory.db / 托盘，
    # 造成配置互相覆盖和 SQLite 争锁（实测发生过）。
    from PyQt6.QtCore import QLockFile
    import tempfile
    _lock = QLockFile(os.path.join(tempfile.gettempdir(), "pet_feiyu.lock"))
    if not _lock.tryLock():
        QMessageBox.warning(None, "桌宠已经在运行",
                            "桌宠大肥鱼已经在运行了（托盘里找找看），\n"
                            "不需要再开第二个。")
        return 2

    # ── 主线程心跳 + 看门狗 ─────────────────────────────────
    # 卡死（「未响应」）往往发生在 Qt/GL 原生层，异常钩子看不到；
    # 看门狗会在卡死瞬间把全线程 Python 栈写进 _trace.log。
    from PyQt6.QtCore import QTimer
    _beat = QTimer(app)
    _beat.setInterval(400)
    _beat.timeout.connect(trace.touch)
    _beat.start()
    trace.start_watchdog()

    # 首次使用 DeepSeek：没有 Key 就先把设置框摆出来（打包入口 setup_and_run
    # 直接调这里、不走 main()，所以检查必须放这一层）
    cfg = config_store.load()
    prov = cfg.get("ai_provider") or llm_client.PROVIDER_DEEPSEEK
    if (prov != llm_client.PROVIDER_OLLAMA
            and not (cfg.get("ai_api_key") or "").strip()):
        dlg = ai_settings.AISettingsDialog(cfg)
        dlg.exec()   # 取消也照常启动，之后右键 → AI 设置随时补

    pet = PetWindow(model=args_model, host=args_host)
    pet.show()

    # 系统托盘
    tray = QSystemTrayIcon(make_tray_icon(), app)
    tray.setToolTip(f"{pet.persona_name} · 右键桌宠可打开菜单")
    menu = QMenu()
    menu.addAction("显示 / 隐藏", lambda: pet.setVisible(not pet.isVisible()))
    menu.addAction("聊聊天", pet.start_chat)
    menu.addSeparator()
    menu.addAction("播放 / 暂停音乐", pet.music.play_pause)
    menu.addAction("下一首", pet.music.next)
    menu.addAction("上一首", pet.music.prev)
    menu.addSeparator()
    menu.addAction("AI 设置…", pet.open_ai_settings)
    menu.addAction("清空记忆", pet.clear_history)
    menu.addAction("退出", pet.quit_app)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: pet.start_chat()
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None
    )
    tray.show()
    app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
