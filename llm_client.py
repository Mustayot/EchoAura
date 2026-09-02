# -*- coding: utf-8 -*-
import re
import socket
import httpx
from openai import OpenAI

_EMO_RE = re.compile(r"\[EMO:\s*([A-Za-z0-9_]+)\s*\]", re.IGNORECASE)   # 提取情绪名（必须闭合）
_EMO_OPEN = re.compile(r"\[EM(?=O|$)", re.IGNORECASE)                   # 检测未闭合标签起点（含 [EM 这种刚切出来的残缺）
_EMO_STRIP = re.compile(r"\[EM[^\]]*?\]|\[EM[^\]]*$", re.IGNORECASE)    # 抹掉完整/含]标签；以及串尾残缺 [EM（无]）
_NEUTRAL = "neutral"

# 回复语言 -> 语言名（用于 system prompt 的「Reply in X」指令）。
# 仅保留 GPT-SoVITS 支持的中/英；其它语言一律回落英文。
_LANG_NAME = {
    "en": "English", "zh": "Chinese (Simplified)",
}

# 每条用户消息前附加的「强制语言」标记：作用域离生成点最近，对小模型最有效。
# 只发给 LLM，不进历史、不进 UI。英文无需（模型默认英文）。
_LANG_TAG = {
    "zh": "【重要：你只能用中文回复，绝对不要使用英文】",
}


def lang_instruction(lang: str | None) -> str:
    """system prompt 首条「回复语言」指令（强制式 + 否定约束 + 目标语言锚定）。
    仅 en/zh 受支持；其它/未知语言回落英文。"""
    code = (lang or "en").lower()
    if code == "en":
        return ("Reply in English, naturally and conversationally, "
                "like a real friend, not like a customer service or encyclopedia.")
    if code == "zh":
        return ("Reply in Chinese (Simplified). You MUST write your ENTIRE reply in Chinese, "
                "and you must NOT use English unless the user writes in English. "
                "Keep it conversational, like a real friend.")
    return ("Reply in English, naturally and conversationally, "
            "like a real friend, not like a customer service or encyclopedia.")


def _lang_tag(lang: str | None) -> str:
    """每条用户消息前附加的强制语言标记（仅中文；英文/未知无标记）。"""
    return _LANG_TAG.get((lang or "en").lower(), "")


def _strip_emo(text: str):
    """剥离 [EMO: x] 标签（含不完整）并返回 (clean_text, emotion)。无标签则 emotion=neutral。"""
    emo = _NEUTRAL
    m = _EMO_RE.search(text or "")
    if m:
        emo = m.group(1).lower()
    return _EMO_STRIP.sub("", text or "").strip(), emo

# 对话：连接 5s 报错、生成最长 300s；健康探测：整体 3s 内快速返回
_CHAT_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=60.0, pool=5.0)
_PROBE_TIMEOUT = httpx.Timeout(connect=2.0, read=3.0, write=3.0, pool=2.0)


def _normalize_base_url(base_url: str) -> str:
    """把 localhost 换成 127.0.0.1，避免 Windows 上 IPv6 ::1 黑洞拖慢探测。"""
    return (base_url or "").replace("://localhost", "://127.0.0.1")


def llm_port_alive(base_url: str, timeout: float = 0.3) -> bool:
    """毫秒级 TCP 探测 LLM 端口（状态灯快速判断用，端口通≠服务就绪）。"""
    from urllib.parse import urlparse
    u = urlparse(_normalize_base_url(base_url or ""))
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 80), timeout=timeout):
            return True
    except Exception:
        return False


class LLMClient:
    def __init__(self, base_url: str, model: str,
                 system_prompt: str, temperature: float = 0.7,
                 max_history: int = 20, memory=None,
                 chat_provider=None):
        if not model:
            raise ValueError("config.LM_MODEL 还没有填！请在 LM Studio 或 Ollama 加载模型后，"
                             "把模型名字填到 config.py 里")
        self.client = OpenAI(base_url=_normalize_base_url(base_url), api_key="lm-studio",
                             timeout=_CHAT_TIMEOUT)
        self.model = model
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.max_history = max_history
        self.history: list[dict] = []
        self.last_emotion = _NEUTRAL
        # 长期记忆（MemoryManager 或 None）。None = 功能未启用，完全不影响原流程（零侵入降级）。
        self.memory = memory
        # 聊天记录注入器（callable，返回"历史对话记录"文本块或 None）。None = 不注入。
        # 由 app_server 传入 chat_log.load_for_inject 的闭包；异常静默降级。
        self.chat_provider = chat_provider

    def _build_messages(self, user_text: str, lang: str | None = None) -> list[dict]:
        """组装 system + 历史 + 本条用户消息（保证 system 后首条为 user）。
        lang 指定回复语言：覆盖 system prompt 模板 __LANG_LINE__，并在用户消息前
        附加强制语言标记（离生成点最近、对小模型最有效）。标记只发给 LLM，不进历史。
        长期记忆（A+B 双轨）：
        - A：system prompt 追加"精选长期记忆"（top 重要度）
        - B：当前用户消息前追加"本次召回 top-K"（BM25 相关记忆，含历史值）
        记忆模块异常时静默跳过，不影响正常对话。"""
        self._repair_history()
        hist = self._last_window(self.history)
        while hist and hist[0]["role"] != "user":
            hist = hist[1:]
        system = (self.system_prompt or "").replace("__LANG_LINE__", lang_instruction(lang))
        sent = user_text
        tag = _lang_tag(lang)
        if tag:
            sent = f"{tag}\n{user_text}"
        try:
            mem = self.memory
            if mem is not None and mem.enabled:
                sys_mems = mem.top_important()
                if sys_mems:
                    system += "\n\n" + mem.format_system(sys_mems)
                recalled = mem.recall(user_text)
                if recalled:
                    sent = mem.format_user(recalled) + "\n" + sent
        except Exception as e:  # noqa: BLE001
            print(f"[记忆] 注入失败（静默跳过）：{e}")
        try:
            if self.chat_provider is not None:
                block = self.chat_provider(self.history)
                if block:
                    system += "\n\n" + block
        except Exception as e:  # noqa: BLE001
            print(f"[聊天记录] 注入失败（静默跳过）：{e}")
        return ([{"role": "system", "content": system}]
                + hist + [{"role": "user", "content": sent}])

    def _commit(self, user_text: str, reply: str) -> None:
        """成功后才写历史，避免失败导致连续两条 user（roles must alternate）。"""
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": reply})
        self._repair_history()

    def reply(self, user_text: str, lang: str | None = None) -> str:
        resp = self.client.chat.completions.create(
            model=self.model, messages=self._build_messages(user_text, lang),
            temperature=self.temperature, max_tokens=512)
        reply = (resp.choices[0].message.content or "").strip()
        clean, emo = _strip_emo(reply)
        self.last_emotion = emo
        self._commit(user_text, clean)
        return clean

    def reply_stream(self, user_text: str, lang: str | None = None):
        """流式生成器：逐 chunk yield delta.content，结束后写历史一次。"""
        try:
            stream = self.client.chat.completions.create(
                model=self.model, messages=self._build_messages(user_text, lang),
                temperature=self.temperature, max_tokens=512, stream=True)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"流式请求失败：{e}") from e

        full = []
        emitted = 0
        try:
            for chunk in stream:
                if not chunk.choices:
                    continue
                content = getattr(chunk.choices[0].delta, "content", None)
                if not content:
                    continue
                full.append(content)
                raw = "".join(full)
                # 完整标签出现即记录情绪；按规范应出现在回复最前面，取第一个有效标签
                m = _EMO_RE.search(raw)
                if m:
                    self.last_emotion = m.group(1).lower()
                # 若 raw 末尾存在未闭合的 [EMO...，则只吐到它之前，避免标签碎片进入 UI
                open_at = -1
                for mo in _EMO_OPEN.finditer(raw):
                    if not _EMO_RE.match(raw[mo.start():]):
                        open_at = mo.start()
                safe = raw if open_at < 0 else raw[:open_at]
                safe_clean = _EMO_STRIP.sub("", safe)
                if len(safe_clean) > emitted:
                    yield safe_clean[emitted:]
                    emitted = len(safe_clean)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"流式生成中断：{e}") from e

        raw = "".join(full)
        clean, emo = _strip_emo(raw)
        self.last_emotion = emo
        self._commit(user_text, clean)

    def _last_window(self, messages):
        return messages[-self.max_history:]

    def _repair_history(self):
        """去连续同角色、截断到最近 max_history 条、保证以 user 开头。"""
        clean = []
        for m in self.history:
            if clean and clean[-1]["role"] == m["role"]:
                continue
            clean.append(m)
        clean = self._last_window(clean)
        if clean and clean[0]["role"] != "user":
            clean = clean[1:]
        self.history = clean

    def clear_history(self):
        self.history.clear()

    @staticmethod
    def list_models(base_url: str) -> list[str]:
        try:
            client = OpenAI(base_url=_normalize_base_url(base_url), api_key="lm-studio",
                            timeout=_PROBE_TIMEOUT, max_retries=0)
            return [m.id for m in client.models.list().data]
        except Exception:
            return []
