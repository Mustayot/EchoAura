# -*- coding: utf-8 -*-
"""
GPT-SoVITS 调用
"""
import io
import re
import wave

import numpy as np
import requests

# 文字语言检测：按脚本（汉字/假名/谚文/拉丁）占比判定 en/zh/ja/ko。
# 纯标点/数字回落 None，由调用方用 config.TTS_TEXT_LANG 兜底。
# GPT-SoVITS 的 text_lang 必须与文本语言一致——英文文本用中文前端会出错
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_HIRA = re.compile(r"[\u3040-\u309f]")
_KATA = re.compile(r"[\u30a0-\u30ff]")
_HANG = re.compile(r"[\uac00-\ud7a3]")
_LATIN = re.compile(r"[A-Za-z]")


def detect_text_lang(text: str):
    """返回文本最可能是哪种语言：'en' / 'zh' / 'ja' / 'ko'，无法判断返回 None。"""
    if not text:
        return None
    latin = len(_LATIN.findall(text))
    cjk = len(_CJK.findall(text))
    kana = len(_HIRA.findall(text)) + len(_KATA.findall(text))
    hang = len(_HANG.findall(text))
    if latin + cjk + kana + hang == 0:
        return None  # 纯标点/数字，交给调用方兜底
    # 占比最高的脚本决定语言；拉丁字母占优（或唯一非零）即英文前端
    if latin > 0 and latin >= max(cjk, kana, hang):
        return "en"
    if cjk > 0 and cjk >= max(latin, kana, hang):
        return "zh"
    if kana > 0 and kana >= max(latin, cjk, hang):
        return "ja"
    if hang > 0:
        return "ko"
    return "en"


def synthesize(gpt_sovits_url: str, text: str,
               ref_audio_path: str, prompt_text: str,
               prompt_lang: str = "zh", text_lang: str = "zh",
               text_split_method: str = "cut5",
               media_type: str = "wav",
               streaming_mode: bool = False,
               timeout: float = 120.0) -> bytes:
    """把 text 合成成 wav 音频字节。失败抛 RuntimeError。"""
    if not text or not text.strip():
        raise ValueError("要合成的文本不能为空")

    payload = {
        "text": text,
        "text_lang": text_lang,
        "ref_audio_path": ref_audio_path,
        "prompt_text": prompt_text,
        "prompt_lang": prompt_lang,
        "text_split_method": text_split_method,
        "media_type": media_type,
        "streaming_mode": streaming_mode,
    }
    try:
        resp = requests.post(f"{gpt_sovits_url}/tts", json=payload, timeout=timeout)
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(
            f"连不上 GPT-SoVITS（{gpt_sovits_url}）。请确认已运行 python api_v2.py -p 9880"
        ) from e

    if resp.status_code != 200:
        raise RuntimeError(f"TTS 合成失败（HTTP {resp.status_code}）：{resp.text[:300]}")
    return resp.content


def wav_bytes_to_array(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    """把 wav 字节解析成 (float32 单声道音频数组, 采样率)，用于 sounddevice 播放。"""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        sample_rate = w.getframerate()
        n_channels = w.getnchannels()
        n_frames = w.getnframes()
        raw = w.readframes(n_frames)
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if n_channels > 1:
        data = data.reshape(-1, n_channels).mean(axis=1)
    return data, sample_rate


if __name__ == "__main__":
    # 命令行独立测试：合成一句测试语音并保存成 test_output.wav
    import config

    audio = synthesize(
        config.GPT_SOVITS_URL,
        config.TEST_SPEECH_TEXT,
        config.TTS_REF_AUDIO_PATH,
        config.TTS_PROMPT_TEXT,
        config.TTS_PROMPT_LANG,
        config.TTS_TEXT_LANG,
        config.TTS_TEXT_SPLIT_METHOD,
        config.TTS_MEDIA_TYPE,
        config.TTS_STREAMING,
    )
    with open("test_output.wav", "wb") as f:
        f.write(audio)
    print(f"[OK] 已生成 test_output.wav（{len(audio)} 字节）。可以播放验证音色。")
