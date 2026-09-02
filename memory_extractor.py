# -*- coding: utf-8 -*-
"""记忆提取层：从用户话语中抽取值得长期保存的信息。
"""
import re

# 信号词：命中任意一个，即使没到"每 N 条"也值得触发 LLM 提取
SIGNAL_WORDS_ZH = ("记住", "记得", "别忘", "我叫", "我的名字", "我喜欢", "我爱",
                   "我讨厌", "我不喜欢", "我是", "我住在", "我家在", "我今年",
                   "我的生日", "我打算", "我计划", "我要去", "我在")
SIGNAL_WORDS_EN = ("remember", "don't forget", "my name", "i like", "i love",
                   "i hate", "i don't like", "i work", "i live", "i am",
                   "i'm", "i was born", "birthday", "i plan", "i'm going")


def has_signal(text: str) -> bool:
    """检测是否含值得 LLM 提取的信号词。"""
    if not text:
        return False
    low = text.lower()
    return any(w in low for w in SIGNAL_WORDS_ZH) or any(w in low for w in SIGNAL_WORDS_EN)

_QUESTION_MARKS = ("?", "？", "why", "what", "how", "do u", "do you", "know",
                   "吗", "呢", "什么", "怎么", "为啥", "是不是", "对不对", "对吧")


def _looks_like_question(text: str) -> bool:
    """整句是不是提问（问号结尾 或 提问句式）。纯提问不触发 LLM 提取，省 token 且避免垃圾。"""
    if not text:
        return False
    low = text.strip().lower()
    if low.endswith(("?", "？")):
        return True
    return any(w in low for w in ("do you know", "do u know", "what do you", "what do u",
                                  "你知道", "你记得", "你知不知道"))


def _value_is_garbage(value: str, category: str, check_en: bool = True) -> bool:
    """解析层硬过滤：LLM 吐出的 value 若带提问残渣/英文未归一化，丢弃该条。
    这是对"提问不提取 + 归一化中文"两条 prompt 原则的兜底

    check_en：是否检查"英文未归一化"。仅 LLM 提取路径开启（LLM 有翻译能力，
    输出英文 = 没遵守 prompt）；规则提取路径关闭（规则无法翻译，I like coffee
    这类英文偏好宁保留不丢弃）。
    """
    if not value:
        return True
    low = value.lower()
    # 1) 含提问残渣（问号/疑问词/对话残留）→ 丢弃
    for mark in _QUESTION_MARKS:
        if mark in low:
            return True
    # 2) 英文未归一化为中文：value 里英文字母占比过高（name 类英文名除外）
    if check_en and category != "name":
        letters = sum(1 for ch in value if ch.isalpha())
        en = sum(1 for ch in value if 'a' <= ch.lower() <= 'z')
        if letters and en / letters > 0.6:
            return True
    return False


# ---------- 规则提取 ----------
# 元组：(category, key, importance, pattern, value_group, key_group)
# - value_group: 提取为 value 的捕获组号（0 = 整段匹配）
# - key_group: 提取为 key 的捕获组号（None = 用固定 key）
# 注意顺序：更具体的模式在前（如"我不喜欢"先于"我喜欢"），避免误匹配
_RULES: list[tuple[str, str, float, str, int, int | None]] = [
    # ---- 姓名 ----
    ("name", "姓名", 9.5, r"我叫(.{1,12}?)(?:，|。|！|？|,|\.|!|\?|$)", 1, None),
    ("name", "姓名", 9.5, r"我的名字(?:叫|是)(.{1,12}?)(?:，|。|！|？|,|\.|!|\?|$)", 1, None),
    ("name", "姓名", 9.5, r"(?:my name is|i'?m called|call me)\s+([A-Za-z\u4e00-\u9fa5]{1,12})(?:[,.!?\s]|$)", 1, None),
    # ---- 喜好（"我"可省略：口语"最喜欢喝奶茶"；动词并入 value 便于检索"喝什么"） ----
    ("preference", "喜欢的食物", 7.0, r"(?:我)?(?:最)?喜欢((?:吃|喝).{1,20}?)(?:，|。|！|？|,|\.|!|\?|$)", 1, None),
    ("preference", "喜欢", 7.0, r"(?:我)?(?:最)?喜欢(?!吃|喝)(.{1,20}?)(?:，|。|！|？|,|\.|!|\?|$)", 1, None),
    ("preference", "喜欢", 7.0, r"i (?:really |so |also )?like (.{1,30}?)(?:\.|,|!|\?|$)", 1, None),
    ("preference", "喜欢", 7.0, r"i (?:love|enjoy) (.{1,30}?)(?:\.|,|!|\?|$)", 1, None),
    # ---- 厌恶 ----
    ("dislike", "不喜欢", 7.0, r"(?:我)?(?:最)?不(?:太|怎么)?喜欢(.{1,20}?)(?:，|。|！|？|,|\.|!|\?|$)", 1, None),
    ("dislike", "讨厌", 7.0, r"我讨厌(.{1,20}?)(?:，|。|！|？|,|\.|!|\?|$)", 1, None),
    ("dislike", "不喜欢", 7.0, r"i (?:don't like|hate|dislike) (.{1,30}?)(?:\.|,|!|\?|$)", 1, None),
    # ---- 身份/职业 ----
    ("identity", "职业", 7.5, r"我(?:是|是一名|是一个|是位)(?:程序员|工程师|学生|老师|教师|医生|护士|设计师|律师|会计|公务员|作家|画家|厨师|司机|老板|经理|警察)(?:。|，|！|？|\.|,|!|\?|$)", 0, None),
    ("identity", "工作单位", 7.5, r"我(?:在|于)(.{1,15}?)(?:工作|上班|任职)(?:。|，|！|？|\.|,|!|\?|$)", 1, None),
    ("identity", "职业", 7.5, r"i (?:work|am working) (?:as |at )?(.{1,20}?)(?:\.|,|!|\?|$)", 1, None),
    # ---- 居住地 ----
    ("identity", "居住地", 7.0, r"我(?:住在|家住在|家 在|是.{0,3}人)(.{1,12}?)(?:。|，|！|？|\.|,|!|\?|$)", 1, None),
    ("identity", "居住地", 7.0, r"i (?:live|am living) in (.{1,20}?)(?:\.|,|!|\?|$)", 1, None),
    # ---- 年龄 ----
    ("identity", "年龄", 7.0, r"我(?:今年|已经|都)?(\d{1,3})岁(?:了)?(?:。|，|！|？|\.|,|!|\?|$)", 1, None),
    ("identity", "年龄", 7.0, r"i'?m (\d{1,3}) years? old", 1, None),
    # ---- 生日 ----
    ("fact", "生日", 7.0, r"我的生日(?:是|在)(.{1,15}?)(?:。|，|！|？|\.|,|!|\?|$)", 1, None),
    ("fact", "生日", 7.0, r"my birthday is (.{1,15}?)(?:\.|,|!|\?|$)", 1, None),
    # ---- 计划 ----
    ("plan", "计划", 6.5, r"我(?:打算|计划|准备)(.{1,25}?)(?:。|，|！|？|\.|,|!|\?|$)", 1, None),
    ("plan", "计划", 6.5, r"我(?:下个月|下周|明天|后天|这个周末|最近)(?:要|想|打算)?(.{1,25}?)(?:。|，|！|？|\.|,|!|\?|$)", 1, None),
    ("plan", "计划", 6.5, r"i (?:plan|am going|want) to (.{1,30}?)(?:\.|,|!|\?|$)", 1, None),
    # ---- 叮嘱（记住我说的话） ----
    ("note", "叮嘱", 6.0, r"(?:请)?(?:记住|记得|别忘(?:了|记)?|please remember|don't forget)[:：]?\s*(.{1,30}?)(?:。|，|！|？|\.|,|!|\?|$)", 1, None),
    # ---- 一般事实（我的 XX 是 YY / my XX is YY）----
    # key 和 value 都来自捕获组：key=第1组（"狗"），value=第2组（"旺财"）
    ("fact", "", 5.5, r"我的(.{1,8})(?:是|叫)(.{1,20}?)(?:。|，|！|？|\.|,|!|\?|$)", 2, 1),
    ("fact", "", 5.5, r"my (?!name |birthday )(.{1,15}?) is (.{1,25}?)(?:\.|,|!|\?|$)", 2, 1),
    # ---- 行为/动态（用户做了什么，带时间点记忆；追加式 note） ----
    # 例："今天做了个程序挺开心的" → "做了个程序"；"我剪辑了我的pubg击杀视频" → "剪辑了pubg击杀视频"
    # 用整段匹配 + event 专属清洗（去主语/时间词/“我的”），value 保留动作+内容
    ("event", "动态", 5.5,
     r"((?:我|咱|人家)?(?:今天|昨天|刚才|昨晚|早上|下午|最近|刚刚|今早|今晚)?"
     r"(?:做|写|剪辑|录|拍|发|买|去|看|玩|学|参加|完成|打完|通关|通过|考完|搞定|吃|喝|跑|练|建|搭|改|修)"
     r"(?:了|完|过|了个|了下|好了|完了|了下个|了一)(?:我的|咱的|我们的)?.{1,25}?)(?:。|，|！|？|,|\.|!|\?|$)",
     0, None),
]


def extract_rules(text: str, max_items: int = 8) -> list[dict]:
    """规则提取：返回记忆项列表（去重、截断长度）。"""
    if not text or len(text) > 1000:
        return []
    items: list[dict] = []
    seen: set[tuple] = set()
    for category, key, importance, pattern, value_group, key_group in _RULES:
        if len(items) >= max_items:
            break
        for m in re.finditer(pattern, text, re.IGNORECASE):
            if len(items) >= max_items:
                break
            if value_group == 0:
                # 用整段匹配词（职业类 / 行为动态类）
                matched = m.group(0).strip()
                if category == "event":
                    value = _clean_event_value(matched)
                else:
                    value = re.sub(r"^(我是|i am|i'?m|a|an|a )", "", matched, flags=re.IGNORECASE).strip()
            else:
                value = m.group(value_group).strip()
            value = _clean_value(value)
            if not value:
                continue
            if _value_is_garbage(value, category, check_en=False):
                continue
            if key_group is not None:
                key = m.group(key_group).strip()[:20] if m.group(key_group) else ""
            dedup_key = (category, key or value, value)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            items.append({
                "category": category,
                "key": key,
                "value": value,
                "importance": importance,
                # event（行为动态）与 note 一样：追加式，不覆盖（每个时间点的事都保留）
                "kind": "fact" if category not in ("note", "event") else "note",
            })
    return items


def _clean_event_value(v: str) -> str:
    """行为动态值清洗：去主语/时间词/“我的”/句尾语气，保留"动词+内容"。
    例："今天做了个程序挺开心的" → "做了个程序开心"；"我剪辑了我的pubg击杀视频" → "剪辑了pubg击杀视频"。"""
    v = re.sub(r"^(我|咱|人家|我们)", "", v).strip()
    v = re.sub(r"^(今天|昨天|刚才|昨晚|早上|下午|最近|刚刚|今早|今晚|前天|上周末|这周末)", "", v).strip()
    v = re.sub(r"(我的|咱的|我们的)", "", v, count=1).strip()   # "剪辑了我的视频" → "剪辑了视频"
    v = v.rstrip("。，！？,.!?；;:：")
    v = re.sub(r"的$", "", v).strip()                      # "做了个程序挺开心的" → "做了个程序挺开心"
    v = re.sub(r"(挺|很|真的|有点|超级|好|太)开心$", "开心", v).strip()
    return v.strip()[:30]


def _clean_value(v: str) -> str:
    """清洗提取值：去首尾修饰、标点、超长截断。"""
    v = v.strip().strip("。，！？,.!?；;:：")
    v = re.sub(r"^(就是|是|叫|一个|一位|一名|很|比较|非常|特别)", "", v).strip()
    v = re.sub(r"(呢|啊|呀|啦|哦|哈|呗|了)$", "", v).strip()
    return v[:50]


# ---------- LLM 补充提取 ----------
_LLM_EXTRACT_PROMPT = """【核心原则】
1. 只提取**确定的、新出现的**事实（姓名/喜好/厌恶/身份/计划）。如果用户只是提问（如"你知道我喜欢什么吗？"），不要提取提问内容，要提取他**后面回答里**的真实偏好。
2. 提取的 value 必须**归一化为中文**（即使原文是英文，如"clear weather"译为"晴天"），这样最省字符且与系统语言一致。
3. 如果用户说了多个同类事实，只取最明确的那个（如"我喜欢晴天，不喜欢下雨" → 提取"偏好：晴天"）。
4. 没有值得记的新信息就输出空数组 []。

【输出格式】
必须是严格的 JSON 数组，每项为：{{"category": "name|preference|dislike|identity|plan|fact|note","key": "简短中文键名（如 天气、食物、职业）","value": "简短中文值（不超过10个字）","importance": 1到10的整数}}

【正确示例】
- 用户说："i like clear weather most cuz i dont like change."→ 输出：[{{"category":"preference","key":"天气","value":"晴天","importance":7}}]（注意：绝不能提取为"about weather"）
- 用户说："我叫小红，今年18岁。"→ 输出：[{{"category":"name","key":"姓名","value":"小红","importance":9}}, {{"category":"identity","key":"年龄","value":"18岁","importance":7}}]
- 用户说："do u know wht i like about weather?" （这只是提问，没有给出答案）→ 输出：[]

现在，用户说："{text}"
"""


def extract_with_llm(text: str, llm_client=None, model: str = "",
                     base_url: str = "") -> list[dict]:
    """LLM 辅助提取：调用本地 LLM 输出 JSON 事实。失败静默返回 []。

    llm_client 可为 openai.OpenAI 实例或 None（None 时用 model/base_url 新建）。
    这是可选的补充提取，任何异常都不允许上抛。
    """
    try:
        if llm_client is None:
            from openai import OpenAI
            llm_client = OpenAI(base_url=base_url or "http://127.0.0.1:1234/v1",
                                api_key="lm-studio")
        resp = llm_client.chat.completions.create(
            model=model or "local-model",
            messages=[{"role": "user", "content": _LLM_EXTRACT_PROMPT.format(text=text[:500])}],
            temperature=0.0, max_tokens=300)
        raw = (resp.choices[0].message.content or "").strip()
        return _parse_llm_json(raw)
    except Exception as e:  # noqa: BLE001
        print(f"[记忆] LLM 提取失败（静默跳过）：{e}")
        return []


def _parse_llm_json(raw: str) -> list[dict]:
    """容错解析 LLM 输出的 JSON 数组：去 ```json 围栏、找第一个 [ 到最后一个 ]。"""
    import json as _json
    if not raw:
        return []
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = _json.loads(raw[start:end + 1])
    except Exception:
        return []
    out = []
    valid_cats = {"name", "preference", "dislike", "identity", "plan", "fact", "note"}
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict):
            continue
        cat = str(d.get("category", "fact"))
        val = str(d.get("value", "")).strip()
        if not val:
            continue
        if cat not in valid_cats:
            cat = "fact"
        imp = float(d.get("importance", 5.0) or 5.0)
        imp = max(1.0, min(10.0, imp))
        key = str(d.get("key", "")).strip()[:20]
        # value 按 prompt 规格截断为 ≤10 字（归一化中文，最省字符）
        # 并做硬过滤：提问残渣 / 英文未归一化 → 丢弃该条（防 3B 模型垃圾入库）
        if _value_is_garbage(val, cat):
            continue
        out.append({"category": cat, "key": key, "value": val[:10],
                    "importance": imp, "kind": "fact" if cat != "note" else "note"})
    return out
