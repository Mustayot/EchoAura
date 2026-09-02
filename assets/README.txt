把你的 GPT-SoVITS 参考音频（音色来源）放在这个文件夹里，例如：

    assets\ref.wav

然后在 config.py 里把 TTS_REF_AUDIO_PATH 改成它的绝对路径，
并用下面的命令自动生成提示文本：

    .\.venv\Scripts\python.exe tools\gen_prompt_text.py assets\ref.wav
