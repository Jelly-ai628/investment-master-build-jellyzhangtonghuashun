"""DeepSeek writes the answer in plain language; code checks every paragraph against the verified evidence it cites."""

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Prediction, certainty and advice wording is rejected wherever the model writes.
FORBIDDEN = re.compile(r"建议|买入|卖出|加仓|减仓|建仓|仓位|抄底|止损|止盈|目标价|必涨|必跌|将会|将要|预计|有望|大概率|必然|一定会|势必|见底|收益承诺|保证收益|稳赚|低吸|逢低|值得配置")
RISING = re.compile(r"上涨|上行|上升|走高|增长|增加|放大|领先|强于|多于|跑赢")
FALLING = re.compile(r"下跌|回落|下降|走低|减少|缩小|萎缩|落后|弱于|少于|跑输")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# "9月30日", "2026年9月" and the short "09-30"; digits glued to a preceding number ("沪深300月末") are not a month.
CN_DATE = re.compile(r"(?:(\d{4})年)?(?<!\d)(\d{1,2})月(?:(\d{1,2})[日号])?")
SHORT_DATE = re.compile(r"(?<![\d-])()(\d{2})-(\d{2})(?![\d-])")
NUMBER = re.compile(r"(?<![\d.])([+\-−]?)(\d+(?:\.\d+)?)(\s*万亿)?")
# Look-back lengths name a window or an indicator ("60日均线"), not a data value.
PERIODS = {5, 10, 20, 30, 60, 120, 250}
PERIOD_UNIT = re.compile(r"\s*(?:个)?(?:交易日|日|天|周)")
# Small counts ("前五""5天里有2天") are ordinary words; with a market unit attached they are data and must be grounded.
MARKET_UNIT = re.compile(r"\s*(?:%|％|个百分点|倍|万亿|亿|万|元|点|家|只)")

WRITER_INSTRUCTIONS = """
你负责写出用户最终看到的回答。读者是关心A股的普通投资研究用户，你像一位资深研究员当面跟同事讲：用平实的口语，先回答他问的问题，再讲为什么，最后讲哪里还说不准。

写作依据：insights（候选观点，text是数据说明了什么，caveat是它不能说明什么）、facts（已核验事实）、tensions（候选矛盾）都已由服务端从原始字段计算并核验，只能依据它们写。focus 说明用户这次问的是什么。

1. headline：一句话直接回答用户的问题（40字以内，纯文本字符串），不要复述问题，不要写成标题党；headline_insight_ids 列出它依据的观点ID。只调用一次 write_grounded_narrative，所有字段放在同一个参数对象里。
2. paragraphs：按问题需要写1–4段连贯的正文，不用项目符号和小标题。简单问题1–2段就够。
   - 每段第一句就是这段的观点，后面跟支撑它的一两个关键数字；不要把观点里的数字逐条照搬，一段里的数字最好不超过三个。
   - 只写和 focus 有关的证据；与问题无关的维度（例如没问估值时的估值、没问事件时的事件）不必写，产品会在“证据与核验”里单独列出。
   - caveat 只在会影响读者理解的地方用半句话带过，全文同一类提醒只说一次；不要单独写一段“口径与局限”，产品会另外展示数据边界和判断改变条件。
   - 每段控制在150字左右；术语第一次出现时顺手用大白话解释（例如“上涨占比，就是有多少比例的股票收涨”）。均线是过去若干个交易日收盘价的平均，不是持仓成本，不要说成“成本线”或“入场的人处于不利位置”。
   - 段落之间靠内容衔接，不要用“先说……：”“再看……：”“最后”“首先”“其次”“翻译成大白话”“需要说明的是”“综上所述”“值得注意的是”这类套话开头。
3. 每段在 insight_ids 中列出它依据的观点ID；chart 填这段最适合配的图（只能用给出的选项，不需要就填 none），同一张图只配一次。
4. 数字和日期必须来自所引观点或事实：可以四舍五入（如-5.50%写成跌了5.5%、16081亿元写成约1.6万亿元），正负方向不能写反；不要相加、相减或推出原文没有的新数字。日期可以写成“9月30日”。
5. main_tension：当前最影响判断的一组矛盾，用一两句话说清（纯文本字符串）；tension_id 填你参考的那一条候选矛盾，tension_insight_ids 列出依据的观点ID。优先选 kind 为 data 的矛盾（数据之间真实的背离）；只有没有这类矛盾时，才用 kind 为 method 的口径类矛盾。
6. hypotheses：1–3条待验证的假设，即数据背后可能的原因或机制，写成假设；check 写明需要什么证据才能验证。
7. followups：2–4个用户接下来可以继续研究的问题，以问号结尾，而且必须是本产品现在能取数回答的：指数、行业或个股的区间行情与相互比较（大小盘风格、指定行业、相对沪深300），行业排名，最新交易日的市场宽度与涨跌停，与历史同长度区间的对比，宽基指数截至日的 PE/PB 水平，央行公开市场操作报道。宽基指数只有沪深300、中证500、中证1000、上证指数、创业板指。不要问逐日宽度变化、多日涨跌停、估值历史分位或“历史上处于什么位置”、资金流向、融资余额、宏观数据、其他指数（如上证50）等本产品取不到的内容。
8. 不预测涨跌，不写“将会、预计、有望、大概率”等判断未来的词；不给买卖、仓位、抄底、止损、目标价等任何操作上的说法，也不要用“建议”这个词或“不构成投资建议”之类的套话；不把同时出现写成因果；证据缺失的维度直接说没有取得，不用常识补齐。
""".strip()


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=4, max_length=320)
    insight_ids: list[str] = Field(min_length=1, max_length=4)


class Headline(Claim):
    text: str = Field(min_length=4, max_length=80)


class Paragraph(Claim):
    text: str = Field(min_length=10, max_length=480)
    insight_ids: list[str] = Field(min_length=1, max_length=6)
    chart: Literal["none", "indices", "breadth", "sectors"] = "none"


class Tension(Claim):
    tension_id: str


class Hypothesis(Claim):
    check: str = Field(min_length=4, max_length=200)


class GroundedNarrative(BaseModel):
    model_config = ConfigDict(extra="forbid")
    headline: Headline
    paragraphs: list[Paragraph] = Field(min_length=1, max_length=4)
    main_tension: Tension
    hypotheses: list[Hypothesis] = Field(min_length=1, max_length=3)
    followups: list[str] = Field(min_length=2, max_length=4)


class NarrativeRejected(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def inline(schema: dict) -> dict:
    # Tool schemas are sent without $ref so the provider does not need to resolve definitions.
    definitions = schema.pop("$defs", {})
    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(dict(definitions[node["$ref"].rsplit("/", 1)[-1]]))
            return {key: resolve(value) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(value) for value in node]
        return node
    return resolve(schema)


def available_charts(evidence: dict) -> list[str]:
    kinds = {item["kind"] for item in evidence.values()}
    charts = []
    if "index_history" in kinds:
        charts.append("indices")
    if any(item["kind"] == "market_breadth" and item["data"].get("quality") != "insufficient_coverage" for item in evidence.values()):
        charts.append("breadth")
    if "sector_ranking" in kinds:
        charts.append("sectors")
    return charts


def writer_tool(insights: dict, tensions: dict, charts: list[str]) -> dict:
    schema = GroundedNarrative.model_json_schema()
    for definition in schema["$defs"].values():
        if "insight_ids" in definition.get("properties", {}):
            definition["properties"]["insight_ids"]["items"]["enum"] = list(insights)
    schema["$defs"]["Tension"]["properties"]["tension_id"]["enum"] = list(tensions)
    schema["$defs"]["Paragraph"]["properties"]["chart"]["enum"] = ["none"] + charts
    schema["properties"]["headline"] = {"type": "string", "maxLength": 80, "description": "一句话直接回答用户的问题，40字以内"}
    schema["properties"]["headline_insight_ids"] = {"type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "string", "enum": list(insights)}}
    schema["properties"]["main_tension"] = {"type": "string", "maxLength": 320, "description": "当前最影响判断的一组矛盾，一两句话"}
    schema["properties"]["tension_id"] = {"type": "string", "enum": list(tensions)}
    schema["properties"]["tension_insight_ids"] = {"type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "string", "enum": list(insights)}}
    schema["required"] = ["headline", "headline_insight_ids", "paragraphs", "main_tension", "tension_id", "tension_insight_ids", "hypotheses", "followups"]
    return {"type": "function", "function": {"name": "write_grounded_narrative",
            "description": "一次调用写完整份回答：headline、paragraphs、main_tension、hypotheses、followups 放在同一个参数对象里；每段都要列出依据的观点ID。", "parameters": inline(schema)}}


def normalize_draft(value):
    # The wire format is flat (headline + headline_insight_ids): a nested headline object was repeatedly malformed live on 2026-10-07.
    if isinstance(value, dict) and isinstance(value.get("headline"), str):
        value = dict(value)
        ids = value.pop("headline_insight_ids", None)
        paragraphs = value.get("paragraphs") or []
        first = paragraphs[0].get("insight_ids") if paragraphs and isinstance(paragraphs[0], dict) else None
        value["headline"] = {"text": value["headline"], "insight_ids": ids if isinstance(ids, list) and ids else first or []}
    # main_tension is flat on the wire too (main_tension + tension_id + tension_insight_ids); the nested form failed in 6 of 13 live drafts.
    if isinstance(value, dict) and isinstance(value.get("main_tension"), str):
        value = dict(value)
        ids = value.pop("tension_insight_ids", None) or value.pop("insight_ids", None)
        headline = value.get("headline") if isinstance(value.get("headline"), dict) else {}
        value["main_tension"] = {"text": value["main_tension"], "tension_id": value.pop("tension_id", ""),
                                 "insight_ids": ids if isinstance(ids, list) and ids else headline.get("insight_ids") or []}
    return value


def merge_calls(arguments: list[str]) -> dict:
    # Live 2026-10-07: the model sometimes split one draft across several calls (headline in one, paragraphs in the next).
    merged, errors = {}, []
    for text in arguments:
        try:
            part = json.loads(text)
        except json.JSONDecodeError as error:
            errors.append(f"参数不是合法JSON：{error.msg}（第{error.pos}个字符附近）")
            continue
        if isinstance(part, dict):
            merged.update({key: value for key, value in part.items() if key not in merged})
    if not merged:
        raise NarrativeRejected(errors or ["参数为空"])
    return merged


def values_in(text: str) -> list[tuple[str, float]]:
    found = []
    for match in NUMBER.finditer(DATE.sub(" ", text)):
        found.append((match.group(1).replace("−", "-"), float(match.group(2)) * (1e4 if match.group(3) else 1)))
    return found


def ungrounded(text: str, source: str) -> list[str]:
    # A number may be a rounded copy of a cited value, never a new one; an unsigned copy of a signed value must not flip its direction.
    problems = [f"日期{date}不在所依据的观点或事实中" for date in DATE.findall(text) if date not in source]
    dates = {tuple(map(int, day.split("-"))) for day in DATE.findall(source)}

    def known_date(match):
        year, month, day = (int(group) if group else None for group in match.groups())
        if not any((year is None or year == y) and month == m and (day is None or day == d) for y, m, d in dates):
            problems.append(f"日期{match.group(0)}不在所依据的观点或事实中")
        return " "

    body = SHORT_DATE.sub(known_date, CN_DATE.sub(known_date, DATE.sub(" ", text)))
    body = re.sub(r"(\d{4})年", lambda m: " " if any(m.group(1) == str(y) for y, _, _ in dates) else m.group(0), body)
    available = values_in(source)
    for match in NUMBER.finditer(body):
        sign, digits, wan = match.group(1).replace("−", "-"), match.group(2), match.group(3)
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        scale = 1e4 if wan else 1
        value = float(digits) * scale
        if not decimals and not wan:
            if (value <= 10 and not MARKET_UNIT.match(body, match.end())) or (value in PERIODS and PERIOD_UNIT.match(body, match.end())):
                continue
        tolerance = 0.5 * 10 ** -decimals * scale + 1e-9
        signs = {s for s, v in available if abs(v - value) <= tolerance}
        if not signs:
            problems.append(f"数字{sign}{digits}{wan or ''}不在所依据的观点或事实中")
        elif sign and sign not in signs:
            problems.append(f"数字{sign}{digits}的正负号与原文不一致")
        elif not sign:
            before = body[max(0, match.start() - 6):match.start()]
            if signs == {"-"} and RISING.search(before):
                problems.append(f"{digits}在原文中是负值，不能写成{RISING.search(before)[0]}")
            if signs == {"+"} and FALLING.search(before):
                problems.append(f"{digits}在原文中是正值，不能写成{FALLING.search(before)[0]}")
    return problems


# Openers like "先直接说结论：" carry no content; removing them changes no claim, numbers or citations.
FILLER = re.compile(r"^(?:先直接说结论|先说结论|直接说结论|先说[^：:，,。]{0,8}|再看[^：:，,。]{0,8}|最后看[^：:，,。]{0,8}|首先|其次|最后|综上所述|总的来说)[：:，,]\s*")


def tidy(text: str) -> str:
    return FILLER.sub("", text.strip()) or text.strip()


def check_narrative(draft: GroundedNarrative, insights: dict, tensions: dict, prohibited, scope: str = "", facts: list | None = None) -> dict:
    # scope holds the research window, as-of date and instrument names: stating them is not new data.
    problems = []
    facts = facts or []

    def verify(label: str, claim: Claim, extra: str = "") -> list[str]:
        unknown = [key for key in claim.insight_ids if key not in insights]
        if unknown:
            problems.append(f"{label}引用了不存在的观点{unknown}")
            return []
        refs = list(dict.fromkeys(ref for key in claim.insight_ids for ref in insights[key]["evidence_ids"]))
        # Verified facts drawn from the same evidence may be quoted alongside the insights.
        related = [fact["text"] for fact in facts if set(fact["evidence_ids"]) <= set(refs)]
        source = " ".join([insights[key]["text"] + " " + insights[key].get("caveat", "") for key in claim.insight_ids] + related + [extra, scope])
        texts = [claim.text] + ([claim.check] if isinstance(claim, Hypothesis) else [])
        for text in texts:
            problems.extend(label + "：" + item for item in ungrounded(text, source))
            if FORBIDDEN.search(text):
                problems.append(f"{label}含有不允许的措辞“{FORBIDDEN.search(text)[0]}”")
        return refs

    if draft.main_tension.tension_id not in tensions:
        problems.append("main_tension引用了不存在的矛盾")
    headline_refs = verify("headline", draft.headline)
    paragraph_refs = [verify(f"paragraphs第{i}段", claim) for i, claim in enumerate(draft.paragraphs, 1)]
    tension_refs = verify("main_tension", draft.main_tension, tensions.get(draft.main_tension.tension_id, {}).get("text", ""))
    hypothesis_refs = [verify(f"hypotheses第{i}条", claim) for i, claim in enumerate(draft.hypotheses, 1)]
    charts = [claim.chart for claim in draft.paragraphs if claim.chart != "none"]
    if len(charts) != len(set(charts)):
        problems.append("同一张图只能配一次")
    # A follow-up that is too long or outside the boundary is dropped; it never costs a rewrite of the whole answer.
    followups = [question for question in dict.fromkeys(question.strip() for question in draft.followups)
                 if 6 <= len(question) <= 40 and question.endswith(("？", "?")) and not prohibited(question) and not FORBIDDEN.search(question)]
    if problems:
        raise NarrativeRejected(problems)
    return {"author": "deepseek",
            "headline": draft.headline.text,
            "summary": draft.headline.text,
            "summary_ids": list(dict.fromkeys(draft.headline.insight_ids + draft.paragraphs[0].insight_ids)),
            "summary_evidence_ids": headline_refs,
            "paragraphs": [{"text": tidy(claim.text), "evidence_ids": refs, "insight_ids": claim.insight_ids, "chart": claim.chart}
                           for claim, refs in zip(draft.paragraphs, paragraph_refs)],
            "main_tension": draft.main_tension.text, "tension_evidence_ids": tension_refs,
            "interpretations": [],
            "hypotheses": [{"text": claim.text, "check": claim.check, "evidence_ids": refs} for claim, refs in zip(draft.hypotheses, hypothesis_refs)],
            "followups": followups}
