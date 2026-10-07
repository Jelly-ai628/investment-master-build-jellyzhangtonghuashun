"""Questions that need no market data are answered by DeepSeek in plain language; no data is fetched or stated."""

import json
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .connections import DEEPSEEK, ProbeError, first_message, request_json
from .narrative import FORBIDDEN

CAPABILITIES = """本产品是只读取证的A股市场研究助手，每次研究都会实际调用数据接口，并让结论可以追溯到原始字段。
能研究：
- 宽基指数：沪深300、中证500、中证1000、上证指数、创业板指在5/10/20/60个交易日窗口里的首末变化、最大回撤、相对20/60日均线的位置、区间高低点位置；大小盘风格比较。
- 行业：同花顺881系列行业指数的区间表现排名，以及指定行业（如银行、半导体）之间、与沪深300的比较。
- 个股：沪深北A股，用简称或6位代码提问即可（按扶摇A股代码表匹配）。可以看前复权行情、与沪深300同区间的相对表现、最新PE/PB估值快照。所属行业、财务报表、公司公告和资金流目前没有接入。
- 市场宽度：最新完整交易日的上涨、下跌、平盘家数；最近约10个交易日的全市场成交额；涨跌停分布。
- 估值：沪深300等宽基在截至日的PE(TTM)与PB（来自iFinD），没有历史分位。
- 事件：研究区间末段央行公开市场操作相关报道（来自iFinD新闻，并核对原网页）。
- 历史阶段：当前区间与历史同长度区间的对比。
不能做：预测涨跌、收益承诺、买卖时机、目标价、止损位、仓位建议；个股筛选、个股涨幅排名、选股或荐股名单（可以改看行业排名，或研究用户点名的某只股票）；美股、港股、基金、商品与加密资产研究。"""

CHAT_INSTRUCTIONS = """你是“市场研判”产品里的研究助手，现在回答一个不需要取行情数据的问题（产品能力、使用方法、概念解释、打招呼或其他一般问题）。

{capabilities}

回答要求：
1. 用自然、简洁的中文直接回答，像同事之间说话，通常2–6句；需要列举时可以用简短的分行，不用标题。
2. 本轮没有调用任何数据接口：不要写任何具体的点位、涨跌幅、成交额、估值数值，也不要描述某个日期的市场表现。用户需要数据时，告诉他怎么问，产品会实际取数。
3. 解释概念时说清它衡量什么、怎么看、有什么局限；可以用抽象的例子，例如“100只股票里60只上涨”，但不要说成真实行情。
4. 问题超出产品范围时，直接说明做不到，并给出最接近的可研究问题。
5. 不预测涨跌，不给买卖、仓位、抄底、止损、目标价等任何操作上的说法，不用“建议”这个词，也不写“不构成投资建议”之类的套话。
6. followups给2–3个用户可以直接点击的研究问题，必须是上面“能研究”范围内的问句。"""


class ChatAnswer(BaseModel):
    # Extra keys are ignored: only the answer text is shown, and its content is what gets checked.
    model_config = ConfigDict(extra="ignore")
    answer: str = Field(min_length=4, max_length=1500)
    followups: list[str] = Field(default_factory=list, max_length=8)


# Without fetched evidence, any market figure in the answer would be invented.
MARKET_FIGURE = re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|个百分点|点(?!击)|亿|万亿|倍)|\d{4}-\d{2}-\d{2}|\d{4}年\d{1,2}月\d{1,2}日")
NEGATED = re.compile(r"(?:不|没|无法|不能|不会|不提供|不给|不做|不涉及|不包括)[^，。；！？\n]{0,8}$")
DEFAULT_FOLLOWUPS = ["贵州茅台最近20个交易日表现如何？", "最近有哪些表现较强的行业板块？", "最近20个交易日市场整体状态如何？"]


def advice_words(text: str) -> list[str]:
    # Saying what the product will not do ("不提供买入建议") is allowed; advice itself is not.
    return [match[0] for match in FORBIDDEN.finditer(text) if not NEGATED.search(text[max(0, match.start() - 12):match.start()])]


def check_chat(answer: ChatAnswer, prohibited) -> dict:
    problems = [f"含有不允许的措辞“{word}”" for word in advice_words(answer.answer)]
    figure = MARKET_FIGURE.search(answer.answer)
    if figure:
        problems.append(f"本轮未取数，不能写具体数值“{figure[0]}”，请删掉数字或换成抽象说法")
    if problems:
        raise ProbeError("chat_answer_rejected:" + "；".join(problems[:4]))
    # A follow-up outside the boundary is dropped rather than failing the whole answer.
    followups = [q for q in dict.fromkeys(q.strip() for q in answer.followups)
                 if 6 <= len(q) <= 40 and q.endswith(("？", "?")) and not prohibited(q) and not advice_words(q)][:3]
    return {"answer": answer.answer.strip(), "followups": followups or DEFAULT_FOLLOWUPS}


async def answer_without_data(client, settings, question: str, context: str, prohibited, record=None) -> dict:
    tool = {"type": "function", "function": {"name": "answer_without_data", "description": "直接回答不需要取数的问题，并给出可继续研究的问题。",
            "parameters": ChatAnswer.model_json_schema()}}
    messages = [{"role": "system", "content": CHAT_INSTRUCTIONS.format(capabilities=CAPABILITIES)},
                {"role": "user", "content": json.dumps({"question": question, "conversation_context": context}, ensure_ascii=False)}]
    problems = []
    for attempt in range(2):
        response = await request_json(client, "POST", DEEPSEEK + "/chat/completions",
            headers={"Authorization": "Bearer " + settings.get("DEEPSEEK_API_KEY")},
            json={"model": settings.get("DEEPSEEK_MODEL"), "thinking": {"type": "disabled"}, "max_tokens": 1200, "messages": messages,
                  "tools": [tool], "tool_choice": {"type": "function", "function": {"name": "answer_without_data"}}})
        content = None
        try:
            reply = first_message(response)
            calls = reply.get("tool_calls") or []
            if len(calls) == 1 and calls[0].get("function", {}).get("name") == "answer_without_data":
                content = calls[0]["function"]["arguments"]
            elif isinstance(reply.get("content"), str) and reply["content"].strip():
                # A plain-text reply is still an answer; it goes through the same checks.
                content = json.dumps({"answer": reply["content"].strip()}, ensure_ascii=False)
            else:
                raise ProbeError("chat_answer_rejected:没有调用 answer_without_data")
            try:
                draft = ChatAnswer.model_validate_json(content)
            except ValidationError as error:
                raise ProbeError("chat_answer_rejected:" + "；".join(f"{'.'.join(map(str, e['loc']))}：{e['msg']}" for e in error.errors()[:4])) from None
            return check_chat(draft, prohibited)
        except ProbeError as error:
            problems.append(error.category.split(":", 1)[-1])
            if record:
                record(attempt, {"problems": problems[-1], "content": content, "not_for_display": True})
            messages.append({"role": "assistant", "content": content if isinstance(content, str) else "{}"})
            messages.append({"role": "user", "content": "回答未通过服务端核验，请修正后重新调用 answer_without_data：" + problems[-1]})
    raise ProbeError("chat_answer_rejected:" + " / ".join(problems))
