"""大模型通信层（DeepSeek 云端 / 本地 Ollama）。

只做一件事：把对话消息发给模型，流式取回文本。不依赖任何 UI 代码。

默认后端是 DeepSeek 云端（OpenAI 兼容格式），需要在「AI 设置」里填 API Key；
也可以切回本地 Ollama，不需要联网和密钥。
"""

import json
from typing import Dict, Iterator, List

import requests

# ---------- DeepSeek ----------
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
# 官方 2026-09 起的主力模型，deepseek-chat / deepseek-reasoner 已下线
DEEPSEEK_MODEL = "deepseek-flash"
DEEPSEEK_MODELS = ("deepseek-flash", "deepseek-v4-pro")

# ---------- 本地 Ollama（可选后端）----------
DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen2.5:3b"

PROVIDER_DEEPSEEK = "deepseek"
PROVIDER_OLLAMA = "ollama"
PROVIDER_CUSTOM = "custom"          # 任意 OpenAI 兼容服务（中转站、硅基流动等）

# 回复长度档位：max_tokens 是硬上限（1 token ≈ 1.5 个汉字），
# hint 是追加给人设的长度指令——人设文件里往往写着「50 字以内」，得覆盖掉
REPLY_STYLES = {
    "short": {
        "label": "简短（一句话说完）",
        "max_tokens": 800,
        "hint": "【回复长度】保持简短，一般一句话说完，50 字以内。",
    },
    "normal": {
        "label": "正常（约 200 字）",
        "max_tokens": 4096,
        "hint": ("【回复长度】人设里若有「50 字以内 / 最多两句话」这类字数限制，"
                 "一律按本条执行：把话讲完整，一般 200 字以内就够了；"
                 "用户要求详细时可以更长。"),
    },
    "long": {
        "label": "详细（不限长度）",
        "max_tokens": 8192,
        "hint": ("【回复长度】忽略人设里任何字数限制。该说多少说多少，"
                 "复杂问题可以分点、分段讲清楚，但不要重复和灌水。"),
    },
}
DEFAULT_REPLY_STYLE = "normal"


def style_of(name: str) -> dict:
    """取档位配置，非法名字退回默认。"""
    return REPLY_STYLES.get(name or "", REPLY_STYLES[DEFAULT_REPLY_STYLE])


class LLMError(Exception):
    """模型不可用或返回异常时抛出（消息已转成中文、可直接展示给用户）。"""


# 旧名字保留，避免其它脚本 import 时报错
OllamaError = LLMError


# ============================================================
# 本地 Ollama
# ============================================================
def is_available(host: str = DEFAULT_HOST, timeout: float = 2.0) -> bool:
    """探测 Ollama 服务是否在线。"""
    try:
        r = requests.get(f"{host}/api/tags", timeout=timeout)
        return r.status_code == 200
    except requests.RequestException:
        return False


def list_models(host: str = DEFAULT_HOST, timeout: float = 5.0) -> List[str]:
    """列出本地已下载的 Ollama 模型名。"""
    try:
        r = requests.get(f"{host}/api/tags", timeout=timeout)
        r.raise_for_status()
        data = r.json()
        return [m.get("name", "") for m in data.get("models", [])]
    except requests.RequestException as exc:
        raise LLMError(f"无法读取模型列表：{exc}") from exc


def _ollama_stream(messages, model, host, temperature, max_tokens=4096) -> Iterator[str]:
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {
            "temperature": temperature,
            "num_ctx": 8192,
            "num_predict": max_tokens,
        },
    }
    try:
        resp = requests.post(f"{host}/api/chat", json=payload, stream=True,
                             timeout=(5, 300))
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise LLMError(f"请求 Ollama 失败：{exc}") from exc

    # Ollama 的流式响应是「每行一个 JSON」
    resp.encoding = "utf-8"      # 服务端不回 charset 时 requests 会猜成 latin-1
    for raw_line in resp.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        try:
            chunk = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if chunk.get("error"):
            raise LLMError(str(chunk["error"]))
        piece = chunk.get("message", {}).get("content", "")
        if piece:
            yield piece
        if chunk.get("done"):
            break


# ============================================================
# OpenAI 兼容格式（DeepSeek / 自定义中转）
# ============================================================
def _http_error_text(code: int, body: str) -> str:
    """把服务端错误翻译成用户能看懂的一句话。"""
    detail = ""
    try:
        obj = json.loads(body)
        err = obj.get("error")
        if isinstance(err, dict):
            detail = err.get("message") or ""
        elif isinstance(err, str):
            detail = err
        elif obj.get("message"):
            detail = str(obj["message"])
    except (json.JSONDecodeError, TypeError):
        detail = (body or "")[:160]

    hints = {
        400: "请求格式有问题",
        401: "API Key 无效，请到「AI 设置」里检查（注意别带多余空格）",
        402: "账户余额不足，去 DeepSeek 控制台充值后再试",
        403: "没有权限访问该模型",
        404: "接口地址或模型名不对",
        422: "请求参数不被接受",
        429: "请求太频繁，被限速了，等几秒再试",
        500: "服务端出错了，稍后重试",
        502: "网关错误，稍后重试",
        503: "服务暂时不可用，稍后重试",
    }
    tip = hints.get(code)
    if not tip:
        tip = "服务端返回错误" if code >= 500 else "请求失败"
    return f"{tip}（HTTP {code}）" + (f"：{detail}" if detail else "")


def _openai_stream(messages, model, api_key, base_url, temperature,
                   thinking: bool, max_tokens: int = 4096) -> Iterator[str]:
    if not api_key:
        raise LLMError("还没填 API Key，右键桌宠 →「AI 设置」填一下就能聊了")

    base = (base_url or DEEPSEEK_BASE_URL).rstrip("/")
    if base.endswith("/chat/completions"):
        url = base
    else:
        url = f"{base}/chat/completions"

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": int(max_tokens),
    }
    # DeepSeek V4 默认开思考模式；桌宠聊天要的是「快点开口」，默认关掉
    if thinking:
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = "low"
    else:
        payload["thinking"] = {"type": "disabled"}
        payload["temperature"] = temperature

    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, stream=True,
                             timeout=(8, 300))
    except requests.RequestException as exc:
        raise LLMError(f"连不上 {base}：{exc}") from exc

    if resp.status_code != 200:
        raise LLMError(_http_error_text(resp.status_code, resp.text))

    # SSE：每行 "data: {...}"，结尾 "data: [DONE]"
    resp.encoding = "utf-8"      # event-stream 不带 charset 时 requests 会猜成 latin-1
    for raw_line in resp.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        line = raw_line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            if line == "[DONE]":
                break
            continue
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue

        if chunk.get("error"):
            err = chunk["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise LLMError(f"模型返回错误：{msg}")

        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        # 只取正文。开了思考模式时，思考过程在 delta.reasoning_content 里，
        # 那是给调试看的内心独白，绝不能念进气泡——这里故意不取。
        piece = delta.get("content") or ""
        if piece:
            yield piece


# ============================================================
# 统一入口
# ============================================================
def chat_stream(
    messages: List[Dict[str, str]],
    model: str = DEEPSEEK_MODEL,
    host: str = DEFAULT_HOST,
    temperature: float = 0.8,
    num_ctx: int = 8192,
    provider: str = PROVIDER_DEEPSEEK,
    api_key: str = "",
    base_url: str = DEEPSEEK_BASE_URL,
    thinking: bool = False,
    max_tokens: int = 4096,
) -> Iterator[str]:
    """流式对话。逐块 yield 增量文本，阻塞式生成器。

    messages 格式：[{"role": "system"|"user"|"assistant", "content": "..."}]
    provider: "deepseek" / "custom" 走 OpenAI 兼容接口；"ollama" 走本地。
    max_tokens: 单次回复的 token 上限（中文约 1.5 字/token）。
    """
    if provider == PROVIDER_OLLAMA:
        yield from _ollama_stream(messages, model or DEFAULT_MODEL, host,
                                  temperature, max_tokens)
    else:
        yield from _openai_stream(messages, model or DEEPSEEK_MODEL, api_key,
                                  base_url, temperature, thinking, max_tokens)


def chat_once(messages: List[Dict[str, str]], timeout: float = 25.0, **kw) -> str:
    """一次性取完整回复（用于「测试连接」这类短请求）。"""
    buf = []
    for piece in chat_stream(messages, **kw):
        buf.append(piece)
        if sum(len(x) for x in buf) > 2000:
            break
    return "".join(buf)


# ---------- 单独调试用 ----------
if __name__ == "__main__":
    import os

    key = os.environ.get("DEEPSEEK_API_KEY", "")
    print("provider = deepseek, key =", "已设置" if key else "未设置")
    if key:
        print("\n--- 试一句 ---")
        for tok in chat_stream([{"role": "user", "content": "用一句话介绍你自己"}],
                               api_key=key):
            print(tok, end="", flush=True)
        print()
    else:
        print("设置 DEEPSEEK_API_KEY 环境变量后可试跑")
