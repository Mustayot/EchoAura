# -*- coding: utf-8 -*-
"""
自动生成 GPT-SoVITS 参考音频的 prompt_text
------------------------------------------
GPT-SoVITS 要求 ref_audio_path 的音频内容和 prompt_text 严格一致，否则音色会变差。
本工具用 faster-whisper 对你的参考音频做一次语音识别，自动把识别文字打印出来，
复制到 config.py 的 TTS_PROMPT_TEXT 即可。

用法：
    python tools/gen_prompt_text.py <参考音频路径> [模型大小]
示例：
    python tools/gen_prompt_text.py assets/ref.wav
"""
import sys
import os

# 让脚本能 import 到项目根目录的 config / stt 模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from stt import STTClient


def main():
    if len(sys.argv) < 2:
        print("用法：python tools/gen_prompt_text.py <参考音频路径> [模型大小=turbo]")
        sys.exit(1)
    audio_path = sys.argv[1]
    model_size = sys.argv[2] if len(sys.argv) > 2 else config.STT_MODEL_SIZE

    print(f"[1/2] 加载语音识别模型（{model_size}，首次会下载模型文件）…")
    stt = STTClient(model_size, config.STT_DEVICE, config.STT_COMPUTE_TYPE,
                    config.STT_LANGUAGE)
    print(f"      模型就绪：{stt.note}")

    print(f"[2/2] 识别参考音频：{audio_path}")
    # faster-whisper 会自动解码音频并重采样到 16k
    text = stt.transcribe(audio_path)
    print()
    print("=" * 60)
    print("把下面这行文字复制到 config.py 的 TTS_PROMPT_TEXT = 后面：")
    print()
    print(f'    TTS_PROMPT_TEXT = "{text}"')
    print()
    print("=" * 60)
    print(f"（共 {len(text)} 字）如果识别有错字，请手动改正后再填进 config.py。")


if __name__ == "__main__":
    main()
