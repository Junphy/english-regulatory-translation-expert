# agent_core.py
"""
两步翻译流程：译者（中文 -> 英文）-> 审校（一次调用同时输出 Minimalist Correction 与 Polished Academic English）。

- 第 1 步的 user 消息是清理后的中文原文，system 消息是方案里的 translator 规则。
- 第 2 步的 user 消息是第 1 步的英文译文（审校看不到原文），system 消息是方案里的 reviewer 规则。
- 发给模型之前，原文里的“￼”（U+FFFC，对象替换字符，常是复制粘贴带进来的标签替身）会被删除，
  并把由此产生的连续空格合并成一个。导出记录里保存的仍是用户输入的原文。
- 目前只支持中译英。输入里几乎没有汉字（主要是英文）时直接提示，不调用模型。
"""
import re
from typing import Callable, Tuple

from openai import OpenAI

DIRECTION_LABEL = "中译英"


class EmptyCompletionError(Exception):
    """模型返回了空内容（常见于被安全策略拦截，或输出被截断）。"""

    def __init__(self, model, finish_reason=None):
        self.model = model
        self.finish_reason = finish_reason
        reason = f"finish_reason={finish_reason}" if finish_reason else "接口没有给出原因"
        super().__init__(
            f"模型 {model} 返回了空内容（{reason}）。常见原因：输入或输出被安全策略拦截，或输出被截断。"
            "可以换一个模型，或缩短、拆分输入后重试。"
        )


class UnsupportedInputError(Exception):
    """输入不是中文（目前只支持中译英）。不会调用模型。"""


class ReviewFormatError(Exception):
    """审校输出里找不到两个约定的小节标题，无法拆成两个版本。raw 保存模型的原始输出。"""

    def __init__(self, raw):
        self.raw = raw
        super().__init__("审校输出里没有找到“1. Minimalist Correction”与“2. Polished Academic English”两个小节标题，无法拆分。")


class StepError(Exception):
    """某一步失败，带上步骤序号、原始异常，以及失败前已经完成的内容（partial，如第 1 步的译文）。"""

    def __init__(self, step, label, cause, partial=None):
        self.step = step
        self.label = label
        self.cause = cause
        self.partial = partial or {}
        super().__init__(f"第 {step} 步（{label}）调用失败：{cause}")


# ==================== 输入处理 ====================
_OBJ_REPLACEMENT = "￼"
_HAN_CHAR = re.compile(r"[一-鿿]")
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")


def clean_source(text: str) -> str:
    """删除“￼”，并把删除后留下的连续空格或制表符合并成一个空格（不动换行）。"""
    if _OBJ_REPLACEMENT not in text:
        return text
    cleaned = text.replace(_OBJ_REPLACEMENT, "")
    return re.sub(r"[ \t]{2,}", " ", cleaned)


def check_chinese(text: str) -> None:
    """汉字个数不少于英文单词个数才视为中文；否则抛出 UnsupportedInputError。"""
    han = len(_HAN_CHAR.findall(text))
    latin_words = len(_LATIN_WORD.findall(text))
    if han == 0 or han < latin_words:
        raise UnsupportedInputError("目前只支持中译英：输入里的汉字很少，看起来主要是英文。请输入中文原文。")


# ==================== 模型调用 ====================
def get_completion(client, user_message: str, system_message: str, model: str, temperature: float = 0.3) -> str:
    """调用 OpenAI 兼容接口，返回模型文本。system 消息为空时不发送 system 消息。"""
    messages = [{"role": "user", "content": user_message}]
    if (system_message or "").strip():
        messages.insert(0, {"role": "system", "content": system_message})
    response = client.chat.completions.create(
        model=model,
        temperature=temperature,
        messages=messages,
    )
    choices = response.choices or []
    content = choices[0].message.content if choices else None
    if content is None or not str(content).strip():
        raise EmptyCompletionError(model, getattr(choices[0], "finish_reason", None) if choices else None)
    return content


def make_llm(client, model: str) -> Callable[[str, str], str]:
    """把 client 与 model 绑定成 llm(user_message, system_message) 调用。"""

    def llm(user_message: str, system_message: str) -> str:
        return get_completion(client, user_message, system_message, model)

    return llm


# ==================== 审校输出的拆分 ====================
# 小节标题允许带 # 或 * 修饰、前面的 emoji 可有可无、大小写不限，但必须单独占一行。
_H1 = re.compile(r"^[#*\s]*(?:📥\s*)?1\.\s*Minimalist Correction[#*:：\s]*$", re.M | re.I)
_H2 = re.compile(r"^[#*\s]*(?:🔍\s*)?2\.\s*Polished Academic English[#*:：\s]*$", re.M | re.I)
_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n\s*```\s*$", re.S)


def _unfence(text: str) -> str:
    m = _FENCE.match(text)
    return m.group(1).strip() if m else text


def parse_review(raw: str) -> Tuple[str, str]:
    """把审校的原始输出拆成 (Minimalist Correction, Polished Academic English)。拆不开时抛出 ReviewFormatError。"""
    text = _unfence((raw or "").strip())
    m1, m2 = _H1.search(text), _H2.search(text)
    if not m1 or not m2 or m1.end() > m2.start():
        raise ReviewFormatError(raw)
    minimal = _unfence(text[m1.end():m2.start()].strip())
    polished = _unfence(text[m2.end():].strip())
    if not minimal or not polished:
        raise ReviewFormatError(raw)
    return minimal, polished


# ==================== 流程 ====================
def run_two_step(source_text, model_name, api_key, base_url, translator_rules, reviewer_rules):
    """
    依次执行两步，返回 (译文, Minimalist Correction, Polished Academic English, 方向标签)。
    任何一步失败都抛出 StepError（partial 里带着已经完成的内容）；输入不是中文时抛出 UnsupportedInputError。
    """
    check_chinese(source_text)
    cleaned = clean_source(source_text)

    client = OpenAI(api_key=api_key, base_url=base_url)
    llm = make_llm(client, model_name)

    try:
        translation = llm(cleaned, translator_rules)
    except Exception as exc:
        raise StepError(1, "译者", exc) from exc

    partial = {"translation": translation}
    try:
        raw = llm(translation, reviewer_rules)
    except Exception as exc:
        raise StepError(2, "审校", exc, partial) from exc

    try:
        minimal, polished = parse_review(raw)
    except ReviewFormatError as exc:
        raise StepError(2, "审校", exc, {**partial, "review_raw": raw}) from exc

    return translation, minimal, polished, DIRECTION_LABEL
