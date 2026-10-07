# AI 驱动的股票市场大势研判

## 项目目标与需求依据

本项目的目标是完整完成用户提供的 [《AI 驱动的股票市场大势研判》](01_AI驱动的股票市场大势研判.md) 中的任务和交付要求。该文件是项目需求基准；本 README 用于落实范围和验收标准，不替代或放宽原始要求。后续变更以用户明确指示为准，并同步更新项目说明。

面向 **A 股投资研究用户**，设计并实现一个 **AI Native 市场大势研判 Web 产品**。基于行情结构、市场宽度、风格轮动、估值、流动性、情绪和重要事件等证据，帮助用户理解：

- 当前市场处于什么状态。
- 当前市场的主要矛盾是什么。
- 哪些条件出现时，需要改变当前判断。

产品不得直接预测涨跌或给出仓位建议。项目完成以可访问、可实际操作的产品及配套交付物为准，仅有方案、PRD、PPT、Figma 或截图不算完成。

## 当前状态（2026-10-07）

- **已实现并真实运行**：Web 产品（React 前端 + FastAPI 后端，同源服务），研究主链路使用 DeepSeek、扶摇和 iFinD 的真实调用。
- **产品 URL**：<https://obtained-stays-substance-kerry.trycloudflare.com>（需体验码，向作者索取）。部署方式为作者本机运行 + Cloudflare 临时隧道，可用性限制见下文"公开访问"。
- **源代码仓库**：<https://github.com/Jelly-ai628/investment-master-build-jellyzhangtonghuashun>
- **尚未完成**：演示视频。
- 验证证据见 [测试说明](deliverables/05_测试说明.md) 与 [AI 使用与验证记录](deliverables/06_AI使用与验证记录.md)。

## 启动方式

依赖：Python ≥ 3.11 与 [uv](https://docs.astral.sh/uv/)、Node.js 22。

```bash
uv sync --locked
cp .env.example .env.local        # 本地填写密钥，不提交
cd apps/web && npm ci && npm run build && cd ../..
make serve                        # http://127.0.0.1:8000
```

- 前端开发：另开终端 `cd apps/web && npm run dev`，Vite 将 `/api` 代理到 8000 端口。
- 容器：`docker build -t market-research .`。镜像默认 `MARKET_PUBLIC=1`，必须通过环境变量传入 `MARKET_ACCESS_CODE`（至少 12 位），并为 `/app/work` 挂载持久卷。
- 离线测试：`uv run pytest -q`。不联网、不需要密钥。
- 真实验收：`uv run python scripts/acceptance_runs.py`，会实际调用三家服务，结果写入 `work/acceptance-*.json`。
- 单次研究：`uv run python scripts/research_once.py "问题"`；接入探针：`uv run python scripts/check_connections.py --live --provider deepseek|fuyao|ifind`。

### 公开访问（当前部署方式）

当前产品 URL 由作者的电脑提供服务，没有使用云服务器：

```bash
# 终端 A：公开模式启动（体验码只放在进程环境变量里，至少 12 位）
MARKET_PUBLIC=1 MARKET_ACCESS_CODE='…' caffeinate -i make serve
# 终端 B：Cloudflare 临时隧道，无需账号，输出 https://<随机名>.trycloudflare.com
cloudflared tunnel --url http://127.0.0.1:8000
```

- 应用仍只监听 `127.0.0.1`，外部只能经隧道以 HTTPS 访问；公开模式下会话 Cookie 为 `Secure`，所有研究接口需先输入体验码。
- 选择本机的原因：免费，并且扶摇、iFinD、DeepSeek 已在这台机器上验证可调用；海外云主机对这些服务的可达性未验证。
- 代价：电脑关机、休眠、断网或隧道进程重启时网址不可用；临时隧道每次重启都会换网址，且 Cloudflare 不保证其可用性。需要固定网址时，可改用自有域名 + Cloudflare 命名隧道，或使用仓库中的 `Dockerfile` 部署到云主机（需挂载 `/app/work` 持久卷、单实例运行）。

## 环境变量

| 变量 | 读取位置 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL` | 环境变量优先，其次 `.env.local` | 模型供应商。当前验证型号为 `deepseek-flash` |
| `FUYAO_API_KEY` | 同上 | 扶摇金融数据 API |
| `IFIND_MCP_URL` / `IFIND_MCP_TRANSPORT` / `IFIND_MCP_AUTH_HEADER` / `IFIND_MCP_AUTH_VALUE` | 同上 | iFinD 指数/板块 MCP，用于估值。仅允许 HTTPS 的 `*.51ifind.com` |
| `IFIND_NEWS_URL` | 同上 | iFinD 新闻 MCP，用于事件证据。仅允许 `api-mcp.51ifind.com` |
| `MARKET_PUBLIC` / `MARKET_ACCESS_CODE` | **仅进程环境变量** | 默认只接受本机请求；`MARKET_PUBLIC=1` 时必须设置体验码 |

`.env.local`、`work/`（原始数据、研究存档、SQLite）和原始工具包均在 `.gitignore` 中，不进入交付源代码。

## 目标用户与产品选择

- **用户**：首版面向用户本人开展 A 股市场研究，以及评审通过网页体验核心任务。未使用真实用户画像或持仓。
- **形态**：左侧对话历史 + 中央 AI 对话。回答在同一条时间线呈现结论、证据、图表和不确定性；来源详情通过可关闭的抽屉按需展开（见 [PRODUCT.md](PRODUCT.md)、[DESIGN.md](DESIGN.md)）。
- **一次回答包含**：市场状态标签与置信度、摘要、主要矛盾、已核验事实（带证据角标）、ECharts 图表与数据表切换、"如何理解这些证据"（标注维度与"归纳"）、判断改变条件、不确定性与七维状态、继续研究入口。
- **研究控制**：研究窗口（5/10/20/60 个交易日）、截至日期；同一对话中追问时，上一轮的问题、截至日与指数会作为上下文传入；问题含"同一 / 那 / 继续 / 相比 / 上个月"等指代且未另选日期时，沿用上一轮的截至日。
- **证据追溯**：每个角标打开证据抽屉，显示来源、行情日期、取数时间、单位、计算口径、原始字段名，并可分页读取保存在服务端的原始字段记录。

## 市场状态框架与 AI 的角色

七维框架：行情结构、市场宽度、风格轮动、估值、流动性、情绪、重要事件。每一维都会给出状态："已取得"、"已调用，未通过核验（原因）"或"本轮未调用"。缺失维度不按中性处理。

| 环节 | 由谁完成 | 约束 |
|---|---|---|
| 合规拦截 | 代码（规则） | 涨跌预测、仓位、买卖建议类问题直接返回边界说明，不取数 |
| 意图识别 | DeepSeek 强制工具调用 + 代码兜底 | 用户点名的证券或无法匹配的对象，不会被默认指数替代 |
| 取数规划与工具调用 | DeepSeek | 只能调用白名单工具，参数逐项校验；服务端计算最低必需工具集，未覆盖则不出结论 |
| 指标计算、事实、状态标签、切换条件 | 代码（确定性） | 每条事实绑定证据 ID 与原始字段 |
| 候选观点与主要矛盾 | 代码生成 | 每条观点带推导数值和证据引用 |
| 摘要与展开顺序 | DeepSeek 只按 ID 选择 | 不能新增或改写文字；选择不合格会重试一次，仍失败则只展示事实（`facts_only`） |

### 研究技能（`research-skills/`）

研究技能是运行时按问题加载给 DeepSeek 的系统提示，用于取数规划和观点选择（`research.py` 中的 `select_skills`）。每份技能写明适用问题、取数计划、怎么读证据、选择与排序、边界。

| 技能 | 何时加载 | 借鉴自 `investment-masters-toolkit/` 的方法 |
|---|---|---|
| `research-protocol` | 每次研究 | `SKILL.md` 的标准流程（识别意图 → 精确确认代码、禁止凭记忆 → 确认交易日 → 计算 → 结论前置）、"复杂 Query 多维组合"路由、交互原则（不编造数据、一句话结论、定量与定性结合、最后总结待验证假设）；资金面"量级放进参照系""方向、连续性、加速度" |
| `market-state` | 非个股问题 | `market-review` 的复盘顺序；`capital-flow-report` 的"量—质—势—位"，改用于成交额；`support-resistance` 的区间高低点与距离 |
| `comparison` | 比较类问题 | `comparison` 的同口径多维对比；CANSLIM、PEG 只作为"需要哪些证据"的边界 |
| `industry-context` | 行业问题 | `industry-chain`、`event-penetration` 的传导路径（价格、需求、成本）与替代解释 |
| `history-context` | 历史对比 | `indicator-backtest` 的样本充足度与回测前提 |
| `risk-context` | 风险与整体问题 | `capital-flow-report` 的量级与持续性；`max-pessimism`、`safety-margin` 的反证要求 |
| `valuation-boundary` | 估值与个股问题 | `valuation` 的估值研究步骤（实体与时点 → 历史分位 → 相对估值 → 驱动）；`low-pe-high-div` 的价值陷阱提示 |
| `event-context` | 事件与整体问题 | `event-price-in`、`info-analysis` 的事件确认与"是否已被定价"框架 |
| `security-context` | 个股问题 | `SKILL.md` 的代码精确确认；`comparison`、`basic` 的完整个股诊断维度（用于标明未接入的部分） |

其中两项方法由代码确定性实现，作为候选观点供 AI 选择：区间收盘高低点位置（`range_*`）和最近 5 日成交节奏（`turnover_trend`），见 `insights.py`。

没有采用的内容：工具包的投顾人设、买入/持有/卖出结论、时机评分、目标价、止损位、支撑压力位建议、"帮你分析概率"、选股筛选名单，以及利好/利空条数计数。

## 数据来源与口径

| 维度 | 来源 | 口径与限制 |
|---|---|---|
| 交易日历、指数/行业日线 | 扶摇 `/api/a-share/calendar/trading-days`、`/api/a-share-index/prices/historical` | 只使用已完成交易日；区间变化按首末点位，指数点位不含分红再投资 |
| 市场宽度、成交额 | 扶摇全市场快照分页 + 最近 10 交易日日 K 导出 | 快照与带日期日 K 逐只对齐；缺失、冲突、无成交记录排除并计数，不推定停牌；**只支持最新完整交易日** |
| 行业排名 | 扶摇同花顺行业目录 881 系列 | 不混入 884 细分系列或概念主题；排名只描述已发生表现 |
| 涨跌停（情绪） | 扶摇涨停池/跌停池 | 供应商分类，逐条与同日收盘价核对；只支持最新完整交易日 |
| 估值 | iFinD `index_data` | 沪深300等宽基指数的 PE(TTM)/PB；要求响应中的交易日期参数等于研究截至日，否则判为未通过；**无历史分位** |
| 重要事件 | iFinD 新闻检索 + 报道源网页 | 仅检索央行公开市场操作相关报道，标题与日期需和原网页一致；媒体报道不等于原始公告 |

每次研究的原始响应保存在 `work/raw/`（权限 0600），证据的 provenance 中记录 endpoint、请求参数、request_id、取数时间、观察日期、原始字段名、计算版本与单位；对外接口返回前会去掉服务器文件路径。

## 已知边界

- 市场宽度和涨跌停只能取最新完整交易日；选择历史截至日时，这两维会明确显示为未通过核验。
- 估值只有截至日 PE/PB 水平，没有历史分位，不做高估或低估判断。
- 事件证据只覆盖流动性相关报道，不代表全部政策与重要事件，不推断因果。
- 流动性只有成交额；融资余额、利率等杠杆和资金成本数据未接入。
- 单只 A 股研究只覆盖该股前复权行情、与沪深300的同区间对照和最新 PE/PB 快照；行业归属和公司公告尚未接入。基金不支持。任何对象都不输出涨跌预测、收益承诺、买卖或仓位建议，"可以买入吗""要不要卖"一类问题直接返回边界说明。
- 置信度描述证据对当前局部观察的支持程度，不是涨跌概率。
- 状态标签和切换条件规则（`state-review-v1`）是描述性规则，未在历史样本上做系统回测验证。
- iFinD 新闻检索和原文核对结果有波动：同一天的多次运行中，事件证据有时取得、有时未通过核验，界面会如实显示。
- 服务端一次只运行一个研究任务，每个会话每小时最多 20 次；数据保存在单机 SQLite 中。

## 最终交付与验收清单

勾选项均有实际运行证据；未勾选项为未完成或未验证。

- [x] 可访问、可实际操作的 Web 产品 URL：<https://obtained-stays-substance-kerry.trycloudflare.com>。2026-10-07 作者通过该网址输入体验码并完成研究，服务端同一时段记录 3 次研究完成（见测试说明第 5 节）。可用性依赖作者电脑在线，见"公开访问"。
- [x] 源代码仓库：<https://github.com/Jelly-ai628/investment-master-build-jellyzhangtonghuashun>
- [x] README 说明目标用户、产品与设计选择、AI 的角色、数据来源、启动方式、环境变量、已知边界及未做事项（本文件）。
- [x] 主链路实际运行：市场状态框架、取数计划、工具调用、事实与判断区分（见测试说明中的真实运行记录）。
- [x] 产品结果包含结论、证据、时点、置信度和状态切换条件，并支持继续研究风格、行业、个股、风险变量和历史阶段（真实验收：技能 v1 19/19；技能 v2 修正后复跑通过，见测试说明）。
- [x] 关键结论可追溯到原始字段或原文，包含来源、时点、单位及统计口径。
- [x] AI 使用与验证记录：[06_AI使用与验证记录](deliverables/06_AI使用与验证记录.md)。
- [x] 测试说明：[05_测试说明](deliverables/05_测试说明.md)，覆盖主链路、数据缺失、接口失败、极端场景及合规边界，并记录实际结果。
- [ ] 可选：60–180 秒演示视频。**未制作**。

## 未做事项

- 常驻的云端部署与固定网址（当前为本机 + 临时隧道）。
- 估值历史分位、融资余额与利率、宏观数据、全量政策与公告事件。
- 历史日期的市场宽度与涨跌停（需要按日期回溯的全市场数据）。
- 浏览器端自动化测试；目前只有构建、类型检查和后端测试，界面行为靠人工检查。
- 演示视频。

## 文档索引

- [产品与技术实施规划](deliverables/01_产品与技术实施规划.md)、[工具包与数据源核查](deliverables/02_工具包与数据源核查.md)：规划与核查阶段记录。
- [接入准备与验证记录](deliverables/03_接入准备与验证记录.md)、[iFinD 数据能力与质量验证](deliverables/04_iFinD数据能力与质量验证.md)：接入阶段的真实验证记录（保留当时的测试数量与结论）。
- [测试说明](deliverables/05_测试说明.md)、[AI 使用与验证记录](deliverables/06_AI使用与验证记录.md)。
- [DeepSeek 提示词说明](docs/deepseek-prompts.md)：三类模型请求的 system/user 提示、工具与校验规则。
- [图表合同](docs/chart-contract.md)、[ECharts skill](echart/SKILL.md)。

`investment-masters-toolkit/` 与其压缩包只是辅助参考材料，不属于交付范围；其中包含硬编码凭据等问题（见核查文档），不随源代码发布。
