# 语音通话模式：低延迟语音对话（不显示文字，共享记忆）

为本应用新增"语音通话模式"：常驻监听、用户开口即自动识别并尽快语音回复；转写与回复不显示在聊天面板、不落盘，但写入共享 LLM 对话上下文（角色记住通话内容，为长期记忆打底）。参考 OpenLLMVTuber 链路（VAD + 流式 ASR + 流式 LLM + 流式 TTS）。

## 决策（用户逐条确认）

- **触发**：常驻监听，前端 VAD 自动检测开口/停嘴（开口 130ms / 停嘴 520ms，能量 RMS + 迟滞）。
- **文本去向**（Q2'=A）：转写仅作 LLM 输入；不显示、不落盘；**写入共享 LLM 历史**（角色记得，长期记忆基础）。
- **打断**（Q3=是 + Q9=是）：开口即停 TTS 播放 + abort LLM 生成。
- **回声**（Q6=B）：播放时麦克风不停，浏览器 `echoCancellation` + 播放期 VAD 阈值放大 1.7× 防自我触发；实测严重再降级半双工。
- **入口**（Q7=A）：主界面「📞 通话」开关按钮。
- **会话**（Q8=B）：与文字聊天共用同一 LLM 实例，语音轮次并入聊天历史。
- **延迟目标**（Q4=A）：话停 → 语音首字 <1.5s（v1 已实现整段识别 + 逐句朗读，约 1.5–2.5s；二期用 partial 提前启动 LLM 冲 <1.5s）。
- **流式 ASR**（Q5=B + Q10）：`sherpa-onnx-streaming-paraformer-bilingual-zh-en`（int8，165MB+71MB）下载到独立的 STT 服务目录（用户本机任意路径，如 STT 服务所在目录 `models_stt\`），在该服务侧以 `start_stt.bat` 在 **9989** 启动（避开 9988 现有服务），新增 `/transcribe_stream` 接口。

## 实施（v1 已完成）

- 独立 STT 服务（sherpa-onnx `stt_server_sherpa.py`，存放于用户本机独立的 STT 服务目录）：新增 `get_stream_recognizer`（OnlineRecognizer.from_paraformer，优先 int8）、`recognize_stream`（0.3s 分块增量解码）、`/transcribe_stream`；`start_server` 增加 `stream_model_dir` 参数（缺省兼容旧调用）。
- `config.py`：新增 `STT_STREAM_API_URL = "http://127.0.0.1:9989/transcribe_stream"`。
- `app_server.py`：新增 `POST /api/call/transcribe`（代理到流式接口，未配置时从 STT_API_URL 推导）。
- 前端 `web/`：`📞 通话` 按钮；WebAudio 采集（echoCancellation）→ 能量 VAD 状态机 → WAV 编码 → `/api/call/transcribe` → 无声流式对话（`/api/chat/stream`，历史后端提交）→ 按句末标点逐句 TTS 播放 → 打断（停播放 + abort）。
- 已知事实：本机 9988 是另一 ffmpeg/Cohere 系 STT 服务（非 stt_server_sherpa.py），故流式服务独立于 9989。

## 待办（二期）

1. 真流式 partial ASR（边说边出）→ LLM 在用户说完前提前启动 → 冲 <1.5s。
2. GPT-SoVITS streaming_mode 逐包转发（当前仍逐句整段合成）。
3. 回声严重时：Web Audio 增强 AEC / 半双工降级。
