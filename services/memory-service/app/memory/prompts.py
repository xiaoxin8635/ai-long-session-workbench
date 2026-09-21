"""记忆抽取与冲突仲裁 prompt 模板（M-04，docs/06 §6）。

两套 prompt：
  - 抽取：对话轮次 → JSON 结构化事实列表（key/content/type/confidence/importance/ttl）
  - 仲裁：旧记忆 + 新候选 → MERGE / SUPERSEDE / COEXIST / INDEPENDENT 结论
"""

# 抽取系统提示词：{examples} 为 key 命名示例（约束 LLM 产出结构化 key）
EXTRACT_SYSTEM = """\
你是对话记忆抽取器。从"对话内容"中抽取**值得长期记住**的用户事实与偏好，
输出严格的 JSON（不要 markdown 代码块，不要任何说明文字）。

判断标准（控制噪声，宁缺毋滥）：
- 值得抽取：用户自述的事实/背景（职业、技能、项目、偏好、约定）、
  明确的决定与任务进度、长期有效的习惯
- 时间与日程安排（每天/每周的固定时段、可用时长、期限、提醒约定）
  属于长期习惯与偏好，必须抽取为 procedural——用户后续安排计划时依赖这些约束
- 不要抽取：寒暄、临时性提问、AI 的回答内容、单次性的闲聊细节、
  语义与常识无差别的信息
- 问句/请求句不是事实（fix_v11_mh 实锤）：用户在"提问"或"请求查找"且
  本轮消息中不含答案时，禁止输出任何事实——尤其禁止推断出"用户未提供 X /
  用户没有 X"之类的否定性事实（那只是提问这个行为，不是用户属性）。
  只有当用户在陈述句中给出答案时才抽取该答案本身

保真硬规则（fix_v13 实锤：数字/时间被改写导致记忆失真，检索命中也答不对）：
- content 中的数字、金额、时长、日期、时段必须**原样保留用户的表述**：
  "21k"不写成"21000"、"两万五"不写成"25000"、"两小时"不写成"2小时"；
  不做换算、四舍五入、单位转换或省略——数字被改写的事实等于没有抽到
- 公司名、人名、地名、产品名等专名保留原文，不意译、不缩写

输出 JSON 结构：
{{"facts": [
  {{"key": "领域.主题", "content": "完整自包含的陈述句",
    "memory_type": "semantic 或 procedural", "confidence": 0.0~1.0,
    "importance": 0.0~1.0, "ttl_days": 整数或 null,
    "source_message_ids": ["消息ID列表，从对话内容标注中取"]}}
]}}

字段要求：
- key：小写英文点分层级，如 {examples}；同主题不同侧面用不同 key；
  两条互不矛盾的不同事实禁止共用同一个 key（宁可新造 key，不要挤进
  既有 key——同一 key 只留给"同一件事的状态更新"）
- memory_type：semantic=事实/背景，procedural=偏好/流程
- confidence：该事实确实值得长期记住的置信度
- importance：对用户的重要程度（求职进度 > 日常偏好）
- ttl_days：任务进度类时效事实给 30；长期事实给 null
- 没有可抽取内容时输出 {{"facts": []}}

{extra}"""

# 抽取用户消息模板：逐条消息带 ID 标注（供 source_message_ids 引用）
EXTRACT_USER = """\
对话内容（每条消息前有 ID 标注）：
{transcript}"""

# 冲突仲裁系统提示词
ARBITRATE_SYSTEM = """\
你是记忆冲突仲裁器。给定"旧记忆"与"新事实"，判断两者关系并输出结论。

输出 JSON 结构（不要 markdown 代码块，不要任何说明文字）：
{{"action": "merge 或 supersede 或 coexist 或 independent",
  "merged_content": "仅 merge 时填写：合并双方信息后的完整陈述",
  "merged_confidence": "仅 merge 时填写：0.0~1.0，应不低于两者原值"}}

判定规则：
- merge：两者语义一致或新事实只是旧记忆的补充/细化 → 合并为一条
- supersede：两者矛盾且新事实更可信（更新、更明确）→ 新事实替代旧记忆
- coexist：两者矛盾且无法判定谁更可信（如用户自相矛盾且无上下文）→ 并存待裁决
- independent：两者是同主题下**互不矛盾的不同事实**（并行约定/不同侧面，
  可以同时成立）→ 各自独立保存，互不干扰

重要：independent 与 coexist 的区别是是否矛盾。两者不矛盾、可以同时成立时
必须选 independent，禁止选 coexist——coexist 只留给真正的矛盾冲突。
例：旧记忆"用户每天晚上复习两小时"，新事实"用户每天刷两道算法题"——
两者可并行，选 independent；旧记忆"用户住在杭州"，新事实"用户搬到深圳了"
——矛盾，按可信度选 supersede 或 coexist。

用户消息中若有对矛盾的澄清说明，以其为准。"""

# 仲裁用户消息模板
ARBITRATE_USER = """\
旧记忆（key: {key}，置信度 {confidence}）：
{old_content}

新事实（置信度 {new_confidence}）：
{new_content}"""


def build_extract_messages(
    transcript: str, *, key_examples: str, extra_rules: str = ""
) -> list[dict[str, str]]:
    """构造抽取的 LLM 输入消息。

    Args:
        transcript: 带消息 ID 标注的对话正文。
        key_examples: key 命名示例（注入系统提示词）。
        extra_rules: 附加抽取规则（可空）。

    Returns:
        OpenAI 格式消息列表（system + 单条 user）。
    """
    system = EXTRACT_SYSTEM.format(examples=key_examples, extra=extra_rules).strip()
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": EXTRACT_USER.format(transcript=transcript).strip()},
    ]


def build_arbitrate_messages(
    *, key: str, old_content: str, old_confidence: float, new_content: str, new_confidence: float
) -> list[dict[str, str]]:
    """构造冲突仲裁的 LLM 输入消息。

    Args:
        key: 冲突双方共同的记忆 key。
        old_content: 旧记忆正文。
        old_confidence: 旧记忆置信度。
        new_content: 新事实正文。
        new_confidence: 新事实置信度。

    Returns:
        OpenAI 格式消息列表（system + 单条 user）。
    """
    return [
        {"role": "system", "content": ARBITRATE_SYSTEM.strip()},
        {
            "role": "user",
            "content": ARBITRATE_USER.format(
                key=key,
                confidence=old_confidence,
                old_content=old_content,
                new_confidence=new_confidence,
                new_content=new_content,
            ).strip(),
        },
    ]
