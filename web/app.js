const $ = (id) => document.getElementById(id);

/* 隐私模式下 localStorage 可能不可用，统一 try-catch 封装 */
function storeGet(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }
function storeSet(key, val) { try { localStorage.setItem(key, val); } catch (e) {} }

let loadingMsg = null;
let servicesReady = false;

/* ===== 模型参数自动识别（PARAM_* / Param* 两套体系通用） ===== */
let P = {};
function resolveParams(core) {
  const ids = (core._parameterIds || []).map((id) => id.toString());
  const pick = (cands) => cands.find((n) => ids.indexOf(n) >= 0) || null;
  const eyeX = pick(["ParamEyeBallX", "PARAM_EYE_BALL_X"]);
  const eyeY = pick(["ParamEyeBallY", "PARAM_EYE_BALL_Y"]);
  const eyeX2 = [pick(["PARAM_L_EYE_BALL_X"]), pick(["PARAM_R_EYE_BALL_X"])].filter(Boolean);
  const eyeY2 = [pick(["PARAM_L_EYE_BALL_Y"]), pick(["PARAM_R_EYE_BALL_Y"])].filter(Boolean);
  P = {
    mouth:  pick(["ParamMouthOpenY", "PARAM_MOUTH_OPEN_Y", "MouthOpen", "ParamMouthOpen"]),
    eyeL:   pick(["ParamEyeLOpen", "PARAM_EYE_L_OPEN"]),
    eyeR:   pick(["ParamEyeROpen", "PARAM_EYE_R_OPEN"]),
    breath: pick(["ParamBreath", "PARAM_BREATH", "Breath"]),
    angleX: pick(["ParamAngleX", "PARAM_ANGLE_X"]),
    angleY: pick(["ParamAngleY", "PARAM_ANGLE_Y"]),
    bodyX:  pick(["ParamBodyAngleX", "PARAM_BODY_ANGLE_X"]),
    eyeBX:  eyeX || (eyeX2.length ? eyeX2 : null),
    eyeBY:  eyeY || (eyeY2.length ? eyeY2 : null),
  };
}

/* 设置 Live2D 参数（自动跳过不存在的参数） */
function setParam(ids, value) {
  if (!ids) return;
  const list = Array.isArray(ids) ? ids : [ids];
  try {
    const core = model && model.internalModel && model.internalModel.coreModel;
    if (!core) return;
    for (const id of list) core.setParameterValueById(id, value);
  } catch (e) { /* 忽略 */ }
}

let app = null, model = null;

/* ===== 情绪表情：标准情绪 -> 当前模型真实表情名 ===== */
let AVAILABLE_EXPRESSIONS = [];   // 当前模型可用表情名（model3 注入的 Expressions）
let EMOTION_MAP = {};             // 标准情绪 -> 候选表情名（来自后端 config）
let _emoTimer = null;

function setEmotionMap(map) { EMOTION_MAP = map || {}; }

// 把标准情绪解析为当前模型真实存在的表情名；无则返回 null（降级 neutral，静默跳过）
function resolveEmotion(emotion) {
  if (!emotion || emotion === "neutral") return null;
  const cands = EMOTION_MAP[emotion] || EMOTION_MAP[String(emotion).toLowerCase()] || [];
  for (const name of cands) {
    if (AVAILABLE_EXPRESSIONS.indexOf(name) >= 0) return name;
  }
  return null;
}

// 从文本抹掉 [EMO: x] 及任何残缺形式（[EM / [EMO / [EMO: 缺 ]）。
// 两道：先去含 ] 的（含]残缺也去），再去串尾孤立的 [EM 碎片（无]才到串尾）。
function stripEmo(text) {
  if (!text) return text;
  return (text + "")
    .replace(/\[EM[^\]]*?\]/gi, "")
    .replace(/\[EM[^\]]*$/gi, "")
    .replace(/\s{2,}/g, " ")
    .trim();

}// 用于聊天显示与 TTS 朗读文本，避免 * _ 之类符号混入界面或被念出来。
function cleanReply(text) {
  if (!text) return text;
  let t = stripEmo(text);
  try {
    const lead = new RegExp("^\\s*" + (CHAR_NAME || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*[：:]");
    t = t.replace(lead, "");
  } catch (e) { /* CHAR_NAME 异常则跳过前缀清洗 */ }
  t = t.replace(/\*([^*]+)\*/g, "$1").replace(/_([^_]+)_/g, "$1");  // *斜体* _粗体_ -> 文字
  return t.replace(/\s{2,}/g, " ").trim();
}

// 流式分块清洗：只去 EMO 残留与 Markdown 符号，**不做 trim、不折叠空格**。
// 关键：分块常以空格结尾（"Hello "），trim 会吃掉块尾空格导致与下一块单词黏连
// （"Hello"+"there" -> "Hellothere"）。整句 trim 交给 cleanReply（打字机收尾时做）。
function cleanStreamChunk(text) {
  if (!text) return text;
  return (text + "")
    .replace(/\[EM[^\]]*?\]/gi, "")
    .replace(/\[EM[^\]]*$/gi, "")
    .replace(/\*([^*]+)\*/g, "$1").replace(/_([^_]+)_/g, "$1");
}

// 采集当前模型可用表情名：优先 expressionManager.definitions，
// 兜底从 model3 的 FileReferences.Expressions 直接取（与后端 augment 注入一致）。
function collectExpressions() {
  AVAILABLE_EXPRESSIONS = [];
  try {
    const em = model && model.internalModel && model.internalModel.motionManager
               && model.internalModel.motionManager.expressionManager;
    if (em && em.definitions && em.definitions.length) {
      AVAILABLE_EXPRESSIONS = em.definitions.map((d) => d.Name || d.name).filter(Boolean);
    }
  } catch (e) { console.warn("[表情] 采集 definitions 失败：", e); }
  if (!AVAILABLE_EXPRESSIONS.length) {
    try {
      const m3 = (model.internalModel && model.internalModel.model3Json) || model.model3Json;
      const fr = (m3 && m3.FileReferences) || {};
      AVAILABLE_EXPRESSIONS = (fr.Expressions || []).map((e) => (e && (e.Name || e.name)) || "").filter(Boolean);
    } catch (e) {}
  }
  console.log("[DEBUG] 可用表情列表：", AVAILABLE_EXPRESSIONS);
}

// 播放情绪表情。该库无 model.expression()，必须走底层 expressionManager.setExpression。
function playEmotion(emotion) {
  if (!model || !emotion || emotion === "neutral") return;
  if (!AVAILABLE_EXPRESSIONS.length) collectExpressions();  // 兜底：表情管理器晚于采集时再取一次
  const name = resolveEmotion(emotion);
  if (!name) {
    return;  // 模型无对应表情 -> 静默降级，不阻断对话
  }
  try {
    const em = model.internalModel.motionManager.expressionManager;
    if (_emoTimer) { clearTimeout(_emoTimer); _emoTimer = null; }
    if (typeof model.expression === "function") model.expression(name);
    else em.setExpression(name);
    console.log("[表情] 播放：", name, "（情绪", emotion + "）");
    const hold = window.__EMOTION_HOLD_MS || 0;
    if (hold > 0) {
      _emoTimer = setTimeout(() => {
        try { if (typeof em.resetExpression === "function") em.resetExpression(); } catch (e) {}
        _emoTimer = null;
      }, hold);
    }
  } catch (e) {
    console.warn("[表情播放失败]", name, e);
  }
}
let audioCtx = null;
let talking = false;
let mediaRecorder = null, chunks = [];
let mouth = 0;                          // 当前嘴型值 0~1
let CHAR_NAME = "01";
let REPLY_LANG = "en";   // 当前回复语言（默认英文），运行时生效，存 localStorage
// 语言徽章文案（与下拉 value 一一对应）
const LANG_LABELS = { en: "EN", zh: "中文" };
// 回复语言选项：value(发给后端) <-> 显示文案（毛玻璃下拉按文案展示）
const LANG_OPTIONS = [["en", "English"], ["zh", "中文"]];
function langLabel(code) { const f = LANG_OPTIONS.find(([c]) => c === code); return f ? f[1] : "English"; }
function langCode(label) { const f = LANG_OPTIONS.find(([, l]) => l === label); return f ? f[0] : "en"; }
function updateLangBadge() {
  const b = $("lang-badge");
  if (b) b.textContent = LANG_LABELS[REPLY_LANG] || "EN";
}
// 各语言测试语音文案（测试按钮用），与后端 GPT-SoVITS text_lang 对应
const TEST_PHRASES = {
  en: "Hello there, I'm 01, what would you like to talk about today?",
  zh: "你好呀，我是 01，今天想聊点什么呢？",
};
let FEATURES = { streaming: false, stream_tts: false, stream_tts_chunk: 0 };  // 由 /api/features 下发
let _tw = null;                          // 当前打字机（新消息打断旧消息用）

/* ===== 模型设置（自动保存） ===== */
const DEFAULT_SETTINGS = { zoom: true, drag: true, scale: 1.0, posX: 0.70, posY: 0.60 };
let S = Object.assign({}, DEFAULT_SETTINGS);

function computeDefaultPos() {
  const panel = document.querySelector("#panel");
  const right = panel ? panel.getBoundingClientRect().right : window.innerWidth * 0.42;
  const cx = (right + window.innerWidth) / 2 / window.innerWidth;
  return { posX: Math.min(0.9, Math.max(0.3, cx)), posY: 0.55 };
}
function loadSettings() {
  try {
    const s = JSON.parse(storeGet("vt_settings") || "null");
    S = Object.assign({}, DEFAULT_SETTINGS, s ? s : computeDefaultPos());
  } catch (e) { S = Object.assign({}, DEFAULT_SETTINGS); }
}
function saveSettings() { storeSet("vt_settings", JSON.stringify(S)); }

/* 提示气泡：右上（不挡设置按钮） */
let toastTimer = null;
function showToast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.style.bottom = "auto";
  t.style.top = "56px";
  t.style.right = "18px";
  t.style.left = "auto";
  t.style.maxWidth = "300px";
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 4000);
}

function addMsg(who, text, cls) {
  const chat = $("chat");
  const d = document.createElement("div");
  d.className = "msg " + cls;
  const w = document.createElement("span");
  w.className = "who";
  w.textContent = who + "：";
  d.appendChild(w);
  d.appendChild(document.createTextNode(stripEmo(text)));
  chat.appendChild(d);
  chat.scrollTop = chat.scrollHeight;
  return d;
}

/* 背景图 */
async function applyBackground() {
  try {
    const bg = await fetch("/api/background").then((r) => r.json());
    if (bg && bg.url) {
      document.body.style.setProperty("--bg-image", `url(${bg.url})`);
      document.body.classList.add("has-bg");
    }
  } catch (e) { /* 用默认背景 */ }
}

/* 服务状态灯 */
function setDot(id, ok) {
  const el = $(id);
  if (el) el.className = "dot " + (ok ? "on" : "off");
}
const READY_PLACEHOLDER = "想对01说点什么…（回车发送）";
const LOADING_PLACEHOLDER = "加载中…";
function setControlsEnabled(ready) {
  const input = $("input"), send = $("btn-send"), talk = $("btn-talk"), test = $("btn-test"), call = $("btn-call");
  [input, send, talk, test, call].forEach((el) => { if (el) el.disabled = !ready; });
  if (input) input.placeholder = ready ? READY_PLACEHOLDER : LOADING_PLACEHOLDER;
}
async function pollStatus() {
  try {
    const s = await fetch("/api/status").then((r) => r.json());
    setDot("dot-llm", !!s.llm);
    setDot("dot-tts", !!s.tts);
    setDot("dot-stt", !!s.stt);
    const ready = !!(s.llm && s.tts && s.stt);
    if (ready && !servicesReady) {
      servicesReady = true;
      if (loadingMsg && loadingMsg.parentNode) {
        loadingMsg.parentNode.removeChild(loadingMsg);
        loadingMsg = null;
        addMsg("系统", "所有服务已就绪", "sys");
      }
    } else if (!ready && servicesReady) {
      servicesReady = false;
      addMsg("系统", "部分服务已断开，等待恢复…", "sys");
    }
    setControlsEnabled(ready);
  } catch (e) { /* 服务器未响应，忽略 */ }
}
setInterval(pollStatus, 8000);
pollStatus();
loadEmotionConfig();

async function loadTitle() {
  try { const t = await fetch("/api/title").then((r) => r.json()); if (t.title) CHAR_NAME = t.title; } catch (e) {}
}

/* 设置面板开合动画 */
function showPanel(el) {
  clearTimeout(el._hideTimer);
  el.classList.remove("hidden");
  requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add("show")));
}
function hidePanel(el) {
  el.classList.remove("show");
  clearTimeout(el._hideTimer);
  el._hideTimer = setTimeout(() => { if (!el.classList.contains("show")) el.classList.add("hidden"); }, 320);
}
function anySettingsOpen() {
  return !$("settings-panel").classList.contains("hidden") || !$("cfg-panel").classList.contains("hidden");
}

function bindSettingsUI() {
  $("btn-settings").addEventListener("click", () => {
    const p = $("settings-panel");
    if (p.classList.contains("hidden")) showPanel(p); else hidePanel(p);
  });
  $("sp-close").addEventListener("click", () => hidePanel($("settings-panel")));
  $("sp-zoom").checked = S.zoom;
  $("sp-zoom").addEventListener("change", () => { S.zoom = $("sp-zoom").checked; saveSettings(); });
  $("sp-drag").checked = S.drag;
  $("sp-drag").addEventListener("change", () => { S.drag = $("sp-drag").checked; saveSettings(); });
  const range = (id, key) => {
    $(id).value = S[key];
    $(id).addEventListener("input", () => {
      S[key] = parseFloat($(id).value); saveSettings();
      if (model) positionModel();
    });
  };
  range("sp-scale", "scale");
  $("sp-reset").addEventListener("click", () => {
    S = Object.assign({}, DEFAULT_SETTINGS, computeDefaultPos());
    saveSettings();
    $("sp-zoom").checked = S.zoom;
    $("sp-drag").checked = S.drag;
    $("sp-scale").value = S.scale;
    if (model) positionModel();
  });

  // 清空聊天（二次确认）
  let clearArmed = false, clearTimer = null;
  $("sp-clear").addEventListener("click", () => {
    if (!clearArmed) {
      clearArmed = true;
      $("sp-clear").textContent = "确认清空？";
      $("sp-clear").classList.add("confirm");
      clearTimeout(clearTimer);
      clearTimer = setTimeout(() => {
        clearArmed = false;
        $("sp-clear").textContent = "清空聊天记录";
        $("sp-clear").classList.remove("confirm");
      }, 3000);
      return;
    }
    clearArmed = false;
    $("sp-clear").textContent = "清空聊天记录";
    $("sp-clear").classList.remove("confirm");
    clearTimeout(clearTimer);
    $("chat").innerHTML = "";
    fetch("/api/clear_chat", { method: "POST" }).catch(() => {});
    addMsg("系统", "聊天界面已清空（历史记录仍保存在 memory 文件夹，AI 记得之前的对话）", "sys");
  });
}

let modelCombo = null;

async function loadLive2dModels(silent) {
  let names = [];
  try {
    const d = await fetch("/api/models").then((r) => r.json());
    names = (d.models || []).map((m) => m.name);
  } catch (e) { /* 下拉保持现状 */ }
  if (!modelCombo) return names;
  const cur = modelCombo.wrap.dataset.value || currentModelName;
  try {
    modelCombo.setOptions(names);
    if (names.indexOf(cur) >= 0) modelCombo.setValue(cur, silent);
    else if (names.length) modelCombo.setValue(names[0], silent);
  } catch (e) { /* 下拉异常不影响返回 */ }
  return names;
}

function initModelCombo() {
  const host = $("sp-model");
  if (!host) return;
  host.innerHTML = "";
  modelCombo = createGlassSelect([], "", false, async (name) => {
    if (!name || name === currentModelName) return;
    showToast("切换模型：" + name + "…");
    const ok = await loadModel(name);
    if (ok) {
      showToast("已切换模型：" + name);
      loadLive2dModels(true);
    } else {
      if (modelCombo) modelCombo.setValue(currentModelName || "", true);
    }
  });
  host.appendChild(modelCombo.wrap);
  $("sp-model-refresh").addEventListener("click", async () => {
    const names = await loadLive2dModels();
    showToast("模型列表已刷新（" + ((names || []).length ? names.join("、") : "空") + "）");
  });
  loadLive2dModels(true);
}

function bindZoom() {
  window.addEventListener("wheel", (e) => {
    if (!S.zoom || !model || anySettingsOpen()) return;
    e.preventDefault();
    S.scale = Math.min(2.0, Math.max(0.3, S.scale * (e.deltaY < 0 ? 1.06 : 0.94)));
    $("sp-scale").value = S.scale;
    saveSettings();
    positionModel();
  }, { passive: false });
}

/* 鼠标拖动模型 */
let _drag = { on: false, sx: 0, sy: 0, px: 0, py: 0 };
function bindDrag() {
  const el = app.view;
  el.addEventListener("pointerdown", (e) => {
    if (!S.drag || !model) return;
    _drag.on = true;
    _drag.sx = e.clientX; _drag.sy = e.clientY;
    _drag.px = S.posX;    _drag.py = S.posY;
    try { el.setPointerCapture(e.pointerId); } catch (err) {}
  });
  el.addEventListener("pointermove", (e) => {
    if (!_drag.on) return;
    S.posX = Math.min(1.3, Math.max(-0.3, _drag.px + (e.clientX - _drag.sx) / window.innerWidth));
    S.posY = Math.min(1.3, Math.max(-0.3, _drag.py + (e.clientY - _drag.sy) / window.innerHeight));
    saveSettings();
    positionModel();
  });
  const end = () => { _drag.on = false; };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
}

/* ---------------- Live2D 模型 ---------------- */
let lastModelUrl = null;
let lastModelName = "";
let currentModelName = "";

async function initLive2D() {
  // 关键兼容配置：让动作里的 PartOpacity 曲线驱动部件透明度（否则 hiyori 两套手臂都显示）
  try {
    if (PIXI.live2d && PIXI.live2d.CubismConfig) {
      PIXI.live2d.CubismConfig.setOpacityFromMotion = true;
    }
  } catch (e) { /* 旧库无此配置则忽略 */ }

  app = new PIXI.Application({
    resizeTo: window,
    backgroundAlpha: 0,
    antialias: true,
    autoStart: false,
    resolution: Math.min(window.devicePixelRatio || 1, 2),
    autoDensity: true,
  });
  $("stage").appendChild(app.view);
  window.__app = app;

  window.addEventListener("resize", () => { if (model) positionModel(); });
  bindDrag();
  window.addEventListener("pointermove", onPointerMove);
  setInterval(blink, 2600 + Math.random() * 3200);

  // 动画循环：rAF 主循环 + setInterval 兜底（页面隐藏时 rAF 停摆，后台也动）
  let lastT = performance.now();
  let rafOK = false;
  const stepAnim = (now) => {
    if (!model || !model.internalModel) return;
    const dt = Math.max(1, Math.min(100, now - lastT));
    lastT = now;
    const t = now / 1000;
    setParam(P.mouth, mouth);
    setParam(P.breath, 0.5 + 0.5 * Math.sin(t * 1.6));
    setParam(P.bodyX, 2.5 * Math.sin(t * 0.7));
    if (model.update) model.update(dt);
    app.renderer.render(app.stage);
  };
  const rafLoop = (now) => { rafOK = true; stepAnim(now); requestAnimationFrame(rafLoop); };
  requestAnimationFrame(rafLoop);
  setInterval(() => { if (!rafOK) stepAnim(performance.now()); rafOK = false; }, 33);

  window.__setMouth = (v) => { mouth = Math.max(0, Math.min(1, v)); };

  const ok = await loadModel();
  if (!ok && lastModelUrl) showToast("模型加载失败，请尝试其他模型");
}

async function loadModel(name) {
  let res;
  try {
    const q = name ? "?name=" + encodeURIComponent(name) : "";
    res = await fetch("/api/model" + q).then((r) => r.json());
  } catch (e) {
    showToast("连不上本地服务器");
    return false;
  }
  if (res.error) { showToast(res.error); return false; }
  const ok = await loadModelByUrl(res.url, name || res.name);
  if (ok && modelCombo && modelCombo.wrap.dataset.value !== currentModelName) {
    modelCombo.setValue(currentModelName, true);
  }
  return ok;
}

/* 按 URL 加载模型（可重复调用=切换），失败回退上一个，序列令牌防并发叠加 */
let _modelLoadSeq = 0;

async function loadModelByUrl(url, nameForDisplay) {
  const seq = ++_modelLoadSeq;
  if (model) {
    try { model.off("hit"); } catch (e) {}
    try { app.stage.removeChild(model); } catch (e) {}
    try { model.destroy(); } catch (e) {}
    model = null;
  }
  let m;
  try {
    m = await PIXI.live2d.Live2DModel.from(url, { autoInteract: true, autoUpdate: false });
  } catch (e) {
    if (lastModelUrl && lastModelUrl !== url) {
      showToast("模型加载失败，已回退上一个模型");
      return loadModelByUrl(lastModelUrl, lastModelName);
    }
    showToast("模型加载失败：" + (e && e.message ? e.message : e));
    return false;
  }
  if (seq !== _modelLoadSeq) {
    try { m.destroy(); } catch (e) {}
    return false;
  }
  model = m;
  collectExpressions();
  lastModelUrl = url;
  if (nameForDisplay) { currentModelName = nameForDisplay; lastModelName = nameForDisplay; }
  app.stage.addChild(model);
  window.__model = model;
  resolveParams(model.internalModel.coreModel);
  positionModel();
  playIdle();
  model.on("hit", (areas) => {
    if (!areas || !areas.length) return;
    try {
      const names = (model.internalModel && model.internalModel.motionManager && model.internalModel.motionManager.definitionNames) || [];
      const hit = ["Tap", "TapBody", "Touch", "FlickHead", "Poke", "hit"].find((n) => names.indexOf(n) >= 0);
      if (hit) model.motion(hit);
    } catch (e) { /* 忽略单帧点击异常 */ }
  });
  return true;
}

let baseScale = 1;
function computeBaseScale() {
  try {
    const iw = model.internalModel.width || model.width;
    const ih = model.internalModel.height || model.height;
    baseScale = Math.min(app.renderer.width / iw, app.renderer.height / ih) * 0.92;
  } catch (e) { baseScale = 1; }
}
function positionModel() {
  computeBaseScale();
  model.anchor.set(0.5, 0.5);
  model.scale.set(baseScale * S.scale);
  model.position.set(app.renderer.width * S.posX, app.renderer.height * S.posY);
}

function playIdle() {
  if (!model) return;
  try {
    const names = model.internalModel.motionManager.definitionNames || [];
    const hit = ["Idle", "idle", "idle00", "Idle_2", "main"].find((n) => names.indexOf(n) >= 0);
    if (hit) model.motion(hit);
  } catch (e) { /* 无待机则静立 */ }
  try {
    model.once("motionFinish", () => setTimeout(playIdle, 1500));
  } catch (e) { /* 忽略 */ }
}

function onPointerMove(e) {
  if (!model || !model.internalModel) return;
  const nx = (e.clientX / window.innerWidth) * 2 - 1;
  const ny = (e.clientY / window.innerHeight) * 2 - 1;
  setParam(P.angleX, nx * 10);
  setParam(P.angleY, -ny * 6);
  setParam(P.eyeBX, nx);
  setParam(P.eyeBY, -ny);
  setParam(P.bodyX, nx * 4);
}

function blink() {
  if (!model || !model.internalModel || mouth > 0.3) return;  // 说话时不眨眼
  setParam(P.eyeL, 0);
  setParam(P.eyeR, 0);
  setTimeout(() => {
    setParam(P.eyeL, 1);
    setParam(P.eyeR, 1);
  }, 150);
}

async function getAudioCtx() {
  if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  if (audioCtx.state === "suspended") await audioCtx.resume();
  return audioCtx;
}

async function speak(text) {
  if (!text) return;
  let resp;
  try {
    resp = await fetch("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, lang: REPLY_LANG }),
    });
  } catch (e) { addMsg("系统", "连不上 TTS：请确认 GPT-SoVITS 已启动", "sys"); return; }
  if (!resp.ok) {
    let msg = "TTS 失败（HTTP " + resp.status + "）";
    try { msg = (await resp.json()).error || msg; } catch (e) {}
    addMsg("系统", msg, "sys");
    return;
  }
  try {
    const buf = await resp.arrayBuffer();
    const ctx = await getAudioCtx();
    const audioBuf = await ctx.decodeAudioData(buf);
    const src = ctx.createBufferSource();
    src.buffer = audioBuf;
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    src.connect(analyser);
    analyser.connect(ctx.destination);
    const data = new Uint8Array(analyser.fftSize);
    const mouthTimer = setInterval(() => {
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) { const v = (data[i] - 128) / 128; sum += v * v; }
      mouth = Math.min(1, Math.sqrt(sum / data.length) * 5);
    }, 50);
    return await new Promise((resolve) => {
      src.onended = () => { mouth = 0; clearInterval(mouthTimer); resolve(); };
      src.start();
    });
  } catch (e) {
    addMsg("系统", "音频播放出错：" + (e.message || e), "sys");
  }
}

const CALL = {
  active: false, stream: null, ctx: null, srcNode: null, proc: null,
  speech: false, voiceMs: 0, silentMs: 0, samples: [],
  bargeIn: false, playing: false, audioQ: [], src: null, aborter: null,
};
const CALL_CFG = {
  rate: 16000, buf: 2048,
  openMs: 130,          // 出声超 130ms 判定开口
  thrOpen: 0.02,        // 开口 RMS 阈值
  thrClose: 0.009,      // 结束 RMS 阈值（迟滞）
  boostPlaying: 1.7,    // 角色播放时阈值放大（防回声误触发，OpenLLMVTuber 同思路）
};
// VAD 停嘴超时(ms)：静音超此值判定"说完"。由 config.VAD_SILENCE_TIMEOUT_MS 下发（默认 2500）
let VAD_SILENCE_TIMEOUT_MS = 2500;
async function loadVadConfig() {
  try {
    const cfg = await fetch("/api/config").then((r) => r.json());
    if (typeof cfg.VAD_SILENCE_TIMEOUT_MS === "number") VAD_SILENCE_TIMEOUT_MS = cfg.VAD_SILENCE_TIMEOUT_MS;
  } catch (e) { /* 服务器未响应，用默认值 */ }
}

function encodeWav(samples, sampleRate) {
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buf);
  const ws = (o, s) => { for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i)); };
  ws(0, "RIFF"); view.setUint32(4, 36 + samples.length * 2, true); ws(8, "WAVE");
  ws(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, 1, true); view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  ws(36, "data"); view.setUint32(40, samples.length * 2, true);
  let off = 44;
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true); off += 2;
  }
  return new Blob([buf], { type: "audio/wav" });
}

function _callProcess(e) {
  const rate = e.inputBuffer.sampleRate || 48000;
  const step = Math.max(1, Math.round(rate / CALL_CFG.rate));
  const inData = e.inputBuffer.getChannelData(0);
  const n = Math.floor(inData.length / step);
  let sum = 0;
  for (let i = 0; i < n; i++) { const v = inData[i * step]; sum += v * v; }
  const rms = Math.sqrt(sum / Math.max(1, n));
  const frameMs = (CALL_CFG.buf / rate) * 1000;
  const thr = CALL.playing ? CALL_CFG.thrOpen * CALL_CFG.boostPlaying : CALL_CFG.thrOpen;
  if (rms > thr) {
    CALL.silentMs = 0; CALL.voiceMs += frameMs;
    if (!CALL.speech && CALL.voiceMs >= CALL_CFG.openMs) _callSpeechStart();
  } else if (CALL.speech) {
    CALL.voiceMs = 0; CALL.silentMs += frameMs;
    if (CALL.silentMs >= VAD_SILENCE_TIMEOUT_MS) _callSpeechEnd();
  }
  if (CALL.speech) for (let i = 0; i < n; i++) CALL.samples.push(inData[i * step]);
}

function _callSpeechStart() {
  CALL.speech = true;
  CALL.samples = [];
  // 打断（barge-in）：角色正在说 / LLM 正在生成 → 立即停，本句从这次开口重新计
  if (CALL.playing || CALL.aborter || CALL.audioQ.length) {
    CALL.bargeIn = true;
    _callStopPlayback();
    CALL.bargeIn = false;
  }
}

function _callSpeechEnd() {
  CALL.speech = false;
  const samples = CALL.samples; CALL.samples = [];
  if (CALL.active && samples.length >= CALL_CFG.rate * 0.4) _callTranscribe(encodeWav(samples, CALL_CFG.rate));
}

function _callStopPlayback() {
  CALL.audioQ.length = 0;
  if (CALL.src) { try { CALL.src.stop(); } catch {} CALL.src = null; }
  if (CALL.aborter) { try { CALL.aborter.abort(); } catch {} CALL.aborter = null; }
}

async function _callTranscribe(wav) {
  try {
    const resp = await fetch("/api/call/transcribe", {
      method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: wav,
    });
    const data = await resp.json();
    if (data.error) { showToast("识别失败：" + data.error); return; }
    if (data.text) await callChat(data.text);
    else showToast("没听清，再说一次");
  } catch (e) { showToast("识别失败：" + (e.message || e)); }
}

// 无声流式对话：走 /api/chat/stream（LLM 历史由后端提交），不显示面板文本
async function callChat(text) {
  if (!CALL.active || !text) return;
  CALL.aborter = new AbortController();
  const signal = CALL.aborter.signal;
  try {
    const resp = await fetch("/api/chat/stream", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, lang: REPLY_LANG, source: "voice" }), signal,
    });
    if (!resp.ok || !resp.body) { showToast("通话请求失败（HTTP " + resp.status + "）"); return; }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "", sentence = "", emotion = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const line = frame.replace(/^data:\s?/, "").trim();
        if (!line) continue;
        let obj; try { obj = JSON.parse(line); } catch { continue; }
        if (obj.error) { showToast("通话出错：" + obj.error); return; }
        if (obj.emotion && obj.emotion !== emotion) { emotion = obj.emotion; playEmotion(emotion); }
        if (typeof obj.content === "string" && obj.content) {
          const c = cleanStreamChunk(obj.content);
          sentence += c;
          if (/[。！？；!?;．·\n]/u.test(c) && sentence.trim()) {
            const s = cleanReply(sentence).trim(); sentence = "";
            if (s) await _callSpeak(s);
          }
        }
      }
    }
    if (sentence.trim()) { const s = cleanReply(sentence).trim(); if (s) await _callSpeak(s); }
  } catch (err) {
    if (err && err.name === "AbortError") return;  // 被打断
    showToast("通话失败：" + (err && err.message ? err.message : err));
  } finally { CALL.aborter = null; }
}

async function _callSpeak(text) {
  if (!CALL.active) return;
  CALL.audioQ.push(text);
  if (!CALL.playing) await _callPlayNext();
}
async function _callPlayNext() {
  CALL.playing = true;
  try {
    while (CALL.audioQ.length && CALL.active && !CALL.bargeIn) {
      await _callSpeakOne(CALL.audioQ.shift());
    }
  } finally { CALL.playing = false; }
}
async function _callSpeakOne(text) {
  let resp;
  try {
    resp = await fetch("/api/tts", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, lang: REPLY_LANG }),
    });
  } catch { return; }
  if (!resp.ok) return;
  try {
    const buf = await resp.arrayBuffer();
    const ctx = await getAudioCtx();
    const audioBuf = await ctx.decodeAudioData(buf);
    const src = ctx.createBufferSource();
    src.buffer = audioBuf;
    const analyser = ctx.createAnalyser(); analyser.fftSize = 512;
    src.connect(analyser); analyser.connect(ctx.destination);
    const data = new Uint8Array(analyser.fftSize);
    const mouthTimer = setInterval(() => {
      analyser.getByteTimeDomainData(data);
      let s = 0; for (let i = 0; i < data.length; i++) { const v = (data[i] - 128) / 128; s += v * v; }
      mouth = Math.min(1, Math.sqrt(s / data.length) * 5);
    }, 50);
    CALL.src = src;
    return await new Promise((resolve) => {
      src.onended = () => { mouth = 0; clearInterval(mouthTimer); CALL.src = null; resolve(); };
      src.start();
    });
  } catch { return; }
}

async function startCall() {
  if (CALL.active) return;
  if (!servicesReady) { showToast("服务未就绪，请稍后"); return; }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { showToast("当前环境不支持麦克风"); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const srcNode = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(CALL_CFG.buf, 1, 1);
    proc.onaudioprocess = _callProcess;
    const mute = ctx.createGain(); mute.gain.value = 0;   // 不把麦克风声音送回音箱
    srcNode.connect(proc); proc.connect(mute); mute.connect(ctx.destination);
    CALL.active = true; CALL.stream = stream; CALL.ctx = ctx; CALL.srcNode = srcNode; CALL.proc = proc;
    $("btn-call").classList.add("recording");
    $("btn-call").textContent = "📞 通话中…";
    showToast("通话模式已开启：直接说话，说一句它回一句（不显示文字）");
  } catch (e) { showToast("麦克风不可用：" + (e.message || e)); }
}

function stopCall() {
  if (!CALL.active) return;
  CALL.active = false;
  CALL.bargeIn = true;
  _callStopPlayback();
  try { if (CALL.proc) CALL.proc.disconnect(); } catch {}
  try { if (CALL.srcNode) CALL.srcNode.disconnect(); } catch {}
  try { if (CALL.ctx) CALL.ctx.close(); } catch {}
  try { if (CALL.stream) CALL.stream.getTracks().forEach((t) => t.stop()); } catch {}
  CALL.proc = CALL.srcNode = CALL.ctx = CALL.stream = null;
  CALL.speech = false; CALL.samples = []; CALL.audioQ = [];
  CALL.bargeIn = false; CALL.playing = false; CALL.src = null;
  $("btn-call").classList.remove("recording");
  $("btn-call").textContent = "📞 通话";
  showToast("通话模式已关闭");
}

/* ---------------- 聊天 ---------------- */
async function postChat(text) {
  const resp = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, lang: REPLY_LANG }),
  });
  return resp.json();
}

async function sendText(text) {
  text = (text || "").trim();
  if (!text) return;
  if (!servicesReady) { showToast("服务未就绪，请稍后"); return; }
  if (_tw && _tw.timer) { _tw.stop(); _tw = null; }
  addMsg("你", text, "me");
  $("input").value = "";
  if (FEATURES.streaming) {
    await streamChat(text);
    return;
  }
  try {
    let data = await postChat(text);
    if (data.error) {
      await new Promise((r) => setTimeout(r, 1000));
      data = await postChat(text);
    }
    if (data.error) { addMsg("系统", "出错：" + data.error, "sys"); return; }
    const replyText = cleanReply(data.reply || "");
    addMsg(CHAR_NAME, replyText, "her");
    playEmotion(data.emotion);
    speak(replyText);
  } catch (e) {
    addMsg("系统", "连不上本地服务器", "sys");
  }
}

/* ---------------- 流式打字机 ---------------- */
function addStreamMsg(who, cls) {
  const chat = $("chat");
  const d = document.createElement("div");
  d.className = "msg " + cls;
  const w = document.createElement("span");
  w.className = "who";
  w.textContent = who + "：";
  d.appendChild(w);
  const textSpan = document.createElement("span");
  textSpan.className = "tw-text";
  d.appendChild(textSpan);
  const cursor = document.createElement("span");
  cursor.className = "tw-cursor";
  cursor.textContent = "▍";
  d.appendChild(cursor);
  chat.appendChild(d);
  chat.scrollTop = chat.scrollHeight;
  return { box: d, textSpan, cursor };
}

class Typewriter {
  constructor(node) {
    this.textSpan = node.textSpan;
    this.cursor = node.cursor;
    this.buffer = "";
    this.display = "";
    this.timer = null;
    this.pendingPause = 0;
    this.done = false;
    this.stopping = false;
    this.onComplete = null;
    this.streamTts = FEATURES.stream_tts;
    this.sentenceBuf = "";
    this._speakQueue = [];
    this._speaking = false;
  }
  feed(text) { this.buffer += text; }
  finish() { this.done = true; }
  start() { this.timer = setInterval(() => this._tick(), 45); }
  stop() {
    if (this.timer) { clearInterval(this.timer); this.timer = null; }
    this.buffer = ""; this.done = true; this.stopping = true;
    if (this.cursor && this.cursor.parentNode) this.cursor.remove();
  }
  _tick() {
    if (this.stopping) return;
    if (this.pendingPause > 0) { this.pendingPause -= 45; return; }
    if (!this.buffer) {
      if (this.done) {
        clearInterval(this.timer); this.timer = null;
        this.display = cleanReply(this.display);
        this.textSpan.textContent = this.display;
        setTimeout(() => { if (this.cursor && this.cursor.parentNode) this.cursor.remove(); }, 1000);
        if (this.onComplete) { const cb = this.onComplete; this.onComplete = null; cb(this.display); }
      }
      return;
    }
    const ch = this.buffer[0];
    this.buffer = this.buffer.slice(1);
    this.display += ch;
    this.textSpan.textContent = this.display;
    $("chat").scrollTop = $("chat").scrollHeight;
    this.sentenceBuf += ch;
    if (this.streamTts && /[。！？；!?;．·]/u.test(ch)) {
      this._queueSpeak(this.sentenceBuf);
      this.sentenceBuf = "";
    }
    if (/[。！？；!?;．·]/u.test(ch)) this.pendingPause = 130;  // 句末停顿
  }
  _queueSpeak(text) {
    if (!text || !text.trim()) return;
    this._speakQueue.push(text);
    if (!this._speaking) this._nextSpeak();
  }
  async _nextSpeak() {
    this._speaking = true;
    while (this._speakQueue.length) {
      const t = this._speakQueue.shift();
      try { await speak(t); } catch (e) { /* 忽略单句失败 */ }
    }
    this._speaking = false;
  }
}

async function streamChat(text) {
  const node = addStreamMsg(CHAR_NAME, "her");
  const tw = new Typewriter(node);
  _tw = tw;
  tw.onComplete = (full) => { if (!tw.streamTts && full) speak(cleanReply(full)); };
  tw.start();
  try {
    const resp = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, lang: REPLY_LANG }),
    });
    if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "";
    let emotion = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const line = frame.replace(/^data:\s?/, "").trim();
        if (!line) continue;
        let obj;
        try { obj = JSON.parse(line); } catch (e) { continue; }
        if (obj.error) {
          tw.stop();
          addMsg("系统", "出错：" + obj.error, "sys");
          return;
        }
        if (obj.emotion && obj.emotion !== emotion) { emotion = obj.emotion; playEmotion(emotion); }
        if (typeof obj.content === "string" && obj.content) tw.feed(cleanStreamChunk(obj.content));
        if (obj.done) tw.finish();
      }
    }
    tw.finish();
    if (tw.streamTts && tw.sentenceBuf.trim()) tw._queueSpeak(cleanReply(tw.sentenceBuf));
  } catch (e) {
    tw.stop();
    addMsg("系统", "连不上本地服务器", "sys");
  }
}

/* ---------------- 按住说话 ---------------- */
async function startRecord() {
  if (talking) return;
  if (!servicesReady) { showToast("服务未就绪，请稍后"); return; }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    showToast("当前环境不支持麦克风");
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    // 优先 webm，其次 mp4（部分浏览器不支持 webm 时降级），都不支持用默认格式
    const mime = ["audio/webm", "audio/mp4"].find((t) => MediaRecorder.isTypeSupported(t)) || "";
    if (!mime) console.warn("[录音] 浏览器不支持 audio/webm 与 audio/mp4，使用默认格式");
    mediaRecorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    chunks = [];
    mediaRecorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    mediaRecorder.start();
    talking = true;
    $("btn-talk").classList.add("recording");
    $("btn-talk").textContent = "🔴 松开结束";
  } catch (e) {
    showToast("麦克风权限被拒绝：" + (e.message || e));
  }
}

async function stopRecord() {
  if (!talking) return;
  talking = false;
  $("btn-talk").classList.remove("recording");
  $("btn-talk").textContent = "🎤 按住说话";
  try { mediaRecorder.stop(); } catch (e) { return; }
  mediaRecorder.onstop = async () => {
    const blob = new Blob(chunks, { type: mediaRecorder.mimeType || "audio/webm" });
    if (mediaRecorder.stream) mediaRecorder.stream.getTracks().forEach((t) => t.stop());
    if (blob.size < 1000) { addMsg("系统", "没听到声音，请按住再说一次", "sys"); return; }
    addMsg("系统", "识别中…", "sys");
    try {
      const resp = await fetch("/api/transcribe", {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: blob,
      });
      const data = await resp.json();
      if (data.error) { addMsg("系统", "识别失败：" + data.error, "sys"); return; }
      if (data.text) sendText(data.text);
      else addMsg("系统", "没听清，请再说一次", "sys");
    } catch (e) {
      addMsg("系统", "识别失败：" + (e.message || e), "sys");
    }
  };
}

function bindUI() {
  $("btn-send").addEventListener("click", () => sendText($("input").value));
  $("input").addEventListener("keydown", (e) => { if (e.key === "Enter") sendText($("input").value); });
  const talk = $("btn-talk");
  talk.addEventListener("pointerdown", startRecord);
  talk.addEventListener("pointerup", stopRecord);
  talk.addEventListener("pointerleave", stopRecord);
  const callBtn = $("btn-call");
  if (callBtn) callBtn.addEventListener("click", () => { if (CALL.active) stopCall(); else startCall(); });
  $("btn-test").addEventListener("click", () => {
    addMsg("系统", "测试语音…", "sys");
    speak(TEST_PHRASES[REPLY_LANG] || TEST_PHRASES.en);
  });
}

/* ---------------- 右上角高级配置面板 ---------------- */
async function loadEmotionConfig() {
  try {
    const cfg = await fetch("/api/config").then((r) => r.json());
    setEmotionMap(cfg.EMOTION_MAPPING || {});
    if (typeof cfg.EMOTION_HOLD_MS === "number") window.__EMOTION_HOLD_MS = cfg.EMOTION_HOLD_MS;
  } catch (e) { /* 服务器未响应 */ }
}

async function loadConfigForm() {
  try {
    const cfg = await fetch("/api/config").then((r) => r.json());
    setEmotionMap(cfg.EMOTION_MAPPING || {});
    $("cfg-llm-url").value = cfg.LM_STUDIO_BASE_URL || "";
    if (llmCombo) {
      llmCombo.trigger.value = cfg.LM_MODEL || "";
      llmCombo.wrap.dataset.value = cfg.LM_MODEL || "";
    }
    $("cfg-tts-url").value = cfg.GPT_SOVITS_URL || "";
    $("cfg-tts-ref").value = cfg.TTS_REF_AUDIO_PATH || "";
    $("cfg-tts-prompt").value = cfg.TTS_PROMPT_TEXT || "";
    $("cfg-stt-url").value = cfg.STT_API_URL || "";
    const ss = $("cfg-stt-stream-url"); if (ss) ss.value = cfg.STT_STREAM_API_URL || "";
    $("cfg-models-dir").value = storeGet("tts_models_dir") || "";
  } catch (e) { /* 服务器未响应 */ }
}

async function loadModelList() {
  try {
    const d = await fetch("/api/llm_models").then((r) => r.json());
    if (llmCombo) llmCombo.setOptions(d.models || []);
  } catch (e) { /* 连不上 LLM 就不显示候选 */ }
}

/* ---------------- 长期记忆面板（隐蔽式：默认折叠，点击才展开） ---------------- */
const MEM_CATS = {
  name: "名字", preference: "喜好", dislike: "不喜欢", identity: "身份",
  plan: "计划", fact: "事实", note: "笔记", event: "动态",
};
let _memCount = 0;

async function loadMemoryPanel() {
  const countEl = $("mem-count");
  try {
    const d = await fetch("/api/memory").then((r) => r.json());
    if (!d || d.enabled === false) {
      countEl.textContent = "—";
      $("mem-toggle").title = "记忆功能未启用（可在 config.py 打开 MEMORY_ENABLED）";
      $("mem-body").classList.add("hidden");
      return;
    }
    _memCount = d.count || 0;
    countEl.textContent = _memCount + " 条";
    $("mem-toggle").title = "查看或管理已记住的信息";
    // 折叠态只更新计数，不加载列表（隐蔽）；展开时才渲染
    if (!$("mem-body").classList.contains("hidden")) {
      renderMemoryList(d.memories || []);
    }
  } catch (e) {
    countEl.textContent = "—";
  }
}

function toggleMemoryBody() {
  const body = $("mem-body");
  const willOpen = body.classList.contains("hidden");
  body.classList.toggle("hidden");
  if (willOpen) loadMemoryPanel();   // 展开时重新拉取（含列表）
}

function renderMemoryList(mems) {
  const list = $("mem-list");
  list.innerHTML = "";
  if (!mems.length) {
    const empty = document.createElement("div");
    empty.className = "mem-empty";
    empty.textContent = "还没有记忆。聊聊你的名字、喜欢什么，我就会记住。";
    list.appendChild(empty);
    return;
  }
  mems.forEach((m) => {
    const row = document.createElement("div");
    row.className = "mem-item";
    const cat = MEM_CATS[m.category] || m.category;
    const key = m.key ? (m.key + "：") : "";
    const hist = (m.history && m.history.length)
      ? '<span class="mem-hist">（此前：' + m.history.map((h) => h.value).join("、") + "）</span>"
      : "";
    const info = document.createElement("div");
    info.className = "mem-item-text";
    info.innerHTML = '<span class="mem-cat">' + cat + "</span> "
      + "<b>" + escHtml(key + m.value) + "</b>" + hist;
    const del = document.createElement("button");
    del.className = "mem-del";
    del.title = "删除这条记忆";
    del.textContent = "✕";
    del.addEventListener("click", async () => {
      try {
        await fetch("/api/memory/delete", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: m.id }),
        });
        loadMemoryPanel();
      } catch (e) { /* 忽略 */ }
    });
    row.appendChild(info);
    row.appendChild(del);
    list.appendChild(row);
  });
}

function escHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* 自定义确认弹窗（白粉毛玻璃风格，替代默认灰色 confirm） */
function showMemConfirm(onOk) {
  const modal = $("mem-confirm");
  modal.classList.remove("hidden");
  const ok = $("mem-confirm-ok");
  const cancel = $("mem-confirm-cancel");
  const close = () => {
    modal.classList.add("hidden");
    ok.removeEventListener("click", okFn);
    cancel.removeEventListener("click", close);
    modal.removeEventListener("click", maskFn);
  };
  const okFn = () => { close(); onOk(); };
  const maskFn = (e) => { if (e.target === modal) close(); };
  ok.addEventListener("click", okFn);
  cancel.addEventListener("click", close);
  modal.addEventListener("click", maskFn);
}

function bindMemoryPanel() {
  $("mem-toggle").addEventListener("click", toggleMemoryBody);
  $("mem-refresh").addEventListener("click", loadMemoryPanel);
  $("mem-clear").addEventListener("click", () => {
    showMemConfirm(async () => {
      try {
        await fetch("/api/memory/clear", { method: "POST" });
        showToast("已清空全部记忆");
        loadMemoryPanel();
      } catch (e) { showToast("清空失败：连不上本地服务器"); }
    });
  });
}

function bindConfigPanel() {
  $("cfg-open").addEventListener("click", () => {
    const p = $("cfg-panel");
    if (p.classList.contains("hidden")) {
      showPanel(p);
      loadConfigForm(); loadModelList(); $("cfg-msg").textContent = "";
      loadMemoryPanel();                                  // 打开面板时同步记忆列表
      const dir = storeGet("tts_models_dir");
      if (dir) { $("cfg-models-dir").value = dir; scanTtsModels(); }
      if (langCombo) langCombo.setValue(langLabel(REPLY_LANG), true);   // 打开时同步当前语言
    } else {
      hidePanel(p);
    }
  });
  $("cfg-scan-models").addEventListener("click", scanTtsModels);
  $("cfg-close").addEventListener("click", () => hidePanel($("cfg-panel")));
  // 回复语言：毛玻璃下拉（initLangCombo 创建，onChange 已生效并写 localStorage）
  $("cfg-save").addEventListener("click", async () => {
    const updates = {
      LM_STUDIO_BASE_URL: $("cfg-llm-url").value.trim(),
      LM_MODEL: (llmCombo ? llmCombo.trigger.value : "").trim(),
      GPT_SOVITS_URL: $("cfg-tts-url").value.trim(),
      TTS_REF_AUDIO_PATH: $("cfg-tts-ref").value.trim(),
      TTS_PROMPT_TEXT: $("cfg-tts-prompt").value,
      STT_API_URL: $("cfg-stt-url").value.trim(),
      STT_STREAM_API_URL: ($("cfg-stt-stream-url") || { value: "" }).value.trim(),
    };
    const msg = $("cfg-msg");
    msg.textContent = "保存中…";
    msg.className = "";
    try {
      const resp = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ updates }),
      });
      const data = await resp.json();
      msg.textContent = data.error ? ("保存失败：" + data.error) : "✓ " + data.message;
      msg.className = data.error ? "cfg-err" : "cfg-ok";
    } catch (e) {
      msg.textContent = "保存失败：连不上本地服务器";
      msg.className = "cfg-err";
    }
  });
}

/* ---------------- GPT-SoVITS 模型选择 ---------------- */
let _ttsGpts = [], _ttsSovits = [];

/* 自定义毛玻璃下拉（替代原生 select，避免系统灰底弹窗）；editable=true 时触发框可输入 */
function createGlassSelect(options, selectedValue, editable, onChange) {
  const wrap = document.createElement("div");
  wrap.className = "cfg-custom-select";
  wrap.dataset.value = selectedValue || "";

  let trigger;
  if (editable) {
    trigger = document.createElement("input");
    trigger.className = "cfg-select-trigger cfg-select-input";
    trigger.type = "text";
    trigger.placeholder = "选择或手动输入";
    trigger.value = selectedValue || "";
  } else {
    trigger = document.createElement("div");
    trigger.className = "cfg-select-trigger";
    trigger.textContent = shortName(wrap.dataset.value);
  }
  wrap.appendChild(trigger);

  const menu = document.createElement("div");
  menu.className = "cfg-select-menu";
  // 菜单挂 body + fixed 定位：面板带 overflow 会裁剪下拉
  document.body.appendChild(menu);

  function setDisplay(p) {
    if (editable) trigger.value = p;
    else trigger.textContent = shortName(p);
  }
  function setValue(p, silent) {
    wrap.dataset.value = p;
    setDisplay(p);
    menu.querySelectorAll(".cfg-select-item").forEach((it) => {
      it.classList.toggle("cfg-select-item-selected", it.dataset.value === p);
    });
    if (!silent && typeof onChange === "function") onChange(p);
  }
  function setOptions(opts) {
    menu.innerHTML = "";
    (opts || []).forEach((p) => {
      const item = document.createElement("div");
      item.className = "cfg-select-item";
      if (p === (wrap.dataset.value || (opts[0] || ""))) item.classList.add("cfg-select-item-selected");
      item.dataset.value = p;
      item.textContent = editable ? p : shortName(p);
      item.addEventListener("click", (e) => {
        e.stopPropagation();
        setValue(p);
        menu.classList.remove("open");
      });
      menu.appendChild(item);
    });
  }
  if (options && options.length) setOptions(options);

  function openMenu() {
    closeAllMenus();
    menu.style.position = "fixed";
    const r = wrap.getBoundingClientRect();
    menu.style.left = r.left + "px";
    menu.style.top = (r.bottom + 4) + "px";
    menu.style.width = r.width + "px";
    menu.style.maxHeight = "";
    menu.classList.add("open");
  }
  function closeMenu() { menu.classList.remove("open"); }

  trigger.addEventListener("click", (e) => {
    e.stopPropagation();
    if (menu.classList.contains("open")) { if (!editable) closeMenu(); }
    else openMenu();
  });

  return { wrap, trigger, setValue, setOptions, closeMenu };
}

function closeAllMenus() {
  document.querySelectorAll(".cfg-select-menu.open").forEach((m) => m.classList.remove("open"));
}
document.addEventListener("click", closeAllMenus);
window.addEventListener("scroll", closeAllMenus, true);
window.addEventListener("resize", closeAllMenus);

/* LLM 模型选择（毛玻璃下拉，可手动输入） */
let llmCombo = null;
function initLlmCombo() {
  const host = $("cfg-llm-model");
  host.innerHTML = "";
  llmCombo = createGlassSelect([], "", true);
  host.appendChild(llmCombo.wrap);
}

/* 回复语言选择（毛玻璃下拉，与 LLM/GPT-SoVITS 模型同款白粉风格） */
let langCombo = null;
function initLangCombo() {
  const host = $("cfg-lang-host");
  if (!host) return;
  host.innerHTML = "";
  langCombo = createGlassSelect(LANG_OPTIONS.map(([, l]) => l), langLabel(REPLY_LANG), false, (label) => {
    REPLY_LANG = langCode(label);
    storeSet("replyLang", REPLY_LANG);
    updateLangBadge();
    showToast("回复语言已切换为 " + label);
  });
  host.appendChild(langCombo.wrap);
}

async function scanTtsModels() {
  const dir = $("cfg-models-dir").value.trim();
  const status = $("cfg-models-status");
  if (!dir) {
    status.textContent = "请先填写模型文件夹路径";
    status.className = "cfg-models-status cfg-err";
    return;
  }
  storeSet("tts_models_dir", dir);
  status.textContent = "扫描中…";
  status.className = "cfg-models-status";
  try {
    const d = await fetch("/api/tts_models?dir=" + encodeURIComponent(dir)).then((r) => r.json());
    _ttsGpts = d.gpts || [];
    _ttsSovits = d.sovits || [];
    renderTtsModels(d.models || []);
    if (d.models && d.models.length) {
      status.textContent = "找到 " + d.models.length + " 个模型（每个含 GPT + SoVITS 权重）";
      status.className = "cfg-models-status cfg-ok";
    } else {
      status.textContent = "该目录下未找到成对的 .ckpt + .pth 模型";
      status.className = "cfg-models-status cfg-err";
    }
  } catch (e) {
    status.textContent = "扫描失败：" + (e.message || e);
    status.className = "cfg-models-status cfg-err";
  }
}

function shortName(path) {
  const parts = (path || "").split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

function renderTtsModels(models) {
  const list = $("cfg-models-list");
  list.innerHTML = "";
  models.forEach((m) => {
    const row = document.createElement("div");
    row.className = "cfg-model";
    const name = document.createElement("div");
    name.className = "cfg-model-name";
    name.textContent = m.name;
    row.appendChild(name);

    const gRow = document.createElement("div");
    gRow.className = "cfg-model-row";
    const gLab = document.createElement("span"); gLab.textContent = "GPT";
    const gSel = createGlassSelect(_ttsGpts, m.gpt).wrap;
    gRow.appendChild(gLab); gRow.appendChild(gSel); row.appendChild(gRow);

    const sRow = document.createElement("div");
    sRow.className = "cfg-model-row";
    const sLab = document.createElement("span"); sLab.textContent = "SoVITS";
    const sSel = createGlassSelect(_ttsSovits, m.sovits).wrap;
    sRow.appendChild(sLab); sRow.appendChild(sSel); row.appendChild(sRow);

    const btn = document.createElement("button");
    btn.className = "cfg-model-apply";
    btn.textContent = "应用并切换";
    btn.addEventListener("click", () => {
      applyTtsModel(m.name, gSel.dataset.value, sSel.dataset.value, m.ref_audio, m.prompt);
    });
    row.appendChild(btn);
    list.appendChild(row);
  });
}

async function applyTtsModel(name, gpt, sovits, refAudio, prompt) {
  const status = $("cfg-models-status");
  status.textContent = "切换中…";
  status.className = "cfg-models-status";
  try {
    const resp = await fetch("/api/tts_model_apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gpt, sovits, ref_audio: refAudio || "", prompt: prompt || "", persist: true }),
    });
    const data = await resp.json();
    if (data.error) {
      status.textContent = "切换失败：" + data.error;
      status.className = "cfg-models-status cfg-err";
    } else {
      status.textContent = "已切换到「" + name + "」，后续推理将用此模型";
      status.className = "cfg-models-status cfg-ok";
    }
  } catch (e) {
    status.textContent = "切换失败：连不上本地服务器";
    status.className = "cfg-models-status cfg-err";
  }
}

/* ---------------- 点击飘心/樱花特效（纯装饰） ---------------- */
function initClickFX() {
  const ICONS = ["❤️", "🌸", "⭐", "✨"];
  document.addEventListener("click", (e) => {
    const n = 1 + Math.floor(Math.random() * 2);
    for (let i = 0; i < n; i++) {
      const el = document.createElement("div");
      el.className = "click-fx";
      el.textContent = ICONS[Math.floor(Math.random() * ICONS.length)];
      el.style.fontSize = (16 + Math.floor(Math.random() * 9)) + "px";
      el.style.left = (e.clientX + (Math.random() * 24 - 12)) + "px";
      el.style.top = (e.clientY + (Math.random() * 12 - 6)) + "px";
      document.body.appendChild(el);
      el.addEventListener("animationend", () => el.remove());
    }
  });
}

/* 拉取功能开关（流式 / 边出边读），缺失时保持默认 false → 回退阻塞模式 */
async function loadFeatures() {
  try {
    const f = await fetch("/api/features").then((r) => r.json());
    if (f && typeof f === "object") {
      FEATURES.streaming = !!f.streaming;
      FEATURES.stream_tts = !!f.stream_tts;
      FEATURES.stream_tts_chunk = parseInt(f.stream_tts_chunk, 10) || 0;
    }
  } catch (e) { /* 拿不到就回退阻塞模式 */ }
}

/* ---------------- frameless 自绘标题栏（— / ✕） ---------------- */
function bindTitleBar() {
  $("titlebar-min").addEventListener("click", () => {
    try { window.pywebview.api.minimize(); } catch (e) { /* 浏览器调试模式无 js_api */ }
  });
  $("titlebar-close").addEventListener("click", () => {
    try { window.pywebview.api.close(); } catch (e) { /* 浏览器调试模式无 js_api */ }
  });
}

/* ---------------- frameless 四角缩放手柄（右下/左下/左上，替代系统拖边缩放） ---------------- */
let _resizing = null;
function bindResizeHandle() {
  // 每个手柄 data-fix 表示"固定哪个角不动"：nw=固定左上(拖右下) ne=固定右上(拖左下) se=固定右下(拖左上)
  document.querySelectorAll(".resize-handle").forEach((handle) => {
    handle.addEventListener("mousedown", (e) => {
      e.preventDefault();
      e.stopPropagation();   // 避免被 easy_drag 全局监听抢走
      _resizing = {
        fix: handle.dataset.fix || "nw",
        startX: e.screenX, startY: e.screenY,
        startW: window.innerWidth, startH: window.innerHeight,
      };
      window.addEventListener("mousemove", onResizeMove);
      window.addEventListener("mouseup", onResizeUp);
    });
  });
}
function onResizeMove(e) {
  if (!_resizing) return;
  const dx = e.screenX - _resizing.startX;
  const dy = e.screenY - _resizing.startY;
  let w = _resizing.startW, h = _resizing.startH;
  if (_resizing.fix === "nw")      { w += dx; h += dy; }        // 拖右下角
  else if (_resizing.fix === "ne") { w -= dx; h += dy; }        // 拖左下角
  else if (_resizing.fix === "se") { w -= dx; h -= dy; }        // 拖左上角
  try { window.pywebview.api.resize(Math.round(w), Math.round(h), _resizing.fix); } catch (err) { /* 调试模式忽略 */ }
}
function onResizeUp() {
  _resizing = null;
  window.removeEventListener("mousemove", onResizeMove);
  window.removeEventListener("mouseup", onResizeUp);
}

/* ---------------- 点击设置面板外部自动关闭（保留关闭按钮） ---------------- */
function bindOutsideClickClose() {
  document.addEventListener("mousedown", (e) => {
    // 两个设置面板：任一打开时，若点击点在面板外部 → 关闭该面板
    const panels = [$("settings-panel"), $("cfg-panel")];
    for (const p of panels) {
      if (!p || p.classList.contains("hidden")) continue;
      // 面板内部、打开按钮、毛玻璃下拉菜单（挂在 body 下）、确认弹窗 → 都不触发关闭
      const inside = p.contains(e.target)
        || (e.target.closest && (
            e.target.closest("#btn-settings")
            || e.target.closest("#cfg-open")
            || e.target.closest(".cfg-select-menu")
            || e.target.closest(".mem-confirm")));
      if (!inside) hidePanel(p);
    }
  });
}

window.addEventListener("DOMContentLoaded", () => {
  REPLY_LANG = storeGet("replyLang") || "en";   // 恢复上次选择的回复语言（默认英文）
  updateLangBadge();
  loadSettings();
  bindUI();
  bindSettingsUI();
  initModelCombo();
  initLlmCombo();
  initLangCombo();
  bindConfigPanel();
  bindMemoryPanel();
  bindZoom();
  bindTitleBar();
  bindResizeHandle();
  bindOutsideClickClose();
  initClickFX();
  loadTitle();
  loadFeatures();
  loadVadConfig();
  loadingMsg = addMsg("系统", "正在加载模型和连接服务…", "sys");
  applyBackground();
  initLive2D();
  servicesReady = false;
});
