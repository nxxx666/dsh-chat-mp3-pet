# -*- coding: utf-8 -*-
"""AI 设置功能实测：默认值 / 对话框 / 保存 / 校验 / 错误提示。"""

import os
import sys

os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")
sys.path.insert(0, os.path.abspath("."))

from PyQt6.QtWidgets import QApplication, QDialog   # noqa: E402

import ai_settings       # noqa: E402
import config_store      # noqa: E402
import llm_client        # noqa: E402

app = QApplication(sys.argv)
ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print(f"[{'OK ' if cond else 'FAIL'}] {name}{(' | ' + extra) if extra else ''}",
          flush=True)


# ---------- 1. 默认配置 ----------
d = config_store.DEFAULTS
check("默认后端是 deepseek", d["ai_provider"] == "deepseek", d["ai_provider"])
check("默认模型是最新 deepseek-flash", d["ai_model"] == "deepseek-flash",
      d["ai_model"])
check("默认地址正确", d["ai_base_url"] == "https://api.deepseek.com",
      d["ai_base_url"])
check("默认无 key", d["ai_api_key"] == "", repr(d["ai_api_key"]))
check("默认关闭思考模式", d["ai_thinking"] is False)
check("默认回复长度=正常", d["ai_reply_style"] == "normal", d["ai_reply_style"])

# ---------- 2. 对话框构建 / 预设切换 ----------
cfg = dict(d)
cfg["ai_api_key"] = "sk-test-1234567890abcdef"
dlg = ai_settings.AISettingsDialog(cfg)
dlg.show()
check("对话框标题", dlg.windowTitle() == "AI 设置")
check("预设读回 deepseek", dlg.cb_preset.currentIndex() == 0,
      dlg.cb_preset.currentText())
check("Key 回显的是密码", dlg.ed_key.echoMode() == dlg.ed_key.EchoMode.Password)
check("模型下拉含两个官方模型",
      [dlg.cb_model.itemText(i) for i in range(dlg.cb_model.count())]
      == ["deepseek-flash", "deepseek-v4-pro"])
check("回复长度下拉含三档", dlg.cb_len.count() == 3,
      str([dlg.cb_len.itemText(i) for i in range(dlg.cb_len.count())]))
check("回复长度回显配置", dlg.cb_len.currentData() == "normal",
      dlg.cb_len.currentData())

# 切到本地 Ollama：Key 应禁用、地址变成本机
dlg.cb_preset.setCurrentIndex(2)
app.processEvents()
check("切 Ollama 后 Key 禁用", not dlg.ed_key.isEnabled())
check("切 Ollama 后地址本机", dlg.ed_base.text() == llm_client.DEFAULT_HOST,
      dlg.ed_base.text())
check("切 Ollama 后思考模式隐藏", not dlg.chk_think.isVisible())

# 切回 DeepSeek
dlg.cb_preset.setCurrentIndex(0)
app.processEvents()
check("切回 DeepSeek 后 Key 可用", dlg.ed_key.isEnabled())
check("切回后地址恢复", dlg.ed_base.text() == llm_client.DEEPSEEK_BASE_URL)

dlg.chk_show.setChecked(True)
check("勾选显示后明文", dlg.ed_key.echoMode() == dlg.ed_key.EchoMode.Normal)
dlg.chk_show.setChecked(False)

# 截图留档（看排版）
shot = os.path.abspath("live2d/_ai_settings.png")
dlg.grab().save(shot)
print("   截图 ->", shot, flush=True)

# ---------- 3. 保存写盘（用临时配置文件，不动真实 config.json） ----------
real_path = config_store.CONFIG_PATH
tmp_path = os.path.abspath("_test_config_ai.json")


def _rm(p):
    """沙箱里删除可能被拦，清不掉就留着，不影响测试结论。"""
    try:
        os.remove(p)
    except OSError:
        pass


_rm(tmp_path)
config_store.CONFIG_PATH = tmp_path
try:
    dlg2 = ai_settings.AISettingsDialog(dict(config_store.DEFAULTS))
    dlg2.ed_key.setText("  sk-abcdef123456  ")     # 故意带空格
    dlg2.cb_model.setEditText("deepseek-v4-pro")
    dlg2.ed_base.setText("https://api.deepseek.com/")   # 故意带尾斜杠
    dlg2.chk_think.setChecked(True)
    dlg2._save()
    saved = config_store.load()
    check("保存后对话框被接受", dlg2.result() == QDialog.DialogCode.Accepted)
    check("Key 已去空格", saved["ai_api_key"] == "sk-abcdef123456",
          repr(saved["ai_api_key"]))
    check("模型已保存", saved["ai_model"] == "deepseek-v4-pro", saved["ai_model"])
    check("思考模式已保存", saved["ai_thinking"] is True)

    # 空 Key 不许保存
    dlg3 = ai_settings.AISettingsDialog(dict(config_store.DEFAULTS))
    dlg3.ed_key.setText("")
    dlg3._save()
    check("空 Key 被拦下", dlg3.result() != QDialog.DialogCode.Accepted)
    check("并给出提示", "API Key" in dlg3.lbl_status.text(),
          dlg3.lbl_status.text())
finally:
    config_store.CONFIG_PATH = real_path
    _rm(tmp_path)

# ---------- 4. ensure_ready 逻辑 ----------
ready_cfg = dict(d, ai_api_key="sk-x")
check("有 Key 时放行", ai_settings.ensure_ready(None, ready_cfg) is True)
ollama_cfg = dict(d, ai_provider="ollama", ai_api_key="")
check("Ollama 模式无需 Key", ai_settings.ensure_ready(None, ollama_cfg) is True)

# ---------- 5. 无 Key 的报错提示 ----------
try:
    list(llm_client.chat_stream([{"role": "user", "content": "hi"}],
                                provider="deepseek", api_key=""))
    check("无 Key 应报错", False)
except llm_client.LLMError as exc:
    check("无 Key 报错可读", "API Key" in str(exc), str(exc))

print("\nRESULT:", "ALL PASS" if ok_all else "HAS FAILURE", flush=True)
dlg.close()
sys.exit(0 if ok_all else 1)
