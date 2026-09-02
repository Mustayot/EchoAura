# -*- coding: utf-8 -*-
"""长期记忆存储层：SQLite 持久化 + jieba 分词 + 手写 BM25 关键词检索（零/极轻依赖）。
- 存储：stdlib sqlite3，单文件（默认 memory.db，纯本地，无任何联网）。
- 检索：jieba 分词 + 自实现 BM25（仅依赖 numpy 计算，不引入向量库）。
- 冲突：同 (category, key) 的 fact 覆盖主值，旧值推入 history 字段（最多保留 N 条），
  注入上下文时把「当前值 + 历史值」一起给出，让 LLM 能说出"你以前喜欢咖啡，现在喝茶"。
- 遗忘：容量上限 + 重要性淘汰（importance = base + 命中次数加成），保"重要记忆优先"。
- 零侵入降级：任何异常（缺 jieba、DB 损坏等）→ 记 disabled，所有方法安全返回空，
  绝不抛异常打断聊天主流程。
"""
import json
import math
import os
import sqlite3
import threading
import time
from datetime import datetime

try:
    import jieba
    _JIEBA_OK = True
except Exception:  # noqa: BLE001  jieba 缺失时降级为字符 bigram
    _JIEBA_OK = False


# ---------- 分词 ----------
def tokenize(text: str) -> list[str]:
    """分词：jieba 词 + CJK 单字混合（保证词级与字级都能被检索命中）。
    例："喝咖啡" → ['喝咖啡','喝','咖','啡']；查"咖啡"可命中。
    jieba 缺失时降级为字符 bigram（纯 Python，零依赖）。"""
    if not text:
        return []
    text = str(text).lower()
    if _JIEBA_OK:
        words = [w for w in jieba.lcut(text) if w.strip()]
        out = []
        for w in words:
            w = w.strip()
            if not w:
                continue
            out.append(w)
            if any('\u4e00' <= ch <= '\u9fff' for ch in w) and len(w) > 1:
                # 中文词拆单字，保证"喝咖啡" vs 查询"喝什么"也能命中（两边一致）
                out.extend(ch for ch in w if '\u4e00' <= ch <= '\u9fff')
        return out
    # 降级：字符 bigram（对中文可接受，英文按词切分）
    words = []
    for ch in text:
        if ch.isalnum():
            words.append(ch)
        else:
            words.append(" ")
    joined = "".join(words).split()
    out = []
    for w in joined:
        if len(w) == 1:
            out.append(w)
        else:
            out.extend(w[i:i + 2] for i in range(len(w) - 1))
    return [w for w in out if w.strip()]


# ---------- BM25 ----------
class BM25Index:
    """内存倒排索引 + BM25 打分。条目量级几千条，纯 Python 足够快。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.doc_len: dict[int, int] = {}
        self.df: dict[str, int] = {}
        self.postings: dict[str, dict[int, int]] = {}   # term -> {doc_id: tf}
        self.avgdl = 0.0
        self._n = 0

    def add(self, doc_id: int, tokens: list[str]):
        self.remove(doc_id)  # 幂等：先清旧
        freq: dict[str, int] = {}
        for t in tokens:
            freq[t] = freq.get(t, 0) + 1
        self.doc_len[doc_id] = len(tokens)
        for t, tf in freq.items():
            self.postings.setdefault(t, {})[doc_id] = tf
            self.df[t] = len(self.postings[t])
        self._recalc_avgdl()

    def remove(self, doc_id: int):
        if doc_id not in self.doc_len:
            return
        del self.doc_len[doc_id]
        for t, posting in list(self.postings.items()):
            if doc_id in posting:
                del posting[doc_id]
                if not posting:
                    del self.postings[t]
                    self.df.pop(t, None)
                else:
                    self.df[t] = len(posting)
        self._recalc_avgdl()

    def _recalc_avgdl(self):
        n = len(self.doc_len)
        self._n = n
        self.avgdl = (sum(self.doc_len.values()) / n) if n else 0.0

    def score(self, query_tokens: list[str]) -> dict[int, float]:
        """返回 {doc_id: score}，已过滤零分项。"""
        if not query_tokens or self._n == 0:
            return {}
        n = self._n
        scores: dict[int, float] = {}
        for t in set(query_tokens):
            posting = self.postings.get(t)
            if not posting:
                continue
            df_t = len(posting)
            idf = math.log(1 + (n - df_t + 0.5) / (df_t + 0.5))
            for doc_id, tf in posting.items():
                dl = self.doc_len.get(doc_id, 0) or 1
                denom = tf + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * (tf * (self.k1 + 1)) / denom
        return scores


# ---------- SQLite 存储 ----------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    category   TEXT NOT NULL DEFAULT 'note',   -- name/preference/dislike/identity/plan/fact/note
    key        TEXT NOT NULL DEFAULT '',       -- 结构化键（note 无键）
    value      TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'fact',   -- fact（可覆盖）/ note（追加）
    importance REAL NOT NULL DEFAULT 5.0,      -- 1-10，AI/规则给的基础重要度
    hits       INTEGER NOT NULL DEFAULT 0,     -- 被召回命中次数（记忆"越用越重要"）
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    history    TEXT NOT NULL DEFAULT '[]'      -- JSON 数组 [{value, at}]，旧值保留
);
CREATE INDEX IF NOT EXISTS idx_mem_key ON memories(category, key);
CREATE INDEX IF NOT EXISTS idx_mem_imp ON memories(importance DESC);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class MemoryStore:
    """SQLite 持久化 + BM25 检索的内存态封装（线程安全）。"""

    def __init__(self, db_path: str, max_entries: int = 1500, history_keep: int = 5):
        self.db_path = db_path
        self.max_entries = max_entries
        self.history_keep = history_keep
        self._lock = threading.RLock()
        self.index = BM25Index()
        self.enabled = True
        try:
            os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(_SCHEMA)
            self.conn.commit()
            self._load_index()
        except Exception as e:  # noqa: BLE001  零侵入降级：DB 不可用则静默禁用
            print(f"[记忆] 存储初始化失败，记忆功能已禁用：{e}")
            self.enabled = False
            self.conn = None

    # ---------- 索引 ----------
    def _load_index(self):
        try:
            rows = self.conn.execute(
                "SELECT id, value, key, category FROM memories").fetchall()
            for row in rows:
                mem_id, value, key, category = row
                self.index.add(mem_id, tokenize(f"{key} {category} {value}"))
        except Exception as e:  # noqa: BLE001
            print(f"[记忆] 索引加载失败：{e}")

    def _tokens_of(self, row: dict) -> list[str]:
        return tokenize(f"{row.get('key','')} {row.get('category','')} {row.get('value','')}")

    def _ensure_conn(self):
        if not self.enabled or self.conn is None:
            raise RuntimeError("memory disabled")

    # ---------- 增 ----------
    def add(self, category: str, key: str, value: str,
            importance: float = 5.0, kind: str = "fact") -> int:
        """新增记忆；同 (category,key) 的 fact → 覆盖 + 保留历史（返回行 id）。
        note 类（kind='note'）不覆盖，直接追加。"""
        if not self.enabled:
            return -1
        if category in ("note", "event"):   # note/event 永远是追加式记录，不参与覆盖
            kind = "note"
        value = (value or "").strip()[:200]
        if not value:
            return -1
        key = (key or "").strip()[:60]
        now = _now()
        with self._lock:
            self._ensure_conn()
            cur = self.conn.execute(
                "SELECT id, value, history FROM memories WHERE category=? AND key=? AND kind='fact'",
                (category, key))
            row = cur.fetchone()
            if row and kind == "fact" and key:
                mem_id, old_value, old_history = row
                try:
                    hist = json.loads(old_history or "[]")
                except Exception:
                    hist = []
                if old_value != value:
                    hist.append({"value": old_value, "at": now})
                    hist = hist[-self.history_keep:]
                self.conn.execute(
                    "UPDATE memories SET value=?, importance=MAX(importance,?), "
                    "updated_at=?, history=? WHERE id=?",
                    (value, importance, now, json.dumps(hist, ensure_ascii=False), mem_id))
                self.conn.commit()
                self.index.add(mem_id, self._tokens_of(
                    {"key": key, "category": category, "value": value}))
                return mem_id
            cur = self.conn.execute(
                "INSERT INTO memories (category, key, value, importance, kind, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (category, key, value, float(importance), kind, now, now))
            mem_id = cur.lastrowid
            self.conn.commit()
            self.index.add(mem_id, self._tokens_of(
                {"key": key, "category": category, "value": value}))
            self._evict_if_needed()
            return mem_id

    def _evict_if_needed(self):
        """超容量时淘汰 importance 最低的记忆（AI 自行判断重要度的兜底机制）。"""
        try:
            n = self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            while n > self.max_entries:
                row = self.conn.execute(
                    "SELECT id FROM memories ORDER BY importance ASC, updated_at ASC LIMIT 1"
                ).fetchone()
                if not row:
                    break
                self._delete_row(row[0])
                n -= 1
        except Exception as e:  # noqa: BLE001
            print(f"[记忆] 淘汰失败：{e}")

    # ---------- 删 ----------
    def delete(self, mem_id: int) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            try:
                return self._delete_row(mem_id)
            except Exception:
                return False

    def _delete_row(self, mem_id: int) -> bool:
        self.index.remove(mem_id)
        cur = self.conn.execute("DELETE FROM memories WHERE id=?", (mem_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def clear(self) -> int:
        if not self.enabled:
            return 0
        with self._lock:
            n = self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            self.conn.execute("DELETE FROM memories")
            # 重置 AUTOINCREMENT 计数器，让清空后新记忆 id 从 1 开始
            try:
                self.conn.execute("DELETE FROM sqlite_sequence WHERE name='memories'")
            except Exception:
                pass
            self.conn.commit()
            self.index = BM25Index()
            return n

    def cleanup_garbage(self) -> int:
        """清理已入库的提问残渣记忆（如 "clear weather do u know why"）。
        只删含提问残渣的（问号/疑问词/对话残留），不动正常英文偏好（如 "coffee"）。
        返回删除条数；异常静默返回 0。"""
        from memory_extractor import _value_is_garbage
        if not self.enabled:
            return 0
        removed = 0
        with self._lock:
            try:
                rows = self.conn.execute(
                    "SELECT id, category, value FROM memories").fetchall()
                for mem_id, category, value in rows:
                    if _value_is_garbage(value or "", category, check_en=False):
                        if self._delete_row(mem_id):
                            removed += 1
            except Exception as e:  # noqa: BLE001
                print(f"[记忆] 垃圾清理失败（静默）：{e}")
        return removed

    # ---------- 查 ----------
    def get(self, mem_id: int) -> dict | None:
        if not self.enabled:
            return None
        with self._lock:
            row = self.conn.execute(
                "SELECT id, category, key, value, importance, hits, created_at, updated_at, history "
                "FROM memories WHERE id=?", (mem_id,)).fetchone()
            return self._row_to_dict(row) if row else None

    @staticmethod
    def _row_to_dict(row) -> dict:
        (mem_id, category, key, value, importance, hits, created_at, updated_at, history) = row
        try:
            hist = json.loads(history or "[]")
        except Exception:
            hist = []
        return {"id": mem_id, "category": category, "key": key, "value": value,
                "importance": importance, "hits": hits,
                "created_at": created_at, "updated_at": updated_at, "history": hist}

    def all(self, limit: int = 500) -> list[dict]:
        if not self.enabled:
            return []
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, category, key, value, importance, hits, created_at, updated_at, history "
                "FROM memories ORDER BY importance DESC, updated_at DESC LIMIT ?",
                (max(1, limit),)).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def count(self) -> int:
        if not self.enabled:
            return 0
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    # ---------- 检索 ----------
    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """BM25 关键词检索：返回 top-k 记忆（并累加 hits）。"""
        if not self.enabled:
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        with self._lock:
            scored = self.index.score(tokens)
            if not scored:
                return []
            top = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
            out = []
            for mem_id, _score in top:
                row = self.conn.execute(
                    "SELECT id, category, key, value, importance, hits, created_at, updated_at, history "
                    "FROM memories WHERE id=?", (mem_id,)).fetchone()
                if row:
                    d = self._row_to_dict(row)
                    d["score"] = round(_score, 4)
                    out.append(d)
                    # 命中计数 + 重要度微升（记忆"越用越重要"）
                    self.conn.execute(
                        "UPDATE memories SET hits=hits+1, "
                        "importance=MIN(10.0, importance+0.15) WHERE id=?", (mem_id,))
            self.conn.commit()
            return out

    def close(self):
        if getattr(self, "conn", None):
            try:
                self.conn.close()
            except Exception:
                pass
