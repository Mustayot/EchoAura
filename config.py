# -*- coding: utf-8 -*-
# LM Studio：http://localhost:1234/v1；Ollama：http://localhost:11434/v1
LM_STUDIO_BASE_URL = "http://localhost:1234/v1"
# 留空 = 自动用第一个可用模型
LM_MODEL = "mistralai/ministral-3-3b"
# 0 最严谨，1.5 最放飞，一般 0.7
LM_TEMPERATURE = 0.7
LM_MAX_HISTORY = 20

GPT_SOVITS_URL = "http://127.0.0.1:9880"
# 参考音频路径（音色来源）。改成你自己的参考音频路径：
# Windows 路径建议用正斜杠 C:/xxx/xxx.wav 或 r"C:\xxx\xxx.wav" 前缀，
# 否则 \U \u 会被当转义导致程序闪退。留空则使用 TTS_MODELS_DIR 下首套音色。
TTS_REF_AUDIO_PATH = ""
# 参考音频里实际说的那句话
TTS_PROMPT_TEXT = ""
TTS_PROMPT_LANG = "en"
TTS_TEXT_LANG = "zh"
TTS_TEXT_SPLIT_METHOD = "cut5"
TTS_MEDIA_TYPE = "wav"
TTS_STREAMING = True

ENABLE_STREAMING = True
# 0 = 按句末标点即时朗读；正整数 = 每累积这么多字合成一次
STREAM_TTS_CHUNK_SIZE = 0

# TTS 音色模型目录：每个子文件夹 = 一个音色（*.ckpt + *.pth，可选 ref.wav + prompt.txt）
# 这里填你自己 GPT-SoVITS 的音色模型所在目录
TTS_MODELS_DIR = r"GPTsoVits"

STT_BACKEND = "api"
STT_API_URL = "http://127.0.0.1:9988/transcribe"
STT_STREAM_API_URL = "http://127.0.0.1:9989/transcribe_stream"
STT_API_RESPONSE_FIELD = "text"
STT_API_TIMEOUT = 30

VAD_SILENCE_TIMEOUT_MS = 2500

# 以下为内置 whisper 后端（STT_BACKEND="whisper"）的参数
HF_ENDPOINT = "https://hf-mirror.com"
STT_MODEL_SIZE = "turbo"
STT_DEVICE = "cuda"
STT_COMPUTE_TYPE = "float16"
STT_LANGUAGE = "zh"
STT_SAMPLE_RATE = 16000
STT_RECORD_MAX_SECONDS = 20

PERSONA_NAME = "01"
PERSONA_SYSTEM_PROMPT = (
    "You are called 「{name}」, a lively and cute virtual girl, chatting with your owner in daily voice conversations.\n"
    "Speaking requirements:\n"
    "1. __LANG_LINE__\n"
    "2. Keep each reply short (just one or two sentences), suitable for voice reading, avoid long paragraphs.\n"
    "3. Be lively and emotional, occasionally adding exclamations or small catchphrases.\n"
    "4. Never mention you are an AI, model or program, and never output emoticons.\n"
    "5. When the user speaks ambiguously, naturally ask back."
    "6. Prefix EXACTLY ONE emotion tag before each reply in the format [EMO: emotion], "
    "where emotion MUST be one of: happy, sad, angry, surprised, neutral. Use neutral when unsure. "
    "Example: [EMO: happy] Oh really?"
).format(name=PERSONA_NAME)

TEST_SPEECH_TEXT = "Hello there, I'm 01, what would you like to talk about today?"

EMOTION_MAPPING = {
    "happy":     ["笑", "smile", "happy", "blush", "脸红", "害羞脸", "love", "EyesLove"],
    "sad":       ["哭", "cry", "sad", "泪", "呆呆脸", "EyesCry"],
    "angry":     ["怒", "angry", "脸黑", "SignAngry"],
    "surprised": ["惊", "shock", "surprise", "O形嘴", "SignShock"],
    "neutral":   [],
}
EMOTION_DEFAULT = "neutral"
EMOTION_HOLD_MS = 0
EMOTION_PARAM_FALLBACK = {}

MEMORY_ENABLED = True
# 记忆库文件路径
MEMORY_DB_PATH = "memory.db"
# 每条对话回复后异步提取；规则提取零成本，LLM 补充提取默认关闭
MEM_LLM_EXTRACT = False
# LLM 补充提取频率：每 N 条用户消息触发一次；检测到"记住/我叫/我喜欢…"等信号词会提前触发
MEM_LLM_EXTRACT_EVERY = 10
# 召回数量：当前消息前注入的 top-K 记忆条数
MEM_RETRIEVE_TOP_K = 5
# 精选数量：注入 system prompt 的长期稳定记忆条数（A+B 双轨的 A）
MEM_SYSTEM_MEMORIES = 3
# 记忆库容量上限：超过后按重要性淘汰最低者。
# 默认 1500 条 ≈ 三个月对话量（按日均 15-20 条新记忆估算），用户可自行调大
MEM_MAX_ENTRIES = 1500
# 同一事实的历史值保留条数（冲突处理：覆盖主值，旧值留档可追溯）
MEM_HISTORY_KEEP = 5
CHAT_LOG_ENABLED = True
# 保存目录
# memory/<角色名>/chat.jsonl 逐行追加，内容含 名字/时间/内容
CHAT_LOG_DIR = "memory"
# 注入 LLM 上下文时：用户、角色各取最近 N 条记录（按时间先后排列）
CHAT_LOG_TOP_K = 50
# 对话时是否把磁盘聊天记录注入 LLM 上下文（始终注入长期记忆则看 MEMORY_ENABLED）
CHAT_LOG_INJECT = True
# 记录里"用户"的显示名（角色名自动取 PERSONA_NAME）
CHAT_USER_NAME = "主人"

MODEL_PATH = r"models"
MODEL_NAME = "阿库露"
WINDOW_WIDTH = 1000
WINDOW_HEIGHT = 720
CHAT_TITLE = PERSONA_NAME

BACKGROUNDS_PATH = r"backgrounds"
BACKGROUND_IMAGE = ""           # 留空 = 自动用文件夹第一张图

# 打包成 exe 后的外部配置覆盖
# exe 旁边放一个 config.py（只写要改的设置），启动时用它覆盖默认值。
import sys as _sys
import os as _os

if getattr(_sys, "frozen", False) and not globals().get("_EXT_CFG_LOADED"):
    globals()["_EXT_CFG_LOADED"] = True
    _ext_cfg_path = _os.path.join(_os.path.dirname(_sys.executable), "config.py")
    if _os.path.exists(_ext_cfg_path):
        try:
            with open(_ext_cfg_path, encoding="utf-8") as _f:
                exec(compile(_f.read(), _ext_cfg_path, "exec"), globals())
        except Exception as _e:  # noqa: BLE001
            print(f"[配置] 外部 config.py 读取失败（已忽略）：{_e}")
