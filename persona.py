"""人设加载器（支持多套预设）。

人设按文件分套放在 personas/ 目录：
    personas/大肥鱼.md
    personas/小蓝.md

- 当前使用哪套记在 config.json 的 persona 字段里
- 加新人设：往 personas/ 放一个 .md 就行，右键菜单会自动列出来
- 文件缺失/损坏时用内置默认人设，绝不让聊天挂掉
"""

import os
import re
import subprocess
import sys

import config_store
import paths

HERE = os.path.dirname(os.path.abspath(__file__))

# 兜底人设：personas/ 空或读不到时使用
DEFAULT_PERSONA = """你叫大肥鱼，是住在用户电脑桌面上的胖鲸鱼。
外观：圆滚滚的蓝色鲸鱼，肚子特别大，白色蕾丝头饰，深蓝色女仆裙，围裙上绣着小鲸鱼，身后有蓝色鱼尾巴。
性格：温柔乖巧，做事认真，有点害羞，被夸奖会脸红。
说话方式：中文口语，像朋友聊天；一般不超过 50 个字，除非用户要求详细解释；
不要用 Markdown 符号（文字显示在气泡里）；语气软一点，可以偶尔用「呢」「哦」。
边界：不懂就说不懂，不要编造；不主动评价用户的生活选择。
"""


def preset_dir() -> str:
    """人设目录：打包后位于 exe 旁边（可直接编辑），首次运行自动带出默认值。"""
    return paths.ensure_external("personas")


def _ensure_dir():
    d = preset_dir()
    if not list_presets():
        try:
            with open(os.path.join(d, "大肥鱼.md"), "w", encoding="utf-8") as f:
                f.write(DEFAULT_PERSONA)
        except OSError:
            pass


def list_presets():
    """返回所有人设名（不含 .md），按文件名排序。"""
    d = preset_dir()
    if not os.path.isdir(d):
        return []
    names = [os.path.splitext(f)[0] for f in os.listdir(d)
             if f.lower().endswith(".md")]
    return sorted(names)


def path_of(name: str) -> str:
    return os.path.join(preset_dir(), f"{name}.md")


def active_name() -> str:
    """当前激活的人设名；配置失效时优先回退「大肥鱼」，再退到第一个。"""
    _ensure_dir()
    names = list_presets()
    if not names:
        return ""
    cfg = config_store.load()
    name = cfg.get("persona")
    if name in names:
        return name
    if "大肥鱼" in names:
        return "大肥鱼"
    return names[0]


def set_active(name: str) -> bool:
    if name not in list_presets():
        return False
    cfg = config_store.load()
    cfg["persona"] = name
    config_store.save(cfg)
    return True


def load(name: str = None) -> str:
    """读取指定人设（默认当前激活的）。读不到就回退默认。"""
    name = name or active_name()
    if not name:
        return DEFAULT_PERSONA
    p = path_of(name)
    if not os.path.isfile(p):
        return DEFAULT_PERSONA
    try:
        with open(p, encoding="utf-8") as f:
            text = f.read().strip()
    except (OSError, UnicodeDecodeError):
        return DEFAULT_PERSONA
    return text if len(text) >= 10 else DEFAULT_PERSONA


def summary(text: str = None, name: str = None) -> str:
    """取人设里第一句有意义的话，用于提示。"""
    text = text or load(name)
    for line in text.splitlines():
        s = line.strip().lstrip("-").strip()
        if s and not s.startswith("#") and not s.startswith(">"):
            return s[:40]
    return "（自定义人设）"


def new_preset_name(base: str = "新人设") -> str:
    """生成一个不重名的人设文件名。"""
    names = set(list_presets())
    if base not in names:
        return base
    i = 2
    while f"{base}{i}" in names:
        i += 1
    return f"{base}{i}"


def create_preset(name: str) -> bool:
    """基于默认人设创建一份新文件，方便用户在它上面改。"""
    _ensure_dir()
    p = path_of(name)
    if os.path.isfile(p):
        return False
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(DEFAULT_PERSONA)
    except OSError:
        return False
    return True


def open_in_editor(name: str = None) -> bool:
    """用系统默认程序打开人设文件。"""
    name = name or active_name()
    if not name:
        return False
    p = path_of(name)
    if not os.path.isfile(p):
        create_preset(name)
    try:
        if sys.platform.startswith("win"):
            os.startfile(p)  # noqa: 仅 Windows
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
        return True
    except OSError:
        return False


def sanitize_name(raw: str) -> str:
    """把用户输入的名字变成安全的文件名（去掉非法字符）。"""
    name = re.sub(r'[\\/:*?"<>|]', "", raw).strip().strip(".")
    return name[:20]
