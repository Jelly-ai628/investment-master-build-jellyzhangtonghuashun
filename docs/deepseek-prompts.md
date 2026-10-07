# DeepSeek 提示词说明

一次请求调用 DeepSeek 的环节分五类：识别意图；不需要取数的问题直接回答；规划取数；撰写回答；以及撰写未通过核验时的备用方案“选择观点”。所有请求都用**强制工具调用**或白名单工具，模型通过工具参数返回结构化内容。用户看到的回答正文（一句话结论和 1–4 段说明）、主要矛盾、待验证假设和继续研究的问题由 DeepSeek 撰写，服务端逐段核对后才展示；已核验事实、状态标签和判断改变条件始终由代码生成。模型为 `DEEPSEEK_MODEL`（当前 `deepseek-flash`），均关闭 thinking。

合规拦截在这些请求之前，由正则完成（`research.py` 的 `asks_for_prohibited_output`）；被拦截的问题不会调用 DeepSeek。

以下文字与代码一致；代码修改时以代码为准。

## 1. 识别意图（`intent.py` 的 `classify_question`）

- 参数：`max_tokens=500`；`tool_choice` 强制调用 `identify_research_intent`。
- 工具描述：识别用户真正要解决的问题，不规划无关的大盘模板。
- 返回结构（`ResearchIntent`）：`task`（market_overview / sector_ranking / named_comparison / index_research / risk_research / historical_comparison / valuation / events / single_security / explanation / chat / clarification）、`entities`（最多 5 个）、`entity_codes`（与 entities 对应的 6 位代码线索，可空）、`sector_universe`、`explanation_topic`。

**system**

```text
只识别问题意图，不回答问题。
- 询问哪些板块/行业最近更强、势头较好、领涨、热点或排名：sector_ranking，不能是market_overview。没有指定具体板块时entities留空，绝不自选银行或半导体。板块未明确概念时先按industry。
- 对指定行业/指数的比较：named_comparison，利用上下文解析“第一个/它/这些”。
- 询问具体公司或单只股票（如贵州茅台、宁德时代、中国移动、600519）的涨跌、走势、表现、估值：single_security，entities写用户说的证券名，不能改成指数或market_overview。entity_codes按entities的顺序填写你确知的6位A股代码，不确定就填空字符串；代码只用于检索，服务端会核对名称。
- 询问近期或末日的涨跌停分布、风险信号、情绪、极端交易等实际市场情况：risk_research。
- 市场整体状态：market_overview。
- 问概念含义（如“什么是市场宽度”“PE是什么意思”）：explanation。
- 问本产品能做什么、怎么用、能研究哪些股票或指数，打招呼、闲聊，或其他不需要取行情数据就能回答的一般问题：chat。
- 想研究行情但对象确实无法确定：clarification。
entities只写用户或上下文中明确的实体名，不编造。
```

**user**（JSON）

```json
{"question": "用户问题", "context": "同一对话上一轮的问题、截至日与指数", "selected_window": 20}
```

返回后由代码兜底：非定义式问法却含"风险、情绪、涨停、跌停、极端"时，从 explanation / chat 改为 risk_research（`override_explanation`）。

`entity_codes` 只是检索线索：扶摇代码表按名称检索不到时，服务端再按这个代码检索，**只有代码表里该代码的名称与用户所说的名称一致或互相包含时才采用**（`market_data.py` 的 `resolve_security`）。简称只在代码表中唯一包含它时采用，并在回答里写明匹配结果；对应多只股票时返回候选让用户选。

## 1b. 不需要取数的问题（`chat.py` 的 `answer_without_data`，最多 2 次）

task 为 explanation、chat、clarification，或问概念板块排名时走这一步，不调用任何数据接口。

- 参数：`max_tokens=1200`；`tool_choice` 强制调用 `answer_without_data`；返回 `answer`（4–1500 字）与 `followups`（展示前 3 个合格的）。多余字段忽略；模型直接用正文回答时也按同样规则核验。

**system**（`{capabilities}` 为 `chat.py` 中的 `CAPABILITIES`，列出产品能研究和不能研究的内容）

```text
你是“市场研判”产品里的研究助手，现在回答一个不需要取行情数据的问题（产品能力、使用方法、概念解释、打招呼或其他一般问题）。

{capabilities}

回答要求：
1. 用自然、简洁的中文直接回答，像同事之间说话，通常2–6句；需要列举时可以用简短的分行，不用标题。
2. 本轮没有调用任何数据接口：不要写任何具体的点位、涨跌幅、成交额、估值数值，也不要描述某个日期的市场表现。用户需要数据时，告诉他怎么问，产品会实际取数。
3. 解释概念时说清它衡量什么、怎么看、有什么局限；可以用抽象的例子，例如“100只股票里60只上涨”，但不要说成真实行情。
4. 问题超出产品范围时，直接说明做不到，并给出最接近的可研究问题。
5. 不预测涨跌，不给买卖、仓位、抄底、止损、目标价等任何操作上的说法，不用“建议”这个词，也不写“不构成投资建议”之类的套话。
6. followups给2–3个用户可以直接点击的研究问题，必须是上面“能研究”范围内的问句。
```

**服务端核验**（`check_chat`）：回答中不能出现带单位的具体数值（%、个百分点、点、亿、倍）和具体日期，因为本轮没有取数；不能出现预测与操作措辞，但“不提供买入建议”这类否定式说明不算。越界或格式不对的继续研究问题直接删掉，不导致整篇被拒；一条都不剩时用默认问题。第 1 次未通过时把问题发回重写；第 2 次仍未通过，退回固定说明（`author: rules`）。每一稿的拒绝原因写入 `work/validation-attempts/<run_id>-chat-<n>.json`。界面标注“DeepSeek 回答 · 本轮未取数”。

## 2. 规划取数（`research.py` 的 `run_research`，最多 4 轮）

- 参数：`max_tokens=1400`；`tools` 为本次问题允许的白名单工具，模型可自行决定调用顺序。
- 每轮最多共 10 次工具调用；参数逐项校验（工具名、参数键、代码或行业必须在服务端给定的枚举中，`purpose` 不超过 300 字）。

**system（第 1 条）** = 按问题加载的研究技能全文，依次拼接（见 `research-skills/`，加载规则见 `select_skills`），末尾追加：

```text
你是只读取证的市场研究Agent。按已识别的问题意图选择工具，不套用大盘报告。行业排名必须用get_sector_ranking，不能拿宽基指数或随意选几个行业代替。每个purpose说明取数用途，不输出内部思考。已取得的工具不要重复调用。接口失败明确记录。研究个股时用get_stock_history取该股行情，沪深300只作为大盘对照，不能用指数代替个股。
```

整体问题如果只问部分维度（如"只说明……市场宽度和指数表现"），不再开放风险、估值、事件工具。例如"最近20个交易日A股的行情结构和市场参与度如何？"会加载 `research-protocol`、`market-state`、`risk-context`、`valuation-boundary`、`event-context`，合计约 4900 字。

**user**（JSON）

```json
{"question": "用户问题", "conversation_context": "", "verified_end_date": "2026-09-30", "return_intervals": 20,
 "comparison_end": null, "selected_industries": [], "selected_securities": {}}
```

截至日、窗口、行业和股票代码都由服务端事先确认，模型不能改。

**system（第 2 条）**：服务端计算的最低必需工具集，例如

```text
完成本次问题至少需要尝试这些工具：["get_event_context:", "get_index_history:000300.SH", "get_market_breadth:", "get_risk_context:", "get_valuation_context:000300.SH"]
```

之后每次工具调用的结果以 `tool` 消息回填（证据 ID、类型、字段摘要、来源、观察日期；不含逐日序列）。必需工具没有全部尝试时，研究以 `incomplete_research_plan` 失败，不出结论。

**可用工具**（按问题开放其中一部分）：`get_index_history`、`get_market_breadth`、`get_sector_ranking`、`get_industry_history`、`get_stock_history`、`get_stock_valuation`、`compare_history`、`get_risk_context`、`get_valuation_context`、`get_event_context`。每个工具的描述写明口径和边界，例如 `get_valuation_context`："经iFinD读取宽基指数在研究截至日的PE(TTM)与PB，核对日期参数与单位；只给当前水平，不给历史分位或高低估结论。"

## 3. 撰写回答（`narrative.py`，最多 3 次）

- 参数：`max_tokens=3000`；`tool_choice` 强制调用 `write_grounded_narrative`。距研究开始超过 150 秒时不再重写（`WRITER_DEADLINE`），以免超过 240 秒的任务上限。
- 工具描述：根据已核验的证据写出给用户看的回答、待验证假设和继续研究的问题；每段都要列出依据的观点ID。
- 返回结构（`GroundedNarrative`）：
  - `headline`：一句话结论，纯文本字符串（80 字以内，提示要求 40 字以内），另用 `headline_insight_ids` 列出依据的观点（嵌套的 headline 对象在实测中多次写坏，因此改为平铺）；
  - `paragraphs`：1–4 段正文，每段带 `insight_ids`（1–6 个）和 `chart`（这段配的图：`indices`、`breadth`、`sectors` 中本次有数据的，或 `none`）；
  - `main_tension`：纯文本字符串，另用 `tension_id`（候选矛盾 ID 的枚举）和 `tension_insight_ids` 列出依据（同样因嵌套对象实测常写坏而平铺）；
  - `hypotheses`：1–3 条，每条含 `check`（验证需要的证据）；
  - `followups`：2–4 个问题。
  - 所有 `insight_ids` 的取值被限定为本次候选观点 ID 的枚举。

**system** = 研究技能中与读证据有关的部分（`writer_skill` 去掉“取数计划”“标准流程”“问题与证据组合”“选择与排序”各节），末尾追加：

```text
你负责写出用户最终看到的回答。读者是关心A股的普通投资研究用户，你像一位资深研究员当面跟同事讲：用平实的口语，先回答他问的问题，再讲为什么，最后讲哪里还说不准。

写作依据：insights（候选观点，text是数据说明了什么，caveat是它不能说明什么）、facts（已核验事实）、tensions（候选矛盾）都已由服务端从原始字段计算并核验，只能依据它们写。focus 说明用户这次问的是什么。

1. headline：一句话直接回答用户的问题（40字以内），不要复述问题，不要写成标题党。
2. paragraphs：按问题需要写1–4段连贯的正文，不用项目符号和小标题。简单问题1–2段就够。
   - 每段第一句就是这段的观点，后面跟支撑它的一两个关键数字；不要把观点里的数字逐条照搬，一段里的数字最好不超过三个。
   - 只写和 focus 有关的证据；与问题无关的维度（例如没问估值时的估值、没问事件时的事件）不必写，产品会在“证据与核验”里单独列出。
   - caveat 只在会影响读者理解的地方用半句话带过，全文同一类提醒只说一次；不要单独写一段“口径与局限”，产品会另外展示数据边界和判断改变条件。
   - 术语第一次出现时顺手用大白话解释（例如“上涨占比，就是有多少比例的股票收涨”）。
   - 不要用“先给结论”“再看”“翻译成大白话”“需要说明的是”“综上所述”“值得注意的是”这类套话开头。
3. 每段在 insight_ids 中列出它依据的观点ID；chart 填这段最适合配的图（只能用给出的选项，不需要就填 none），同一张图只配一次。
4. 数字和日期必须来自所引观点或事实：可以四舍五入（如-5.50%写成跌了5.5%、16081亿元写成约1.6万亿元），正负方向不能写反；不要相加、相减或推出原文没有的新数字。日期可以写成“9月30日”。
5. main_tension：当前最影响判断的一组矛盾，用一两句话说清，tension_id 填你参考的那一条候选矛盾。优先选 kind 为 data 的矛盾（数据之间真实的背离）；只有没有这类矛盾时，才用 kind 为 method 的口径类矛盾。
6. hypotheses：1–3条待验证的假设，即数据背后可能的原因或机制，写成假设；check 写明需要什么证据才能验证。
7. followups：2–4个用户接下来可以继续研究的问题，以问号结尾，而且必须是本产品现在能取数回答的：指数、行业或个股的区间行情与相互比较（大小盘风格、指定行业、相对沪深300），行业排名，最新交易日的市场宽度与涨跌停，与历史同长度区间的对比，宽基指数截至日的 PE/PB 水平，央行公开市场操作报道。不要问逐日宽度变化、估值历史分位、资金流向、融资余额、宏观数据等本产品取不到的内容。
8. 不预测涨跌，不写“将会、预计、有望、大概率”等判断未来的词；不给买卖、仓位、抄底、止损、目标价等任何操作上的说法，也不要用“建议”这个词或“不构成投资建议”之类的套话；不把同时出现写成因果；证据缺失的维度直接说没有取得，不用常识补齐。
```

**user**（JSON）：

```json
{"question": "用户问题",
 "focus": {"task": "market_overview", "objects": ["沪深300"], "asked_dimensions": ["行情结构", "市场宽度"], "matched_securities": []},
 "as_of": "2026-09-30", "window": 20, "market_state": "代码生成的状态标签",
 "facts": [{"text": "已核验事实", "evidence_ids": ["…"]}],
 "insights": [{"id": "participation", "text": "2026-09-30有效样本5560家中，上涨2567家、下跌2823家、平盘170家，上涨占比46.17%。", "caveat": "这只是一个交易日的截面，不能说明整个区间的参与度。", "dimension": "市场宽度", "evidence_ids": ["…"]}],
 "tensions": [{"id": "price_vs_breadth", "kind": "data", "text": "沪深300区间首末变化-5.50%……但末日上涨占比仍有46.17%……", "evidence_ids": ["…"]}],
 "not_obtained": ["风格轮动：尚未比较"], "tool_failures": []}
```

- `focus.asked_dimensions` 由问题中的关键词得出（行情、宽度、风格、估值、成交、情绪、事件、历史），告诉模型这次问的是什么。
- 候选观点的 `text` 只写数据说明了什么，`caveat` 写它不能说明什么，避免每句话都拖一条免责说明。
- 候选矛盾的 `kind`：`data` 是数据之间真实的背离（指数走弱而上涨占比不低 `price_vs_breadth`、缩量下跌等 `activity_vs_price`、涨跌停与指数方向相反 `sentiment_vs_price`、比较对象分化 `divergence`、个股相对抗跌但自身仍弱 `stock_vs_market`、累计靠前的行业近 5 日回落 `sector_ranking_scope`）；`method` 是口径类说明（`different_windows`、`evidence_gaps`）。

**收稿前的整理**：模型把一份回答拆成多次 `write_grounded_narrative` 调用时，合并成一份再核验（写坏的那次丢弃）；没有工具调用但正文里有 JSON 时，也按草稿核验；`headline` 为字符串时按平铺格式还原。段首“先直接说结论：”“再看……：”这类不含内容的开头会被去掉，不改动任何数字和引用。

**服务端核验**（`check_narrative`），任何一条不满足即拒绝：

1. 引用的观点 ID、矛盾 ID 都必须存在。
2. 每段文字中的数字都必须来自这段所依据的观点（含 caveat）或同一证据的已核验事实，**允许按所写精度四舍五入**（-5.50% 写成 5.5%，16081 亿元写成 1.6 万亿元）；10 以内的整数如果不带单位（%、个百分点、倍、亿、元、点、家、只）可以直接写；5/10/20/30/60/120/250 后接“日、天、交易日、周”视为窗口或指标名称。日期（2026-09-30 或 9月30日）必须出现在依据中。研究窗口、截至日和指数、行业、股票名称视为研究范围，可以直接写。写了正负号的要与原文一致；原文为负值时不能写成“上涨、上升、放大、强于”等，原文为正值时不能写成“下跌、回落、缩小、弱于”等。
3. 不得出现预测、确定性和操作建议措辞：建议、买入、卖出、加仓、减仓、仓位、抄底、止损、目标价、将会、预计、有望、大概率、必然、见底、逢低等。
4. 同一张图只能配一次。
5. 用户问的维度必须被回答（与选择观点相同的覆盖规则，例如行业排名问题的结论或第一段必须引用 `sector_leaders`）。
6. 继续研究的问题：不是 6–40 字问句或触发合规拦截的，直接删掉，不导致整篇重写；剩下不足 2 个时用默认问题补齐。

**未通过时**，追加 assistant（模型上次的返回）和 user：

```text
解读未通过服务端核验，请修正后重新调用 write_grounded_narrative，参数必须是合法JSON：<逐条问题，最多 8 条>。数字和日期只能来自所依据的观点或事实（可以四舍五入，正负方向不能写反）；没有依据的数字直接删掉。
```

通过后，界面以 DeepSeek 写的结论和段落作为回答正文（标注“DeepSeek 撰写 · 引用与数字已核验”），每段附所引证据的角标，图表放在模型指定的段落之后；继续研究的问题替换默认问题。已核验事实、七维状态和数据边界收进可展开的“证据与核验”。正文没有涉及的估值、风险、事件观点，由代码列在“证据与核验”里的“与本问题关系较远的已核验观点”，标为“代码补充”。

## 4. 选择观点（仅在撰写都未通过时，最多 2 次）

- 参数：`max_tokens=600`；`tool_choice` 强制调用 `select_verified_insights`。
- 工具描述：选择与问题相关的已核验观点，不能新增或改写结论。
- 参数 schema 中 `summary_ids`、`detail_ids` 的取值被限定为本次候选观点 ID 的枚举，`tension_id` 限定为候选矛盾 ID 的枚举；`summary_ids` 1–3 条，`detail_ids` 1–10 条。

**system** = 与第 2 步相同的研究技能全文，末尾追加：

```text
根据用户问题，从已计算并可追溯的候选观点中选择最相关的摘要、主要矛盾和展开顺序。不要改写、补造或返回文字，只返回JSON选择。summary_ids必须包含在detail_ids中，不得重复；仅使用候选id。字段为summary_ids数组、tension_id字符串、detail_ids数组。
```

**user**（JSON）：问题，加上代码生成的候选观点与候选矛盾（每条含 `id`、`text`、`dimension`、`evidence_ids`、推导数值）。

```json
{"question": "用户问题",
 "insights": [{"id": "index_0", "text": "沪深300在2026-09-01至2026-09-30的首末点位变化-5.50%，区间最大回撤-5.87%；……", "caveat": "均线与回撤只描述已经走过的价格路径。", "dimension": "行情结构", "evidence_ids": ["…"], "derivation": {"return_pct": -5.5}}],
 "tensions": [{"id": "different_windows", "kind": "method", "text": "区间指数变化覆盖整段时间，市场宽度只覆盖最后一个交易日……", "evidence_ids": ["…"]}]}
```

**第 1 次未通过校验时**，追加 assistant（模型上次的返回）和 user：

```text
选择未通过校验：<原因>。summary_ids为1至3条，detail_ids为1至10条；summary_ids与detail_ids只能使用insights中的id：<候选观点ID列表>；tension_id只能使用：<候选矛盾ID列表>。
```

第 2 次仍未通过时不再重试，研究状态为 `facts_only`，只展示已核验事实。通过后由代码按 ID 渲染文字（`render_selection`）：摘要只用观点的 `text`，展开中的每条附上 `caveat`；模型选错的非观点 ID 只会被删除，不会新增内容；已取得证据但模型没选的估值、风险、事件观点，单独列为“与本问题关系较远的已核验观点”，不混进回答。

## 记录在哪里

每次研究的意图、解读作者、模型选择和被剔除的 ID 写入 `work/research-runs/<run_id>.json`（`intent`、`narrative.author`、`selected_insights`、`skills_loaded`）；撰写或选择未通过核验时，模型原始返回和逐条问题写入 `work/validation-attempts/`（`<run_id>-narrative-<n>.json`、`<run_id>-selection-<n>.json`）。两处都在 `.gitignore` 中，不进入仓库。
