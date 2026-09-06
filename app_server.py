# -*- coding: utf-8 -*-
import json
import mimetypes
import os
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote, parse_qs

import config
from llm_client import LLMClient, llm_port_alive
from tts import (synthesize, synthesize_indextts, detect_text_lang,
                 emo_vector_from_config)
from stt import STTClient, transcribe_via_api
from memory import MemoryManager
import chat_log

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
    RESOURCE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = BASE_DIR

WEB_DIR = os.path.join(RESOURCE_DIR, "web")

_lock = threading.Lock()
_mem_lock = threading.Lock()
_llm = None
_stt = None
_memory = None

def _resolve_root(p: str) -> str:
    if not os.path.isabs(p):
        p = os.path.join(BASE_DIR, p)
    return os.path.abspath(p)


def resolve_model_root() -> str:
    return _resolve_root(config.MODEL_PATH)


def find_models():
    root = resolve_model_root()
    if not os.path.isdir(root):
        return []
    out = []
    for dirpath, _dirs, filenames in os.walk(root):
        for f in filenames:
            if not f.lower().endswith(".model3.json"):
                continue
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root).replace("\\", "/")
            name = os.path.basename(os.path.dirname(full))
            if not name or name == os.path.basename(root):
                name = f[:-len(".model3.json")]
            out.append({"name": name, "rel": rel, "url": "/models/" + rel})
    out.sort(key=lambda m: m["name"])  # 让"默认取第一个"确定化
    return out


def pick_model(name: str = ""):
    models = find_models()
    if not models:
        return {"error": f"在 {config.MODEL_PATH} 里没找到 .model3.json 模型文件，"
                         "请把模型文件夹放进去后刷新页面"}
    if name:
        for m in models:
            if m["name"] == name:
                return m
        return {"error": f"模型 {name} 不存在，可用：" + "、".join(m["name"] for m in models)}
    if config.MODEL_NAME:
        for m in models:
            if m["name"] == config.MODEL_NAME:
                return m
    return models[0]


def _expression_name(filename: str) -> str:
    """把 害羞脸.exp3.json 归一成 害羞脸（去掉 .exp3 双扩展名残留）。"""
    base = os.path.splitext(filename)[0]
    return base[:-len(".exp3")] if base.lower().endswith(".exp3") else base


def augment_model3(model3_path: str) -> bytes:
    """把模型同目录及常用子目录下的动作/表情注入 model3.json。
    必须写入 FileReferences.Motions/Expressions（pixi-live2d 只读这个位置，
    顶层同名字段会被忽略 → 模型无动作、Part 可见性曲线失效）。
    扫描：同目录 + animations/ + motions/ + expressions/ 子目录。
    Idle 只按依据注入（模型原有或 .vtube.json 的 IdleAnimation），不硬凑效果动作。
    """
    with open(model3_path, "rb") as f:
        data = json.loads(f.read().decode("utf-8"))
    model_dir = os.path.dirname(model3_path)
    try:
        root_files = os.listdir(model_dir)
    except Exception:
        root_files = []

    # .vtube.json 指定的待机（VTS 模型通用约定）
    vtube_idle = ""
    for f in root_files:
        if f.lower().endswith(".vtube.json"):
            try:
                with open(os.path.join(model_dir, f), encoding="utf-8") as vf:
                    vd = json.load(vf)
                vtube_idle = ((vd.get("FileReferences") or {}).get("IdleAnimation") or "").strip()
            except Exception:
                vtube_idle = ""
            break

    # 收集 motion3.json：先子目录 setdefault，后同目录覆盖（同目录优先）
    motion_map = {}
    for sd in ("animations", "motions"):
        sd_path = os.path.join(model_dir, sd)
        if os.path.isdir(sd_path):
            try:
                for f in sorted(os.listdir(sd_path)):
                    if f.lower().endswith(".motion3.json"):
                        motion_map.setdefault(f[:-len(".motion3.json")], f"{sd}/{f}")
            except Exception:
                pass
    for f in sorted(root_files):
        if f.lower().endswith(".motion3.json"):
            motion_map[f[:-len(".motion3.json")]] = f

    if motion_map:
        motions_dict = {g: [{"Name": g, "File": rel, "FadeInTime": 0.5, "FadeOutTime": 0.5}]
                        for g, rel in motion_map.items()}
        if "Idle" not in motions_dict and vtube_idle:
            base = os.path.basename(vtube_idle)
            if base.lower().endswith(".motion3.json"):
                base = base[:-len(".motion3.json")]
            if base in motion_map:
                motions_dict["Idle"] = [{"Name": "Idle", "File": motion_map[base],
                                         "FadeInTime": 0.5, "FadeOutTime": 0.5}]
        data.setdefault("FileReferences", {})["Motions"] = motions_dict

    # 表情：同目录 + 子目录
    fr = data.get("FileReferences") or {}
    exp_out = list(fr.get("Expressions") or [])
    exp_seen = {(e.get("Name") if isinstance(e, dict) else e) for e in exp_out}

    def add_exp(rel_path: str):
        name = _expression_name(os.path.basename(rel_path))
        if name not in exp_seen:
            exp_seen.add(name)
            exp_out.append({"Name": name, "File": rel_path})

    for sd in ("expressions", "animations"):
        sd_path = os.path.join(model_dir, sd)
        if os.path.isdir(sd_path):
            try:
                for f in sorted(os.listdir(sd_path)):
                    if f.lower().endswith(".exp3.json"):
                        add_exp(f"{sd}/{f}")
            except Exception:
                pass
    for f in sorted(root_files):
        if f.lower().endswith(".exp3.json"):
            add_exp(f)
    if exp_out:
        data.setdefault("FileReferences", {})["Expressions"] = exp_out

    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def resolve_backgrounds_root() -> str:
    return _resolve_root(config.BACKGROUNDS_PATH)


def pick_background():
    """在背景图文件夹找一张图，返回 {url, name} 或 {error}。"""
    root = resolve_backgrounds_root()
    if not os.path.isdir(root):
        return {"error": "no bg"}
    files = sorted(os.listdir(root))
    if config.BACKGROUND_IMAGE:
        files = [config.BACKGROUND_IMAGE] + files
    for f in files:
        if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")):
            return {"url": "/backgrounds/" + f, "name": f}
    return {"error": "no bg"}

_TTS_GPT_SUFFIX = re.compile(r"-e\d+\.ckpt$", re.I)
_TTS_SOVITS_SUFFIX = re.compile(r"_e\d+(_s\d+)?\.pth$", re.I)


def _tts_base_name(filename: str) -> str:
    low = filename.lower()
    if low.endswith(".ckpt"):
        return _TTS_GPT_SUFFIX.sub("", filename)
    if low.endswith(".pth"):
        return _TTS_SOVITS_SUFFIX.sub("", filename)
    return filename


def find_tts_models(dir_path: str = ""):
    """递归扫描目录（默认 config.TTS_MODELS_DIR），按前缀配对 ckpt+pth，返回 [{name, gpt, sovits, ref_audio, prompt}]。"""
    d = dir_path or config.TTS_MODELS_DIR
    if not d or not os.path.isdir(d):
        return []
    by_base = {}
    for dirpath, _dirs, files in os.walk(d):
        for f in files:
            low = f.lower()
            if low.endswith(".ckpt") or low.endswith(".pth"):
                entry = by_base.setdefault(_tts_base_name(f), {"name": _tts_base_name(f), "gpt": "", "sovits": ""})
                full = os.path.join(dirpath, f)
                if low.endswith(".ckpt"):
                    entry["gpt"] = full
                else:
                    entry["sovits"] = full
    models = [m for m in by_base.values() if m["gpt"] and m["sovits"]]
    models.sort(key=lambda m: m["name"])
    for m in models:
        mdir = os.path.dirname(m["gpt"])
        ref = os.path.join(mdir, "ref.wav")
        pfile = os.path.join(mdir, "prompt.txt")
        m["ref_audio"] = ref if os.path.exists(ref) else ""
        m["prompt"] = ""
        if os.path.exists(pfile):
            try:
                with open(pfile, encoding="utf-8") as fh:
                    m["prompt"] = fh.read().strip()
            except Exception:
                m["prompt"] = ""
    return models


def switch_tts_model(gpt_path: str, sovits_path: str) -> None:
    import requests
    for ep, w in (("set_gpt_weights", gpt_path), ("set_sovits_weights", sovits_path)):
        try:
            r = requests.get(f"{config.GPT_SOVITS_URL}/{ep}",
                             params={"weights_path": w}, timeout=120)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"连不上 GPT-SoVITS（{ep}）：{e}")
        if r.status_code != 200:
            raise RuntimeError(f"切换 {ep} 失败（HTTP {r.status_code}）：{r.text[:200]}")


def resolve_tts_text_lang(text: str) -> str:
    return detect_text_lang(text) or config.TTS_TEXT_LANG


def scan_tts_models():
    """扫描 TTS_MODELS_DIR 顶层子文件夹，返回 [{name, ckpt, pth, ref_audio, prompt}]。"""
    root = config.TTS_MODELS_DIR
    if not root or not os.path.isdir(root):
        return []
    out = []
    for d in sorted(os.listdir(root)):
        folder = os.path.join(root, d)
        if not os.path.isdir(folder):
            continue
        ckpt = pth = None
        for f in os.listdir(folder):
            if f.lower().endswith(".ckpt"):
                ckpt = os.path.join(folder, f)
            elif f.lower().endswith(".pth"):
                pth = os.path.join(folder, f)
        if ckpt and pth:
            ref = os.path.join(folder, "ref.wav")
            prompt_txt = os.path.join(folder, "prompt.txt")
            prompt = ""
            if os.path.exists(prompt_txt):
                try:
                    with open(prompt_txt, encoding="utf-8") as fh:
                        prompt = fh.read().strip()
                except Exception:
                    prompt = ""
            out.append({"name": d, "ckpt": ckpt, "pth": pth,
                        "ref_audio": ref if os.path.exists(ref) else "", "prompt": prompt})
    return out

def get_llm() -> LLMClient:
    global _llm
    with _lock:
        if _llm is None:
            model = config.LM_MODEL
            if not model:
                models = LLMClient.list_models(config.LM_STUDIO_BASE_URL)
                if not models:
                    raise RuntimeError("LLM 服务（LM Studio/Ollama）没连上或没加载模型，请先启动它的本地服务器")
                model = models[0]
                print(f"[自动选择模型] {model}")
            _llm = LLMClient(config.LM_STUDIO_BASE_URL, model,
                             config.PERSONA_SYSTEM_PROMPT,
                             config.LM_TEMPERATURE, config.LM_MAX_HISTORY,
                             memory=get_memory(),
                             chat_provider=_chat_log_provider())
        return _llm


def _chat_log_provider():
    """返回聊天记录注入闭包：config.CHAT_LOG_INJECT=False 时返回 None（不注入）。"""
    if not getattr(config, "CHAT_LOG_INJECT", True):
        return None

    def _provider(history):
        top_k = int(getattr(config, "CHAT_LOG_TOP_K", 50) or 50)
        return chat_log.load_for_inject(top_k, history)

    return _provider


def get_stt() -> STTClient:
    global _stt
    with _lock:
        if _stt is None:
            _stt = STTClient(config.STT_MODEL_SIZE, config.STT_DEVICE,
                             config.STT_COMPUTE_TYPE, config.STT_LANGUAGE)
        return _stt


def get_memory() -> MemoryManager | None:
    """懒加载长期记忆（零侵入降级：禁用/异常一律返回 None，不影响聊天）。"""
    global _memory
    if not getattr(config, "MEMORY_ENABLED", True):
        return None
    with _mem_lock:
        if _memory is None:
            try:
                db_path = _resolve_root(getattr(config, "MEMORY_DB_PATH", "memory.db"))
                _memory = MemoryManager(db_path=db_path)
                print(f"[记忆] 已启用：{db_path}（{_memory.count()} 条）")
            except Exception as e:  # noqa: BLE001
                print(f"[记忆] 启用失败（已降级禁用）：{e}")
                _memory = None
        return _memory


def _extract_memories_async(user_text: str):
    """回复成功后后台异步提取记忆（不阻塞 TTS 播放 / UI 响应 / SSE 流）。"""
    try:
        mem = get_memory()
        if mem is not None:
            mem.ingest_async(user_text)
    except Exception as e:  # noqa: BLE001
        print(f"[记忆] 异步提取调度失败（静默）：{e}")


def _http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        from urllib.parse import urlparse
        u = urlparse(url)
        with socket.create_connection((u.hostname, u.port or 80), timeout=0.3):
            pass
    except Exception:
        return False
    try:
        import requests
        return requests.get(url, timeout=timeout).status_code == 200
    except Exception:
        return False


def service_status() -> dict:
    status = {"llm": False, "tts": False, "stt": False}
    try:
        if llm_port_alive(config.LM_STUDIO_BASE_URL):
            status["llm"] = bool(LLMClient.list_models(config.LM_STUDIO_BASE_URL))
    except Exception:
        pass
    try:
        # GPT-SoVITS api.py 无 "/" 路由（会 404 刷屏），用 /openapi.json 做健康检查
        # IndexTTS2 薄 API 的 GET / 秒回。
        if getattr(config, "TTS_ENGINE", "gptsovits") == "indextts":
            status["tts"] = _http_ok(f"{config.INDEX_TTS_URL}/")
        else:
            status["tts"] = _http_ok(f"{config.GPT_SOVITS_URL}/openapi.json")
    except Exception:
        pass
    if config.STT_BACKEND == "api":
        try:
            status["stt"] = _http_ok(config.STT_API_URL)
        except Exception:
            pass
    else:
        status["stt"] = _stt is not None
    return status


def _memory_payload() -> dict:
    """记忆管理面板数据：{enabled, count, memories[]}。禁用时返回空列表。"""
    try:
        mem = get_memory()
        if mem is None:
            return {"enabled": False, "count": 0, "memories": []}
        return {"enabled": mem.enabled, "count": mem.count(),
                "memories": mem.list(limit=getattr(config, "MEM_MAX_ENTRIES", 1500))}
    except Exception as e:  # noqa: BLE001
        print(f"[记忆] 列表读取失败（静默）：{e}")
        return {"enabled": False, "count": 0, "memories": []}

# 配置读取 / 写入
EDITABLE_KEYS = (
    "LM_STUDIO_BASE_URL", "LM_MODEL", "GPT_SOVITS_URL",
    "TTS_REF_AUDIO_PATH", "TTS_PROMPT_TEXT", "STT_API_URL",
    "STT_STREAM_API_URL",
    "TTS_ENGINE", "INDEX_TTS_URL", "INDEX_TTS_LANG",
    "INDEX_TTS_VOICES_DIR", "INDEX_TTS_REF_AUDIO_PATH",
    "INDEX_TTS_EMO", "INDEX_TTS_EMO_STRENGTH",
)


def _config_path() -> str:
    return os.path.join(BASE_DIR, "config.py")


def get_editable_config() -> dict:
    cfg = {k: getattr(config, k) for k in EDITABLE_KEYS}
    # 情绪映射（只读，不写回）：前端按此把标准情绪解析为模型真实表情名
    cfg["EMOTION_MAPPING"] = getattr(config, "EMOTION_MAPPING", {})
    cfg["EMOTION_DEFAULT"] = getattr(config, "EMOTION_DEFAULT", "neutral")
    # 语音通话 VAD 停嘴超时（只读，改 config.py 生效）：前端按此判定"说完"
    cfg["VAD_SILENCE_TIMEOUT_MS"] = int(getattr(config, "VAD_SILENCE_TIMEOUT_MS", 2500))
    # 长期记忆（只读，改 config.py 生效）：前端显示开关/条数等
    cfg["MEMORY_ENABLED"] = bool(getattr(config, "MEMORY_ENABLED", True))
    cfg["MEM_LLM_EXTRACT"] = bool(getattr(config, "MEM_LLM_EXTRACT", False))
    cfg["MEM_RETRIEVE_TOP_K"] = int(getattr(config, "MEM_RETRIEVE_TOP_K", 5))
    cfg["MEM_MAX_ENTRIES"] = int(getattr(config, "MEM_MAX_ENTRIES", 1500))
    cfg["TOUCH_ENABLED"] = bool(getattr(config, "TOUCH_ENABLED", True))
    cfg["TOUCH_HOLD_MS"] = int(getattr(config, "TOUCH_HOLD_MS", 3000))
    cfg["TOUCH_RESPONSES"] = getattr(config, "TOUCH_RESPONSES", {})
    return cfg


def normalize_stt_url(value: str) -> str:
    """STT 地址归一：裸端口 9988 → http://127.0.0.1:9988/transcribe。"""
    v = (value or "").strip()
    if not v:
        raise ValueError("STT 地址不能为空")
    if v.isdigit():
        return f"http://127.0.0.1:{v}/transcribe"
    if v.startswith(("http://", "https://")):
        return v
    raise ValueError("STT 地址需是完整 URL（如 http://127.0.0.1:9988/transcribe）或端口号（如 9988）")


def normalize_stt_stream_url(value: str) -> str:
    """流式 STT 地址归一：裸端口 9989 → http://127.0.0.1:9989/transcribe_stream；
    完整 URL 若以 /transcribe 结尾自动换成 /transcribe_stream；空串保留（走自动推导）。"""
    v = (value or "").strip()
    if not v:
        return ""
    if v.isdigit():
        return f"http://127.0.0.1:{v}/transcribe_stream"
    if v.startswith(("http://", "https://")):
        v = v.rstrip("/")
        if v.endswith("/transcribe_stream"):
            return v
        if v.endswith("/transcribe"):
            return v[:-len("/transcribe")] + "/transcribe_stream"
        return v + "/transcribe_stream"
    raise ValueError("STT 流式地址需是完整 URL（如 http://127.0.0.1:9989/transcribe_stream）或端口号（如 9989）")


def save_config_updates(updates: dict) -> None:
    """把指定 key 的新值写回 config.py（只改这几行，其余原样保留）。

    安全：路径反斜杠转正斜杠（防反斜杠转义闪退）；写后 compile 校验，出错回滚。
    """
    unknown = set(updates) - set(EDITABLE_KEYS)
    if unknown:
        raise ValueError(f"不支持的配置项：{', '.join(sorted(unknown))}")
    path = _config_path()
    with open(path, "r", encoding="utf-8") as f:
        text = orig = f.read()
    for key, value in updates.items():
        if key == "STT_API_URL":
            value = normalize_stt_url(value)
        if key == "STT_STREAM_API_URL":
            value = normalize_stt_stream_url(value)
        if isinstance(value, str) and "\\" in value:
            value = value.replace("\\", "/")
        new_line = f"{key} = {json.dumps(value, ensure_ascii=False)}"
        text, n = re.subn(rf"^{re.escape(key)}\s*=.*$", new_line, text, count=1, flags=re.M)
        if n == 0:
            raise ValueError(f"config.py 里找不到 {key} 这一行")
    try:
        compile(text, path, "exec")
    except SyntaxError as e:
        raise ValueError(f"配置写入后会导致语法错误（{e}），已放弃写入")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        compile(text, path, "exec")
    except SyntaxError as e:
        with open(path, "w", encoding="utf-8") as f:
            f.write(orig)
        raise ValueError(f"配置写入后语法校验失败（{e}），已回滚")
    
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send_bytes(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, code=200):
        self._send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                         "application/json; charset=utf-8", code)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _send_sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")  # 禁止中间代理缓冲
        self.end_headers()

    def _sse_send(self, obj: dict):
        self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
        self.wfile.flush()

    def _safe_join(self, root: str, rel: str):
        full = os.path.realpath(os.path.join(root, unquote(rel)))
        return full if full.startswith(os.path.realpath(root) + os.sep) else None

    def _serve_static(self, name: str):
        full = os.path.realpath(os.path.join(WEB_DIR, name))
        if not full.startswith(os.path.realpath(WEB_DIR) + os.sep) or not os.path.isfile(full):
            return self._send_json({"error": "文件不存在"}, 404)
        ctype = "application/javascript" if name.endswith(".js") else \
            (mimetypes.guess_type(full)[0] or "application/octet-stream")
        self._send_bytes(open(full, "rb").read(), ctype)

    def _serve_file(self, root: str, prefix: str, path: str):
        full = self._safe_join(root, path[len(prefix):])
        if not full or not os.path.isfile(full):
            return self._send_json({"error": "文件不存在"}, 404)
        if full.lower().endswith(".model3.json"):
            return self._send_bytes(augment_model3(full), "application/json")
        return self._send_bytes(open(full, "rb").read(),
                                mimetypes.guess_type(full)[0] or "application/octet-stream")

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            return self._serve_static("index.html")
        if path == "/favicon.ico":
            fav = os.path.join(RESOURCE_DIR, "favicon.ico")
            if not os.path.isfile(fav):
                fav = os.path.join(BASE_DIR, "favicon.ico")
            if os.path.isfile(fav):
                return self._send_bytes(open(fav, "rb").read(), "image/x-icon")
            return self._send_json({"error": "文件不存在"}, 404)
        if path.startswith("/lib/") or path in ("/style.css", "/app.js"):
            return self._serve_static(path.lstrip("/"))
        if path.startswith("/models/"):
            return self._serve_file(resolve_model_root(), "/models/", path)
        if path.startswith("/backgrounds/"):
            return self._serve_file(resolve_backgrounds_root(), "/backgrounds/", path)
        if path == "/api/model":
            return self._send_json(pick_model(parse_qs(urlparse(self.path).query).get("name", [""])[0]))
        if path == "/api/models":
            return self._send_json({"models": find_models()})
        if path == "/api/title":
            return self._send_json({"title": config.CHAT_TITLE})
        if path == "/api/background":
            return self._send_json(pick_background())
        if path == "/api/tts_models":
            scan_dir = parse_qs(urlparse(self.path).query).get("dir", [""])[0]
            models = find_tts_models(scan_dir)
            return self._send_json({"models": models,
                                    "gpts": sorted({m["gpt"] for m in models}),
                                    "sovits": sorted({m["sovits"] for m in models}),
                                    "dir": scan_dir or config.TTS_MODELS_DIR})
        if path == "/api/status":
            return self._send_json(service_status())
        if path == "/api/tts_voices":   
            # IndexTTS2 参考音频列表（前端音色下拉）：扫 config.INDEX_TTS_VOICES_DIR 下的 wav
            d = getattr(config, "INDEX_TTS_VOICES_DIR", "")
            voices = []
            try:
                for f in sorted(os.listdir(d)):
                    if f.lower().endswith(".wav") and os.path.isfile(os.path.join(d, f)):
                        voices.append({"name": f, "path": os.path.join(d, f)})
            except Exception:
                pass
            return self._send_json({"voices": voices, "dir": d})       
        if path == "/api/config":
            return self._send_json(get_editable_config())
        if path == "/api/llm_models":
            try:
                models = LLMClient.list_models(config.LM_STUDIO_BASE_URL)
            except Exception as e:  # noqa: BLE001
                return self._send_json({"error": f"LLM 服务不可用：{e}"}, 503)
            return self._send_json({"models": models})
        if path == "/api/features":
            return self._send_json({
                "streaming": bool(getattr(config, "ENABLE_STREAMING", False)),
                "stream_tts": bool(getattr(config, "TTS_STREAMING", False)),
                "stream_tts_chunk": int(getattr(config, "STREAM_TTS_CHUNK_SIZE", 0) or 0),
            })
        if path == "/api/memory":
            return self._send_json(_memory_payload())
        return self._send_json({"error": "404"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/chat/stream":
                self._send_sse_headers()
                try:
                    data = json.loads(self._read_body().decode("utf-8"))
                except Exception:  # noqa: BLE001
                    self._sse_send({"error": "请求体解析失败"})
                    return
                lang = data.get("lang")
                src = data.get("source", "text")          # voice=语音通话 / text=打字
                user_text = data.get("text", "")
                chat_log.save_message("user", user_text, source=src)
                llm = get_llm()
                try:
                    sys_preview = (llm.system_prompt or "")[:80].replace("\n", " ")
                    print(f"[reply-lang v2] path=/api/chat/stream lang={lang!r} sys=\"{sys_preview}…\"", flush=True)
                    emotion_sent = False
                    pieces: list[str] = []
                    with _lock:
                        try:
                            for piece in llm.reply_stream(user_text, lang):
                                pieces.append(piece)
                                if llm.last_emotion and not emotion_sent:
                                    self._sse_send({"emotion": llm.last_emotion})
                                    emotion_sent = True
                                self._sse_send({"content": piece, "done": False})
                        except Exception as e:  # noqa: BLE001
                            try:
                                llm.clear_history()
                            except Exception:
                                pass
                            self._sse_send({"error": str(e)})
                            return
                    if llm.last_emotion and not emotion_sent:
                        self._sse_send({"emotion": llm.last_emotion})
                    self._sse_send({"done": True})
                    chat_log.save_message("assistant", "".join(pieces), source=src)
                    _extract_memories_async(user_text)
                except Exception as e:  # noqa: BLE001
                    try:
                        self._sse_send({"error": str(e)})
                    except Exception:
                        pass
                return

            if path == "/api/chat":
                data = json.loads(self._read_body().decode("utf-8"))
                lang = data.get("lang")
                src = data.get("source", "text")
                user_text = data.get("text", "")
                chat_log.save_message("user", user_text, source=src)
                llm = get_llm()
                sys_preview = (llm.system_prompt or "")[:80].replace("\n", " ")
                print(f"[reply-lang v2] path=/api/chat lang={lang!r} sys=\"{sys_preview}…\"", flush=True)
                try:
                    reply = llm.reply(user_text, lang)
                except Exception:
                    llm.clear_history()  # 避免"连续 user"卡死
                    raise
                chat_log.save_message("assistant", reply, source=src)
                _extract_memories_async(user_text)
                return self._send_json({"reply": reply, "emotion": llm.last_emotion or "neutral"})

            if path == "/api/clear_chat":
                if _llm is not None:
                    _llm.clear_history()
                return self._send_json({"ok": True})

            if path == "/api/tts_model_switch":
                data = json.loads(self._read_body().decode("utf-8"))
                gpt, sovits = data.get("gpt", ""), data.get("sovits", "")
                if not gpt or not sovits:
                    return self._send_json({"error": "缺少 GPT 或 SoVITS 权重路径"}, 400)
                try:
                    switch_tts_model(gpt, sovits)
                except Exception as e:  # noqa: BLE001
                    return self._send_json({"error": str(e)}, 400)
                return self._send_json({"ok": True})

            if path == "/api/tts_model_apply":
                data = json.loads(self._read_body().decode("utf-8"))
                gpt, sovits = data.get("gpt", ""), data.get("sovits", "")
                ref_audio, prompt = data.get("ref_audio", ""), data.get("prompt", "")
                if not gpt or not sovits:
                    return self._send_json({"error": "缺少 GPT 或 SoVITS 权重路径"}, 400)
                try:
                    switch_tts_model(gpt, sovits)
                    if ref_audio and os.path.exists(ref_audio):
                        config.TTS_REF_AUDIO_PATH = ref_audio
                    if prompt:
                        config.TTS_PROMPT_TEXT = prompt
                    if data.get("persist"):
                        ups = {}
                        if ref_audio and os.path.exists(ref_audio):
                            ups["TTS_REF_AUDIO_PATH"] = ref_audio
                        if prompt:
                            ups["TTS_PROMPT_TEXT"] = prompt
                        if ups:
                            try:
                                save_config_updates(ups)
                            except Exception:
                                pass
                except Exception as e:  # noqa: BLE001
                    return self._send_json({"error": str(e)}, 400)
                return self._send_json({"ok": True})

            if path == "/api/tts":
                data = json.loads(self._read_body().decode("utf-8"))
                text = data.get("text", "")
                if getattr(config, "TTS_ENGINE", "gptsovits") == "indextts":
                    # IndexTTS2 引擎：用独立的 INDEX_TTS_REF_AUDIO_PATH（空则回落 TTS_REF_AUDIO_PATH）。
                    # 语言优先用请求体传的（前端所选回复语言），无效才回退 config。
                    req_lang = (data.get("lang") or "").strip().upper()
                    lang = req_lang if req_lang else getattr(config, "INDEX_TTS_LANG", "ZH")
                    ref = getattr(config, "INDEX_TTS_REF_AUDIO_PATH", "") or config.TTS_REF_AUDIO_PATH or ""
                    wav = synthesize_indextts(
                        config.INDEX_TTS_URL, text, ref,
                        lang=lang,
                        emo_vector=emo_vector_from_config(
                            getattr(config, "INDEX_TTS_EMO", "neutral"),
                            float(getattr(config, "INDEX_TTS_EMO_STRENGTH", 0.6)),
                        ),
                        emo_alpha=float(getattr(config, "INDEX_TTS_EMO_STRENGTH", 0.6)),
                    )
                    return self._send_bytes(wav, "audio/wav")          
                lang = data.get("lang")
                detected = detect_text_lang(text)
                if detected in ("en", "zh"):
                    text_lang = detected          # 脚本明确 → 按脚本读（最可靠）
                elif lang in ("en", "zh"):
                    text_lang = lang              # 纯标点/数字 → 用所选语言
                else:
                    text_lang = config.TTS_TEXT_LANG
                wav = synthesize(config.GPT_SOVITS_URL, text,
                                 config.TTS_REF_AUDIO_PATH, config.TTS_PROMPT_TEXT,
                                 config.TTS_PROMPT_LANG, text_lang,
                                 config.TTS_TEXT_SPLIT_METHOD, config.TTS_MEDIA_TYPE,
                                 config.TTS_STREAMING)
                return self._send_bytes(wav, f"audio/{config.TTS_MEDIA_TYPE}")

            if path == "/api/transcribe":
                audio_bytes = self._read_body()
                if not audio_bytes:
                    return self._send_json({"text": ""})
                if config.STT_BACKEND == "api":
                    text = transcribe_via_api(audio_bytes, config.STT_API_URL,
                                              config.STT_API_RESPONSE_FIELD,
                                              config.STT_API_TIMEOUT)
                else:
                    stt = get_stt()
                    text = stt.transcribe(audio_bytes)
                    if not text:
                        text = stt.transcribe(audio_bytes, use_vad=False)
                return self._send_json({"text": text})

            if path == "/api/call/transcribe":
                audio_bytes = self._read_body()
                if not audio_bytes:
                    return self._send_json({"text": ""})
                stream_url = getattr(config, "STT_STREAM_API_URL", "") or \
                    config.STT_API_URL.replace("/transcribe", "/transcribe_stream", 1)
                text = transcribe_via_api(audio_bytes, stream_url,
                                          config.STT_API_RESPONSE_FIELD,
                                          config.STT_API_TIMEOUT)
                return self._send_json({"text": text})

            if path == "/api/memory/delete":
                data = json.loads(self._read_body().decode("utf-8"))
                mem = get_memory()
                if mem is None:
                    return self._send_json({"error": "记忆功能未启用"}, 400)
                ok = mem.delete(int(data.get("id", 0) or 0))
                return self._send_json({"ok": ok})

            if path == "/api/memory/clear":
                mem = get_memory()
                if mem is None:
                    return self._send_json({"error": "记忆功能未启用"}, 400)
                n = mem.clear()
                return self._send_json({"ok": True, "cleared": n})

            if path == "/api/memory/toggle":
                data = json.loads(self._read_body().decode("utf-8"))
                mem = get_memory()
                if mem is None:
                    return self._send_json({"error": "记忆功能未启用"}, 400)
                mem.set_enabled(bool(data.get("enabled", False)))
                return self._send_json({"ok": True, "enabled": mem.enabled})

            if path == "/api/config":
                data = json.loads(self._read_body().decode("utf-8"))
                try:
                    save_config_updates(data.get("updates", {}))
                except ValueError as e:  # noqa: BLE001
                    return self._send_json({"error": str(e)}, 400)
                return self._send_json({"ok": True, "message": "配置已更新，请重启程序生效"})
        except Exception as e:  # noqa: BLE001
            return self._send_json({"error": str(e)}, 500)
        return self._send_json({"error": "404"}, 404)


def start_server(port: int = 8765):
    """后台线程启动服务器，返回 (server, thread)。"""
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[OK] 本地服务器已启动: http://127.0.0.1:{port}")
    return server, thread


if __name__ == "__main__":
    import webbrowser
    server, _thread = start_server()
    webbrowser.open("http://127.0.0.1:8765/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
