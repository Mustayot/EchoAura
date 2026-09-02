# -*- coding: utf-8 -*-
"""记忆门面（MemoryManager）：提取 → 存储 → 召回 → 注入上下文，一站式封装。

供 app_server / llm_client 使用，屏蔽底层 store/extractor 细节。
核心方法：
- ingest(text):          把一句话交给提取器（规则 + 可选 LLM），写入存储
- ingest_async(text):    后台线程异步 ingest（不阻塞 TTS / UI / SSE）
- recall(text, top_k):   BM25 召回 top-k 记忆（累计 hits）
- top_important(n):      按重要度取前 n 条（注入 system prompt 用）
- format_system(mems):   格式化"精选记忆"区块（system prompt）
- format_user(mems):     格式化"本次召回"区块（当前消息前）
- 管理：list/delete/clear/count
"""
import threading

import config
from memory_store import MemoryStore, tokenize
from memory_extractor import (extract_rules, extract_with_llm, has_signal,
                              _looks_like_question)


class MemoryManager:
    def __init__(self, db_path: str = "", max_entries: int = 0,
                 history_keep: int = 0, llm_client=None, model: str = ""):
        self.db_path = db_path or getattr(config, "MEMORY_DB_PATH", "memory.db")
        self.max_entries = max_entries or getattr(config, "MEM_MAX_ENTRIES", 1500)
        self.history_keep = history_keep or getattr(config, "MEM_HISTORY_KEEP", 5)
        self.llm_client = llm_client
        self.model = model or getattr(config, "LM_MODEL", "")
        self.base_url = getattr(config, "LM_STUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
        self.enabled = bool(getattr(config, "MEMORY_ENABLED", True))
        self.llm_extract = bool(getattr(config, "MEM_LLM_EXTRACT", False))
        self.extract_every = max(1, int(getattr(config, "MEM_LLM_EXTRACT_EVERY", 10)))
        self._turn_count = 0
        self.store = MemoryStore(self.db_path, self.max_entries, self.history_keep)
        if not self.store.enabled:
            self.enabled = False
        else:
            # 启动时自动清理历史遗留的提问残渣记忆（3B 模型早期版本的垃圾）
            try:
                self.store.cleanup_garbage()
            except Exception:
                pass

    # ---------- 提取 + 写入 ----------
    def ingest(self, text: str) -> int:
        """同步提取并写入，返回写入条数。任何异常静默返回 0（零侵入）。"""
        if not self.enabled or not text:
            return 0
        try:
            items = extract_rules(text)
            written = 0
            for it in items:
                if self.store.add(it["category"], it["key"], it["value"],
                                  it["importance"], it["kind"]) != -1:
                    written += 1
            self._turn_count += 1
            if self.llm_extract and not _looks_like_question(text):
                trigger = (self._turn_count % self.extract_every == 0) or has_signal(text)
                if trigger:
                    llm_items = extract_with_llm(text, self.llm_client,
                                                 self.model, self.base_url)
                    for it in llm_items:
                        if self.store.add(it["category"], it["key"], it["value"],
                                          it["importance"], it["kind"]) != -1:
                            written += 1
            return written
        except Exception as e:  # noqa: BLE001
            print(f"[记忆] 提取失败（静默）：{e}")
            return 0

    def ingest_async(self, text: str):
        """后台线程异步提取写入，不阻塞主流程。"""
        if not self.enabled or not text:
            return
        threading.Thread(target=self.ingest, args=(text,), daemon=True).start()

    # ---------- 召回 ----------
    def recall(self, text: str, top_k: int = 0) -> list[dict]:
        if not self.enabled:
            return []
        top_k = top_k or getattr(config, "MEM_RETRIEVE_TOP_K", 5)
        try:
            return self.store.search(text, top_k)
        except Exception:
            return []

    def top_important(self, n: int = 0) -> list[dict]:
        if not self.enabled:
            return []
        n = n or getattr(config, "MEM_SYSTEM_MEMORIES", 3)
        try:
            return self.store.all(limit=n)
        except Exception:
            return []
    @staticmethod
    def _fmt_history(hist: list) -> str:
        """把历史值拼成 '（此前: 咖啡、茶）'；无历史返回空串。"""
        if not hist:
            return ""
        old = "、".join(str(h.get("value", "")) for h in hist if h.get("value"))
        return f"（此前: {old}）" if old else ""

    @staticmethod
    def _fmt_time(iso: str) -> str:
        """把 ISO 时间戳格式化为 '8月21日 12:05'（用于行为/动态记忆的时间点展示）。"""
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(iso)
            return f"{dt.month}月{dt.day}日 {dt.strftime('%H:%M')}"
        except Exception:
            return ""

    def _fmt_entry(self, m: dict, with_time: bool) -> str:
        key = m.get("key") or m.get("category", "note")
        label = f"{key}：" if key else ""
        value = m.get("value", "")
        if with_time and m.get("category") == "event":
            t = self._fmt_time(m.get("created_at", ""))
            # 行为动态带时间点："8月21日 12:05 你说过：做了个程序"
            return f"- {t} 你说过：{value}" if t else f"- 你说过：{value}"
        return f"- {label}{value}{self._fmt_history(m.get('history', []))}"

    def format_user(self, mems: list[dict]) -> str:
        if not mems:
            return ""
        lines = ["[关于用户的记忆]"]
        for m in mems:
            lines.append(self._fmt_entry(m, with_time=True))
        return "\n".join(lines)

    def format_system(self, mems: list[dict]) -> str:
        if not mems:
            return ""
        lines = ["长期记住的用户信息（对话中自然引用，不要生硬罗列）："]
        for m in mems:
            lines.append(self._fmt_entry(m, with_time=True))
        return "\n".join(lines)

    # ---------- 管理 ----------
    def list(self, limit: int = 500) -> list[dict]:
        return self.store.all(limit) if self.enabled else []

    def delete(self, mem_id: int) -> bool:
        return self.store.delete(mem_id) if self.enabled else False

    def clear(self) -> int:
        return self.store.clear() if self.enabled else 0

    def count(self) -> int:
        return self.store.count() if self.enabled else 0

    def set_enabled(self, flag: bool):
        self.enabled = bool(flag)

    def close(self):
        self.store.close()
