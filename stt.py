# -*- coding: utf-8 -*-
import io
import os
import glob
import sys

import numpy as np

try:
    import config as _cfg
    if getattr(_cfg, "HF_ENDPOINT", ""):
        os.environ.setdefault("HF_ENDPOINT", _cfg.HF_ENDPOINT)
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")  
except Exception:
    pass


def _add_nvidia_dll_paths():
    roots = []
    try:
        import site
        roots += site.getsitepackages()
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        roots.append(sys._MEIPASS)
    seen = set()
    for base in roots:
        for dll_dir in glob.glob(os.path.join(base, "nvidia", "*", "bin")):
            if dll_dir in seen:
                continue
            seen.add(dll_dir)
            try:
                os.add_dll_directory(dll_dir)
            except Exception:
                pass
            os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")


_add_nvidia_dll_paths()


class STTClient:


    def __init__(self, model_size: str = "turbo", device: str = "cuda",
                 compute_type: str = "float16", language: str = "zh"):
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise RuntimeError('faster-whisper 未安装。请用 STT_BACKEND="api" 连接外部 STT 服务')
        self.language = language
        self.device = device
        self.model_size = model_size
        self.compute_type = compute_type
        try:
            self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
            if device == "cuda":
                self._check_cuda()
            self.note = f"{model_size}@{device}/{compute_type}"
        except Exception as e:  # noqa: BLE001  GPU 不可用回退 CPU
            self._fallback_cpu(e)

    def _check_cuda(self):
        import ctranslate2
        if ctranslate2.get_cuda_device_count() < 1:
            raise RuntimeError("CUDA 设备不可用")

    def _fallback_cpu(self, err):
        self.device = "cpu"
        self.compute_type = "int8"
        self.model_size = fallback = "small" if self.model_size != "small" else "tiny"
        self.model = WhisperModel(fallback, device="cpu", compute_type="int8")
        self.note = f"{fallback}@cpu/int8（GPU 不可用已回退：{err}）"

    def transcribe(self, audio, sample_rate: int = 16000, use_vad: bool = True) -> str:
        """audio 可为 numpy 数组 / 文件路径 / bytes(或文件对象)，返回识别文本。"""
        if audio is None:
            return ""
        kw = dict(language=self.language, vad_filter=use_vad, beam_size=5)
        if isinstance(audio, np.ndarray):
            segments, _info = self.model.transcribe(audio, **kw)
        elif isinstance(audio, (str, os.PathLike)):
            segments, _info = self.model.transcribe(str(audio), **kw)
        else:
            if isinstance(audio, bytes):
                audio = io.BytesIO(audio)
            segments, _info = self.model.transcribe(audio, **kw)
        return "".join(s.text for s in segments).strip()


def transcribe_via_api(audio_bytes: bytes, url: str, response_field: str = "text",
                       timeout: float = 30.0) -> str:
    """把音频字节 POST 给外部 STT 服务，从返回 JSON 取识别文字。

    兼容 {response_field}/{text}/{transcript}/{result}/{data} 或纯文本返回。
    """
    import requests
    try:
        resp = requests.post(url, data=bytes(audio_bytes), timeout=timeout,
                             headers={"Content-Type": "application/octet-stream"})
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"连不上本地 STT 服务（{url}）：{e}")
    if resp.status_code != 200:
        raise RuntimeError(f"STT 服务返回错误（HTTP {resp.status_code}）：{resp.text[:200]}")
    try:
        data = resp.json()
    except Exception:
        return resp.text.strip()
    if isinstance(data, dict):
        for key in (response_field, "text", "transcript", "result", "data"):
            if data.get(key):
                return str(data[key]).strip()
    return ""


if __name__ == "__main__":
    import config
    stt = STTClient(config.STT_MODEL_SIZE, config.STT_DEVICE,
                    config.STT_COMPUTE_TYPE, config.STT_LANGUAGE)
    print(f"[OK] 语音识别模型已加载：{stt.note}")
    print("[识别结果]", stt.transcribe("test_input.wav"))
