"""
每条记录一行 JSON：
    {"role": "user"|"assistant", "name": "主人"/"01", "content": "…",
     "ts": "2026-08-21T13:36:43", "source": "text"|"voice"}

- role:    说话方（user=用户 / assistant=角色）
- name:    显示名（用户取 config.CHAT_USER_NAME，角色取 config.PERSONA_NAME）
- content: 消息文本（语音通话 = ASR 转写后的文字）
- ts:      本地时间 ISO 字符串
- source:  消息来源（text=打字 / voice=语音通话），供前端/统计区分
"""

import json
import os
import sys
import threading
import time
from datetime import datetime

try:
    import config
except Exception:  # noqa: BLE001  (被单测直接 import 时兜底)
    config = None

# 打包成 exe 后：网页在临时解压目录，数据目录在 exe 旁
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_lock = threading.Lock()
_FILE_NAME = "chat.jsonl"


def _enabled() -> bool:
    return bool(getattr(config, "CHAT_LOG_ENABLED", True))


def _log_dir() -> str:
    """返回 memory/<角色名>/ 的绝对路径（自动创建）。"""
    cfg_dir = getattr(config, "CHAT_LOG_DIR", "memory") or "memory"
    base = cfg_dir if os.path.isabs(cfg_dir) else os.path.join(BASE_DIR, cfg_dir)
    persona = getattr(config, "PERSONA_NAME", "01") or "01"
    return os.path.join(base, str(persona))


def _log_file() -> str:
    return os.path.join(_log_dir(), _FILE_NAME)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def save_message(role: str, content: str, persona: str | None = None,
                 name: str | None = None, source: str = "text",
                 ts: str | None = None) -> bool:
    """追加一条聊天记录到磁盘。任何异常返回 False（不影响主流程）。"""
    if not _enabled():
        return False
    content = (content or "").strip()
    if not content:
        return False
    if role not in ("user", "assistant"):
        return False
    try:
        rec = {
            "role": role,
            "name": name or (_user_name() if role == "user" else _persona_name(persona)),
            "content": content[:2000],          # 超长截断，防撑爆文件
            "ts": ts or _now(),
            "source": source if source in ("text", "voice") else "text",
        }
        with _lock:
            d = _log_dir()
            os.makedirs(d, exist_ok=True)
            with open(_log_file(), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[聊天记录] 保存失败（静默跳过）：{e}")
        return False


def _user_name() -> str:
    return str(getattr(config, "CHAT_USER_NAME", "主人") or "主人")


def _persona_name(persona: str | None) -> str:
    return persona or str(getattr(config, "PERSONA_NAME", "01") or "01")


def load_recent(top_k: int = 50) -> list[dict]:
    """读取磁盘记录：用户、角色各取最近 top_k 条，按时间先后排序返回。

    返回 [{role, name, content, ts, source}, ...]。文件不存在/损坏行/被禁用 → []。
    """
    if not _enabled():
        return []
    path = _log_file()
    if not os.path.isfile(path):
        return []
    rows: list[dict] = []
    try:
        with _lock:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:  # noqa: BLE001 损坏行跳过
                        continue
                    if d.get("role") not in ("user", "assistant") or not d.get("content"):
                        continue
                    rows.append(d)
    except Exception as e:  # noqa: BLE001
        print(f"[聊天记录] 读取失败（静默跳过）：{e}")
        return []
    # 各角色取最近 top_k
    user = [r for r in rows if r["role"] == "user"][-top_k:]
    asst = [r for r in rows if r["role"] == "assistant"][-top_k:]
    merged = sorted(user + asst, key=lambda r: r.get("ts", ""))
    return merged


def load_for_inject(top_k: int = 50, skip_history=None) -> str | None:
    """构造注入 LLM 的"历史对话记录"文本块（None = 无记录或未启用）。

    skip_history: 会话内历史 [{role, content}, ...]，用于跳过与当前会话
    重复的最近记录（避免 LLM 同时看到磁盘历史与会话历史两份相同内容）。
    去重规则：从磁盘最新记录往前，若 (role, content) 已在会话历史中 → 跳过，
    直到用户/角色各收集满 top_k 条。
    """
    if not _enabled():
        return None
    path = _log_file()
    if not os.path.isfile(path):
        return None
    skip = set()
    for h in (skip_history or []):
        if h.get("role") in ("user", "assistant") and h.get("content"):
            skip.add((h["role"], h["content"]))
    # 从尾部向前收集，各角色 top_k 条（去重）
    picked_user: list[dict] = []
    picked_asst: list[dict] = []
    try:
        with _lock:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
    except Exception as e:  # noqa: BLE001
        print(f"[聊天记录] 注入读取失败（静默跳过）：{e}")
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        role = d.get("role")
        content = str(d.get("content", "")).strip()
        if role not in ("user", "assistant") or not content:
            continue
        if (role, content) in skip:
            continue
        if role == "user" and len(picked_user) < top_k:
            picked_user.append(d)
        elif role == "assistant" and len(picked_asst) < top_k:
            picked_asst.append(d)
        if len(picked_user) >= top_k and len(picked_asst) >= top_k:
            break
    merged = sorted(picked_user + picked_asst, key=lambda r: r.get("ts", ""))
    if not merged:
        return None
    return format_history(merged)


def format_history(records: list[dict]) -> str:
    """把记录列表格式化为紧凑文本块（时间 + 名字 + 内容）。"""
    lines = ["[你与用户的历史对话记录（按时间先后，对话中自然参考，不要生硬复述）]"]
    for r in records:
        ts = r.get("ts", "")
        when = ""
        if ts:
            try:
                when = datetime.fromisoformat(ts).strftime("%m月%d日 %H:%M ")
            except Exception:  # noqa: BLE001
                when = ""
        name = r.get("name") or ("主人" if r["role"] == "user" else "01")
        lines.append(f"- {when}{name}: {r.get('content', '')}")
    return "\n".join(lines)


def count() -> int:
    """当前角色记录总条数（未启用/文件不存在返回 0）。"""
    if not _enabled():
        return 0
    path = _log_file()
    if not os.path.isfile(path):
        return 0
    try:
        with _lock:
            with open(path, encoding="utf-8") as f:
                return sum(1 for ln in f if ln.strip())
    except Exception:  # noqa: BLE001
        return 0
