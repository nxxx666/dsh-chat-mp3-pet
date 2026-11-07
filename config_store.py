"""用户配置持久化：记住窗口位置、音量、播放模式。

配置文件存在 desktop-pet/config.json，纯 JSON，删除即重置。
"""

import json
import os

import paths

# 打包后 config.json 放在 exe 旁边（要能写）
CONFIG_PATH = os.path.join(paths.base_dir(), "config.json")

DEFAULTS = {
    "x": None,            # 窗口位置（None = 用默认右下角）
    "y": None,
    "volume": 0.65,       # 音量 0~1
    "shuffle": False,
    "loop_one": False,
    "persona": None,      # 当前人设名（对应 personas/*.md）
    "anim_pose": "idle",  # 待机动作（对应 assets/anim/ 下的子目录名）
    "pet_scale": 1.0,     # 桌宠大小倍率（0.5 ~ 2.0，右键菜单或滚轮调）

    # ---- AI（默认 DeepSeek 云端）----
    "ai_provider": "deepseek",   # deepseek / custom（OpenAI 兼容中转）/ ollama
    "ai_api_key": "",            # DeepSeek API Key，只存本机 config.json
    "ai_model": "deepseek-flash",  # deepseek-flash（快）/ deepseek-v4-pro（强）
    "ai_base_url": "https://api.deepseek.com",
    "ai_thinking": False,        # 思考模式：开着更聪明但明显更慢
    "ai_reply_style": "normal",  # 回复长度：short / normal / long（覆盖人设里的字数限制）
    "model": "qwen2.5:3b",       # 切回本地 Ollama 时用的模型名

    # ---- 长期记忆（memory.db）----
    "memory_enabled": True,      # 关掉就完全不落库、不召回
    "memory_recall": 3,          # 每次提问主动回忆几条旧片段（0 = 只存不用）
    "memory_summary": True,      # 历史被挤出上下文时，是否调模型压成摘要

    # ---- DSH 模式（把话交给 DeepSeek Harness 的会话）----
    "ai_mode": "standalone",     # standalone = 桌宠自己调模型；dsh = 用 DSH 的会话/记忆/工具
    "dsh_session_id": "",        # 上次聊的那个 DSH 会话，下次接着用
    "dsh_exe_path": "",          # DSH 桌面版 exe 位置；留空则按常见位置自动找（用于自动拉起 DSH）
}


def load() -> dict:
    data = dict(DEFAULTS)
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass  # 配置坏了就用默认值，不让用户处理
    return data


def save(cfg: dict):
    """只覆盖已知键，写回磁盘。失败静默（配置丢了也不该崩）。"""
    clean = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
