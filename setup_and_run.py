"""一键准备并启动桌宠（打包入口）。

默认后端是 DeepSeek 云端，不需要本地装任何东西：
1. 读配置，看看 AI 后端是云端还是本地
2. 云端：没填 API Key 就提示去设置；本地：检查 Ollama 服务与模型
3. 启动桌宠

用法：python setup_and_run.py
"""

import os
import subprocess
import sys
import time
import webbrowser

import requests

HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:3b"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable


class CmdResult:
    """subprocess.run 的轻量替身，用于命令不存在时返回失败而非抛异常。"""
    returncode = 1
    stdout = ""

    def __init__(self, code=1, out=""):
        self.returncode, self.stdout = code, out


def run(cmd, **kw):
    """命令不存在（如没装 Ollama）时返回失败结果，不抛 FileNotFoundError。"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="ignore", **kw)
    except (FileNotFoundError, OSError):
        return CmdResult()


def step(n, total, msg):
    print(f"\n[{n}/{total}] {msg}", flush=True)


def ollama_installed() -> bool:
    r = run(["ollama", "--version"])
    return r.returncode == 0


def service_up() -> bool:
    try:
        return requests.get(f"{HOST}/api/tags", timeout=2).status_code == 200
    except requests.RequestException:
        return False


def start_service() -> bool:
    """后台拉起 ollama serve，最多等 20 秒。"""
    creationflags = 0
    startupinfo = None
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
    except (FileNotFoundError, OSError):
        return False
    for _ in range(20):
        time.sleep(1)
        if service_up():
            return True
    return False


def model_exists(name: str) -> bool:
    try:
        r = requests.get(f"{HOST}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m.get("name", "") for m in r.json().get("models", [])]
        return any(m.startswith(name) for m in models)
    except requests.RequestException:
        return False


def load_cfg() -> dict:
    try:
        import config_store
        return config_store.load()
    except Exception:
        return {}


def launch(model=None, host=None):
    """启动桌宠本体（打包后 sys.executable 就是自己，不能再起子进程）。"""
    if getattr(sys, "frozen", False):
        import main as main_module
        main_module.run_app(model, host)
        return 0
    args = [PY, os.path.join(HERE, "main.py")]
    if model:
        args += ["--model", model]
    if host:
        args += ["--host", host]
    return subprocess.run(args, cwd=HERE).returncode


def prepare_ollama() -> int:
    total = 4
    # 1. 检查 Ollama
    step(1, total, "检查 Ollama 是否已安装…")
    if not ollama_installed():
        print("   [×] 没检测到 Ollama", flush=True)
        print("   正在打开下载页：https://ollama.com/download", flush=True)
        print("   安装完 Ollama 后，重新运行本程序即可。", flush=True)
        print("   （也可以右键桌宠 →「AI 设置」切回 DeepSeek 云端）", flush=True)
        webbrowser.open("https://ollama.com/download")
        return 1
    print("   [√] Ollama 已安装", flush=True)

    # 2. 检查/启动服务
    step(2, total, "检查 Ollama 服务…")
    if service_up():
        print("   [√] 服务已在运行", flush=True)
    else:
        print("   服务未运行，正在启动 ollama serve …", flush=True)
        if not start_service():
            print("   [×] 服务启动失败，请手动在终端执行：ollama serve", flush=True)
            return 1
        print("   [√] 服务已启动", flush=True)

    # 3. 检查/下载模型
    step(3, total, f"检查模型 {DEFAULT_MODEL} …")
    if model_exists(DEFAULT_MODEL):
        print(f"   [√] {DEFAULT_MODEL} 已就绪", flush=True)
    else:
        print("   模型未下载，开始拉取（约 1.9GB，视网速几分钟）…", flush=True)
        r = run(["ollama", "pull", DEFAULT_MODEL])
        if r.returncode != 0:
            print("   [×] 下载失败，请手动执行：ollama pull " + DEFAULT_MODEL, flush=True)
            return 1
        print(f"   [√] {DEFAULT_MODEL} 下载完成", flush=True)

    # 4. 启动桌宠
    step(4, total, "启动桌宠…")
    print("   桌宠窗口将在屏幕右下角出现。", flush=True)
    print("   右键桌宠可聊天、放音乐、调 AI。", flush=True)
    print("=" * 52, flush=True)
    return launch()


def prepare_cloud(cfg: dict) -> int:
    total = 2
    model = cfg.get("ai_model") or "deepseek-flash"
    prov = cfg.get("ai_provider") or "deepseek"

    step(1, total, "检查 AI 设置…")
    if prov == "custom":
        print(f"   [√] 后端：自定义 OpenAI 兼容接口（模型 {model}）", flush=True)
    else:
        print(f"   [√] 后端：DeepSeek 云端（模型 {model}）", flush=True)
    key = (cfg.get("ai_api_key") or "").strip()
    if key:
        print(f"   [√] API Key 已保存（{key[:6]}…{key[-4:]}）", flush=True)
    else:
        print("   [!] 还没填 API Key —— 启动后会自动弹出设置窗口", flush=True)
        print("       也可以右键桌宠 →「AI 设置」填写", flush=True)

    step(2, total, "启动桌宠…")
    print("   桌宠窗口将在屏幕右下角出现。", flush=True)
    print("   右键桌宠可聊天、放音乐、调 AI。", flush=True)
    print("=" * 52, flush=True)
    return launch()


def main():
    print("=" * 52, flush=True)
    print("  桌宠「大肥鱼」启动", flush=True)
    print("=" * 52, flush=True)

    cfg = load_cfg()
    prov = cfg.get("ai_provider") or "deepseek"
    if prov == "ollama":
        return prepare_ollama()
    return prepare_cloud(cfg)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消", flush=True)
