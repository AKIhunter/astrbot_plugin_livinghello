"""发送前剥离模型泄漏的推理/思考文本。

背景
----
AstrBot 只在非流式 ``_parse_openai_completion()`` 里用正则剥离
``<think>...</think>``；当上游把开标签放进 ``reasoning_content``、只把思考正文
留在 ``content`` 时，正文里就只剩一个孤立的 ``</thinking>``，成对正则匹配不到，
整段思考会被当成角色发言进入投递链，再被分段切成多条气泡逐条发给用户。

本模块提供三层防护，按可信度从高到低：

1. ``strip_thinking``：确定性标签清洗。覆盖成对块、孤立开标签、孤立闭合标签，
   以及历史 prompt 里 ``[以下是你在回复前...内心思考（校验码 ...）]`` 被复述。
2. ``strip_reasoning_echo``：用上游返回的 ``reasoning_content`` 做差分。中转通常
   会正确分离思考与正文，偶发把思考漏进正文；此时用「正文前缀是否逐字出现在
   已知思考里」判断并裁掉，不依赖任何标签。
3. 上层 ``guard_reply_delivery`` / ``guard_segments`` 在合并成整段后调用，
   避免分段把思考切成多条气泡。

围栏代码块/表格原样保留，不参与标签清洗，避免把示例代码误删。

设计约束：不创建新组件对象，只改连续 Plain 中的第一个、把同组其余置空，
以保留 at_guard 投递链里基于 ``id(c)`` 的组件追踪。
"""
from __future__ import annotations

import re

from astrbot.api.message_components import Plain

_TAGS = r"think|thinking|analysis|reasoning"

# 成对块，跨行；标签大小写不敏感。
_PAIRED = re.compile(rf"<({_TAGS})>.*?</\1\s*>", re.I | re.S)
# 未闭合的开标签：从开标签起全部丢弃（后面没有角色正文了）。
_OPEN_ONLY = re.compile(rf"<({_TAGS})>.*\Z", re.I | re.S)
# 孤立闭合标签：丢弃它以及它之前的全部思考正文。
_CLOSE_ONLY = re.compile(rf"\A.*?</(?:{_TAGS})\s*>", re.I | re.S)
# 只删残留的闭合标签本身（用于标签后没有正文的兜底）。
_TAIL_TAG = re.compile(rf"</(?:{_TAGS})\s*>", re.I)
# 历史 user 消息里注入的「内心思考」标记，防止被模型复述出来。
_INNER_THOUGHT = re.compile(
    r"\[?以下是你在回复前[^\]\n]{0,160}内心思考[^\]\n]{0,160}\]?", re.S)
# 围栏代码块，整体跳过清洗。
_FENCE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~)")

# 差分裁剪的保守阈值：重合片段至少这么长，且占正文这么多比例才动手。
ECHO_MIN_OVERLAP = 40
ECHO_MIN_RATIO = 0.30


def _strip_core(text: str) -> str:
    text = _PAIRED.sub("", text)
    text = _OPEN_ONLY.sub("", text)
    if _TAIL_TAG.search(text):
        trimmed = _CLOSE_ONLY.sub("", text, count=1)
        # 闭合标签之后确实还有角色正文时才删前面；否则只去掉标签，
        # 避免把用户真的引用了标签的一整条消息误删成空白。
        text = trimmed if trimmed.strip() else _TAIL_TAG.sub("", text)
    return _INNER_THOUGHT.sub("", text)


def strip_thinking(text: str):
    """返回 (清洗后的文本, 是否发生改动)。幂等。"""
    if not text or ("<" not in text and "以下是你在回复前" not in text):
        return text, False
    original = text
    if _FENCE.search(text):
        # 代码块/表格里的标签是内容，不是模型思考。
        text = "".join(
            block if block.startswith(("```", "~~~")) else _strip_core(block)
            for block in _FENCE.split(text)
        )
    else:
        text = _strip_core(text)
    text = text.strip()
    return text, text != original.strip()


def _norm_with_map(text: str):
    """把连续空白折叠成单个空格，并记录每个归一化字符在原文的下标。"""
    out: list[str] = []
    pos: list[int] = []
    prev_space = True
    for i, ch in enumerate(text):
        if ch.isspace():
            if not prev_space:
                out.append(" ")
                pos.append(i)
                prev_space = True
            continue
        out.append(ch)
        pos.append(i)
        prev_space = False
    return "".join(out), pos


def _longest_prefix_in(needle: str, haystack: str) -> int:
    """二分求最大 L，使 needle[:L] 是 haystack 的子串。"""
    lo, hi, best = 0, len(needle), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if mid and needle[:mid] in haystack:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def strip_reasoning_echo(text: str, reasoning: str):
    """用已知思考文本裁掉漏进正文的思考前缀。

    判定「正文开头逐字出现在 reasoning_content 里」：重合够长且占比够高才动手，
    避免把角色正文里恰好被思考引用过的短句误删。返回 (文本, 是否改动)。
    """
    if not text or not reasoning:
        return text, False
    needle, pos = _norm_with_map(text)
    haystack, _ = _norm_with_map(reasoning)
    if len(needle) < ECHO_MIN_OVERLAP or not haystack:
        return text, False
    overlap = _longest_prefix_in(needle, haystack)
    if overlap < ECHO_MIN_OVERLAP:
        return text, False
    if overlap / len(needle) < ECHO_MIN_RATIO:
        return text, False
    cut = pos[overlap - 1] + 1
    rest = text[cut:]
    # 裁完可能只剩一个孤立的闭合标签，交给标签清洗收尾。
    rest, _ = strip_thinking(rest)
    if not rest.strip():
        # 整段都是思考，没有角色正文；保持原样让上层决定，不静默吞回复。
        return text, False
    return rest, True


def strip_thinking_chain(chain):
    """把连续 Plain 合成整段清洗，保留非 Plain 组件位置与首个 Plain 的对象 id。

    返回是否发生改动；无改动时不动 chain（避免影响 id 追踪）。
    """
    if not chain:
        return False
    changed = False
    index = 0
    while index < len(chain):
        if not isinstance(chain[index], Plain):
            index += 1
            continue
        end = index
        while end < len(chain) and isinstance(chain[end], Plain):
            end += 1
        merged = "".join(c.text for c in chain[index:end])
        cleaned, hit = strip_thinking(merged)
        if hit:
            changed = True
            chain[index].text = cleaned
            for component in chain[index + 1:end]:
                component.text = ""
        index = end
    if changed:
        chain[:] = [c for c in chain
                    if not isinstance(c, Plain) or c.text.strip()]
    return changed


def strip_reasoning_chain(chain, reasoning: str):
    """对 chain 里的 Plain 做 reasoning 差分裁剪。返回 (是否改动, 裁掉字符数)。

    分两种情形：
    - 整段 Plain 都是思考（归一化后完整出现在 reasoning 里）→ 清空该段；
    - 段内思考与正文混合 → 只裁掉与 reasoning 重合的前缀。
    若所有正文段都被判成思考，则整体放弃，避免把回复静默吞成空。
    """
    if not chain or not reasoning:
        return False, 0
    haystack, _ = _norm_with_map(reasoning)
    if not haystack:
        return False, 0
    targets = [c for c in chain if isinstance(c, Plain) and c.text]
    if not targets:
        return False, 0

    plans = []
    for component in targets:
        text = component.text
        needle, _ = _norm_with_map(text)
        # 整段都是思考：这段逐字来自 reasoning。
        if len(needle) >= ECHO_MIN_OVERLAP and needle in haystack:
            plans.append(("", len(text)))
            continue
        cleaned, hit = strip_reasoning_echo(text, reasoning)
        plans.append((cleaned, len(text) - len(cleaned)) if hit else (text, 0))

    if all(not new.strip() for new, _ in plans):
        return False, 0

    changed = False
    removed = 0
    for component, (new_text, cut) in zip(targets, plans):
        if not cut:
            continue
        component.text = new_text
        changed = True
        removed += cut
    if changed:
        # 清掉被判成纯思考而变空的段，避免下游再拿空 Plain 参与分段。
        chain[:] = [c for c in chain
                    if not isinstance(c, Plain) or c.text.strip()]
    return changed, removed


def strip_thinking_contexts(contexts):
    """清掉历史 assistant 消息里残留的思考段，避免模型持续模仿输出思考。

    只认 OpenAI messages 的 dict 形态，其他结构原样跳过；返回清理条数。
    """
    count = 0
    for message in contexts or []:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            cleaned, hit = strip_thinking(content)
            if hit:
                message["content"] = cleaned
                count += 1
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                raw = str(part.get("text") or "")
                cleaned, hit = strip_thinking(raw)
                if hit:
                    part["text"] = cleaned
                    count += 1
    return count
