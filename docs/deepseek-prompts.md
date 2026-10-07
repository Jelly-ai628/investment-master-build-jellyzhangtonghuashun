# DeepSeek 提示词说明

一次研究最多调用 DeepSeek 三类请求：识别意图、规划取数、选择观点。三类请求都用**强制工具调用**或白名单工具，模型只能返回结构化参数，不能返回给用户看的文字。模型为 `DEEPSEEK_MODEL`（当前 `deepseek-flash`），均关闭 thinking。

合规拦截在这三类请求之前，由正则完成（`research.py` 的 `asks_for_prohibited_output`）；被拦截的问题不会调用 DeepSeek。

以下文字与代码一致；代码修改时以代码为准。

## 1. 识别意图（`intent.py` 的 `classify_question`）

- 参数：`max_tokens=500`；`tool_choice` 强制调用 `identify_research_intent`。
- 工具描述：识别用户真正要解决的问题，不规划无关的大盘模板。
- 返回结构（`ResearchIntent`）：`task`（market_overview / sector_ranking / named_comparison / index_research / risk_research / historical_comparison / valuation / events / single_security / explanation / clarification）、`entities`（最多 5 个）、`sector_universe`、`explanation_topic`。

**system**

```text
只识别问题意图。询问哪些板块/行业最近更强、势头较好、领涨、热点或排名，应是sector_ranking，不能是market_overview。没有指定具体板块时entities留空，绝不自选银行或半导体。板块未明确概念时先按industry。对指定行业/指数的比较为named_comparison，利用上下文解析“第一个/它/这些”。询问具体公司或单只股票/基金（如贵州茅台、宁德时代、600519）的涨跌、走势、估值或前景时为single_security，entities写该证券名，不能改成指数或market_overview。问概念含义（如“什么是市场宽度”“PE是什么意思”）或打招呼用explanation，不需要行情取数；询问近期或末日的涨跌停分布、风险信号、情绪、极端交易等实际市场情况时是risk_research，不是explanation。市场整体状态才用market_overview。无法明确对象时clarification。entities只写用户或上下文中明确的实体名，不编造代码。
```

**user**（JSON）

```json
{"question": "用户问题", "context": "同一对话上一轮的问题、截至日与指数", "selected_window": 20}
```

返回后由代码兜底：非定义式问法却含"风险、情绪、涨停、跌停、极端"时，从 explanation 改为 risk_research（`override_explanation`）。

## 2. 规划取数（`research.py` 的 `run_research`，最多 4 轮）

- 参数：`max_tokens=1400`；`tools` 为本次问题允许的白名单工具，模型可自行决定调用顺序。
- 每轮最多共 10 次工具调用；参数逐项校验（工具名、参数键、代码或行业必须在服务端给定的枚举中，`purpose` 不超过 300 字）。

**system（第 1 条）** = 按问题加载的研究技能全文，依次拼接（见 `research-skills/`，加载规则见 `select_skills`），末尾追加：

```text
你是只读取证的市场研究Agent。按已识别的问题意图选择工具，不套用大盘报告。行业排名必须用get_sector_ranking，不能拿宽基指数或随意选几个行业代替。每个purpose说明取数用途，不输出内部思考。已取得的工具不要重复调用。接口失败明确记录。研究个股时用get_stock_history取该股行情，沪深300只作为大盘对照，不能用指数代替个股。
```

例如"最近20个交易日A股的行情结构和市场参与度如何？"会加载 `research-protocol`、`market-state`、`risk-context`、`valuation-boundary`、`event-context`，合计约 4900 字。

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

## 3. 选择观点（最多 2 次）

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
 "insights": [{"id": "index_0", "text": "沪深300在2026-09-01至2026-09-30的首末点位变化为-5.50%；……", "dimension": "行情结构", "evidence_ids": ["…"], "derivation": {"return_pct": -5.5}}],
 "tensions": [{"id": "different_windows", "text": "区间指数变化与末日市场宽度不能互相替代。……", "evidence_ids": ["…"]}]}
```

**第 1 次未通过校验时**，追加 assistant（模型上次的返回）和 user：

```text
选择未通过校验：<原因>。summary_ids为1至3条，detail_ids为1至10条；summary_ids与detail_ids只能使用insights中的id：<候选观点ID列表>；tension_id只能使用：<候选矛盾ID列表>。
```

第 2 次仍未通过时不再重试，研究状态为 `facts_only`，只展示已核验事实。通过后由代码按 ID 渲染文字（`render_selection`）：模型选错的非观点 ID 只会被删除，不会新增内容；已取得证据但模型没选的估值、风险、事件观点，由代码追加到展开末尾。

## 记录在哪里

每次研究的意图、模型选择和被剔除的 ID 写入 `work/research-runs/<run_id>.json`（`intent`、`selected_insights`、`skills_loaded`）；选择未通过校验时，模型原始返回写入 `work/validation-attempts/`。两处都在 `.gitignore` 中，不进入仓库。
