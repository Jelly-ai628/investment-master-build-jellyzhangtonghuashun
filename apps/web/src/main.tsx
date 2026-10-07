import React, { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { ArrowUp, ArrowUpRight, Check, ChevronDown, Copy, FileText, History, Info, LoaderCircle, Menu, PanelLeftClose, Plus, Search, Square, X, ChartNoAxesCombined, CalendarDays } from 'lucide-react'
import type { Conversation, Evidence, Job, Progress, Report } from './types'
import './styles.css'

const Chart = lazy(() => import('./Chart').then(m => ({ default: m.Chart })))
const errors: Record<string, string> = {
  research_timeout: '研究超时，已保留已有进度。可以稍后重新研究。',
  research_failed: '本次研究未完成，请稍后重试。',
  future_research_date: '暂不支持尚未发生的日期，请选择已完成的交易日。',
  incomplete_research_plan: '取数计划没有完整覆盖问题，本次未生成结论。',
  no_verified_research_evidence: '没有取得足以核验的证据，本次未生成正常结论。',
  rate_limited: '数据服务暂时限流，请稍后重试。',
  narrative_boundary_violation: '解释未通过边界检查，未展示该解释。',
}
async function api<T>(path: string, body?: unknown, method?: string): Promise<T> {
  const response = await fetch('/api' + path, {
    method: method || (body === undefined ? 'GET' : 'POST'),
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const value = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : '请求未完成，请稍后重试。')
  return value
}

function SourcePanel({ value, jobId, close }: { value: Evidence | null; jobId: string; close: () => void }) {
  const ref = useRef<HTMLDialogElement>(null)
  const [raw, setRaw] = useState<{ rows: Record<string, unknown>[]; offset: number; total: number; source_index: number; source_count: number } | null>(null)
  const [rawError, setRawError] = useState('')
  useEffect(() => { if (value) ref.current?.showModal(); else ref.current?.close() }, [value])
  useEffect(() => { setRaw(null); setRawError('') }, [value])
  async function loadRaw(offset = 0, source = 0) {
    if (!value) return
    try { setRaw(await api('/research/' + jobId + '/evidence/' + value.id + '/rows?offset=' + offset + '&source=' + source)); setRawError('') }
    catch (e) { setRawError((e as Error).message) }
  }
  const d = value?.data
  return <dialog className="source-panel" ref={ref} onCancel={e => { e.preventDefault(); close() }} onClick={e => { if (e.target === e.currentTarget) close() }}>
    {value && <div className="source-inner">
      <header><div><span className="minor-label">证据详情</span><h2>{d?.name || (value.kind === 'sector_ranking' ? '行业相对表现排名' : value.kind === 'events' ? '流动性相关事件报道' : '市场宽度与成交活跃度')}</h2></div><button className="icon-button" onClick={close} aria-label="关闭证据详情"><X size={20}/></button></header>
      <p className="source-intro">这里呈现回答所依据的数据、时间与计算方法。归纳不等同于原始事实。</p>
      <dl className="source-meta">
        <div><dt>来源</dt><dd>{value.provenance.source}</dd></div>
        <div><dt>行情日期</dt><dd>{value.provenance.observation_date}</dd></div>
        <div><dt>取数时间</dt><dd>{new Date(value.provenance.retrieved_at).toLocaleString('zh-CN')}</dd></div>
        <div><dt>单位</dt><dd>{value.provenance.unit}</dd></div>
        <div><dt>读取方式</dt><dd>{value.provenance.cache_used ? '已核验的近期缓存' : '本次数据调用'}</dd></div>
      </dl>
      <h3>字段与数值</h3>
      {value.kind === 'index_history' ? <>
        <dl className="source-meta"><div><dt>{d?.instrument_type === 'stock' ? '末日收盘价（元，前复权）' : '末日点位'}</dt><dd>{d?.last_close?.toFixed(2)}</dd></div><div><dt>20日均线</dt><dd>{d?.ma20?.toFixed(2) ?? '历史不足'}</dd></div><div><dt>60日均线</dt><dd>{d?.ma60?.toFixed(2) ?? '历史不足'}</dd></div><div><dt>区间变化</dt><dd>{d?.window_return_pct?.toFixed(2)}%</dd></div></dl>
        <p className="source-note">{d?.instrument_type === 'stock'
          ? '区间变化 =（末日前复权收盘价 ÷ 起点前复权收盘价 − 1）× 100%。均线为算术平均；最大回撤按区间内的历史峰值逐日计算。复权口径由扶摇确定，不含分红再投资。'
          : '区间变化 =（末日点位 ÷ 起点点位 − 1）× 100%。均线为算术平均；最大回撤按区间内的历史峰值逐日计算。指数点位口径，不含分红再投资。'}</p>
        <div className="table-scroll source-table"><table><thead><tr><th>日期</th><th>{d?.instrument_type === 'stock' ? '收盘价（前复权）' : '收盘点位'}</th></tr></thead><tbody>{d?.series?.map(row => <tr key={row.date}><td>{row.date}</td><td>{row.close.toFixed(2)}</td></tr>)}</tbody></table></div>
      </> : value.kind === 'historical_comparison' ? <>
        <p className="source-note">使用相同长度交易日区间比较已经发生的指数表现；不以相似历史外推未来。</p>
        <dl className="source-meta">{[d?.current, d?.previous].map((period, i) => <div key={i}><dt>{i ? '对照区间' : '当前区间'}</dt><dd>{period?.start_date} — {period?.end_date}<br/>{period?.window_return_pct.toFixed(2)}%</dd></div>)}</dl>
      </> : value.kind === 'sector_ranking' ? <>
        <dl className="source-meta"><div><dt>比较范围</dt><dd>{d?.valid_count}/{d?.expected_count} 个行业样本</dd></div><div><dt>区间</dt><dd>{d?.start_date} — {d?.end_date}</dd></div></dl>
        <p className="source-note">{d?.scope}</p><p className="source-note">按区间首末点位变化降序排列，近5个交易日变化为辅助观察。相对表现以沪深300同区间变化为基准。</p>
        {d?.limitations?.map(line => <p className="source-note" key={line}>{line}</p>)}
      </> : value.kind === 'valuation' ? <>
        <dl className="source-meta">{d?.items?.map(row => <div key={row.metric}><dt>{row.metric}</dt><dd>{row.raw_value} {row.unit}</dd></div>)}</dl>
        {d?.items?.map(row => <p className="source-note" key={row.metric}>{row.metric}：日期取自 {row.date_source}；{row.unit_source}</p>)}
        {!!d?.missing_metrics?.length && <p className="source-note">未通过核验：{d.missing_metrics.map(m => m.metric + '（' + m.reason + '）').join('、')}</p>}
        {d?.limitations?.map(line => <p className="source-note" key={line}>{line}</p>)}
      </> : value.kind === 'events' ? <>
        <p className="source-note">检索区间 {d?.start_date} — {d?.date}。只展示原网页标题与日期均已核对的报道。</p>
        {d?.records?.map(row => <div className="event-record" key={row.url}><strong>{row.title}</strong><p className="source-note">{row.published_at} · {row.source_scope}{row.contains_plan ? ' · 含拟实施安排' : ''}</p><p className="source-note">{row.excerpt.slice(0, 200)}{row.excerpt.length > 200 ? '…' : ''}</p><a href={row.url} target="_blank" rel="noopener noreferrer">打开原文</a></div>)}
        {d?.limitations?.map(line => <p className="source-note" key={line}>{line}</p>)}
      </> : value.kind === 'sentiment' ? <>
        <dl className="source-meta"><div><dt>涨停池</dt><dd>{d?.limit_up_count} 家</dd></div><div><dt>跌停池</dt><dd>{d?.limit_down_count} 家</dd></div></dl>
        {d?.limitations?.map(line => <p className="source-note" key={line}>{line}</p>)}
      </> : <>
        <dl className="source-meta"><div><dt>代码表样本</dt><dd>{d?.universe_count?.toLocaleString()} 家</dd></div><div><dt>有效样本</dt><dd>{d?.eligible_count?.toLocaleString()} 家</dd></div><div><dt>排除记录</dt><dd>{d?.excluded_count} 家</dd></div><div><dt>有效覆盖</dt><dd>{d?.coverage_pct?.toFixed(2)}%</dd></div></dl>
        <p className="source-note">{d?.sample_definition}</p><p className="source-note">{d?.date_link}</p>
        {d?.limitations?.map(line => <p className="source-note" key={line}>{line}</p>)}
      </>}
      <details><summary>原始字段与计算版本</summary><div className="field-list">{value.provenance.raw_fields.map(field => <code key={field}>{field}</code>)}</div><p className="source-note">{value.provenance.calculation_version}</p><code className="source-id">{value.id}</code></details>
      <div className="raw-data"><button className="raw-button" onClick={() => loadRaw()}>读取原始字段记录</button>{rawError && <p role="alert">{rawError}</p>}
        {raw && <><div className="raw-controls"><span>共 {raw.total.toLocaleString()} 条 · 当前 {raw.total ? raw.offset + 1 : 0}–{Math.min(raw.offset + raw.rows.length, raw.total)}</span>{raw.source_count > 1 && <select aria-label="原始数据来源" value={raw.source_index} onChange={e => loadRaw(0, Number(e.target.value))}>{Array.from({ length: raw.source_count }, (_, i) => <option key={i} value={i}>{value.provenance.source_labels?.[i] || '数据源 ' + (i + 1)}</option>)}</select>}</div>
          <div className="table-scroll"><table><thead><tr>{Object.keys(raw.rows[0] || {}).map(key => <th key={key}>{key}</th>)}</tr></thead><tbody>{raw.rows.map((row, i) => <tr key={i}>{Object.values(row).map((cell, n) => <td key={n}>{cell === null ? '缺失' : String(cell)}</td>)}</tr>)}</tbody></table></div>
          <div className="raw-controls"><button disabled={raw.offset === 0} onClick={() => loadRaw(Math.max(0, raw.offset - 50), raw.source_index)}>上一页</button><button disabled={raw.offset + raw.rows.length >= raw.total} onClick={() => loadRaw(raw.offset + 50, raw.source_index)}>下一页</button></div></>}
      </div>
    </div>}
  </dialog>
}

function Answer({ report, source, followup }: { report: Report; source: (e: Evidence) => void; followup: (q: string) => void }) {
  const [copied, setCopied] = useState(false)
  const focused = report.presentation === 'sector_ranking'
  const ranked = report.evidence.find(e => e.kind === 'sector_ranking')
  if (report.presentation === 'message') return <article className="answer"><div className="answer-heading"><span className="answer-symbol"><ChartNoAxesCombined size={18}/></span><strong>市场研判</strong>{report.narrative?.author === 'deepseek' && <span className="answer-time" title="这个问题不需要行情数据，回答由 DeepSeek 撰写；服务端已检查其中没有具体行情数字和操作性措辞">DeepSeek 回答 · 本轮未取数</span>}</div><div className="answer-content"><p className="answer-lead message-text">{report.narrative?.summary}</p><div className="followups">{report.followups.map(q => <button key={q} onClick={() => followup(q)}>{q}<ArrowUpRight size={15}/></button>)}</div></div></article>
  const cite = (ids: string[]) => <span className="citations">{ids.map(id => {
    const index = report.evidence.findIndex(item => item.id === id)
    return index < 0 ? null : <button key={id} title={'查看证据 ' + (index + 1)} onClick={() => source(report.evidence[index])}>{index + 1}</button>
  })}</span>
  const copy = async () => {
    const text = ['市场研究 · 数据截至 ' + report.as_of, report.narrative?.summary || report.narrative_warning, ...(report.narrative?.paragraphs || []).map(p => p.text), ...report.facts.map(f => f.text), ...(report.narrative?.hypotheses?.length ? ['待验证的假设：', ...report.narrative.hypotheses.map(h => h.text + '（如何验证：' + h.check + '）')] : []), '不确定性：', ...report.uncertainties, '来源：' + [...new Set(report.evidence.map(e => e.provenance.source))].join('、') + '；证据标识：' + report.evidence.map(e => e.id).join('、')].join('\n\n')
    try { await navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 2000) } catch { setCopied(false) }
  }
  const heading = <><div className="answer-heading"><span className="answer-symbol"><ChartNoAxesCombined size={18}/></span><strong>市场研判</strong><span className="answer-time">截至 {report.as_of}</span></div>
    {report.notices?.map(line => <div className="notice" key={line}><Info size={17}/><p>{line}</p></div>)}</>
  const chartOf = (initial?: 'indices' | 'breadth' | 'sectors') => <Suspense fallback={<div className="chart-loading">正在加载图表…</div>}><Chart evidence={focused ? report.evidence.filter(e => e.kind === 'sector_ranking') : report.evidence} onSource={source} initial={initial}/></Suspense>
  const history = report.evidence.filter(e => e.kind === 'historical_comparison').map(e => <section className="chart-block" key={e.id}><h3>{e.data.name} · 历史阶段对照</h3><div className="table-scroll"><table><thead><tr><th>时期</th><th>区间</th><th>首末变化</th><th>最大回撤</th></tr></thead><tbody>{[e.data.current, e.data.previous].map((p, i) => <tr key={i}><td>{i ? '对照' : '当前'}</td><td>{p?.start_date} — {p?.end_date}</td><td>{p?.window_return_pct.toFixed(2)}%</td><td>{p?.max_drawdown_pct.toFixed(2)}%</td></tr>)}</tbody></table></div><div className="chart-caption"><span>相同交易日长度 · 不外推未来表现</span><button onClick={() => source(e)}>查看来源</button></div></section>)
  const conditions = <div className="conditions"><h3>哪些条件会改变判断</h3>{report.transition_conditions.map((item, i) => <div key={i}><strong>{item.label}</strong><p>{item.condition}{cite([item.evidence_id])}</p></div>)}</div>
  const hypotheses = !!report.narrative?.hypotheses?.length && <div className="hypotheses"><h3>待验证的假设</h3><p className="source-note">以下是 DeepSeek 对数据背后原因的推测，属于不确定判断，尚未得到证据验证。</p>{report.narrative.hypotheses.map((item, i) => <div key={i}><span className="dimension-label">假设 · 不确定</span><p>{item.text}{cite(item.evidence_ids)}</p><p className="hypothesis-check">如何验证：{item.check}</p></div>)}</div>
  const boundary = <><p className="uncertainty-scope">{report.confidence.scope}</p>{report.uncertainties.map((line, i) => <p key={i}>{line}</p>)}<dl className="dimension-status">{report.dimensions.map(item => <div key={item.name}><dt>{item.name}</dt><dd>{item.status}</dd></div>)}</dl></>
  const footer = <><div className="answer-actions"><button onClick={copy}>{copied ? <Check size={15}/> : <Copy size={15}/>} {copied ? '已复制' : '复制研究摘要'}</button><span>来源可追溯 · 判断存在不确定性</span></div>
    <div className="followups"><span>继续研究</span>{report.followups.map(q => <button key={q} onClick={() => followup(q)}>{q}<ArrowUpRight size={15}/></button>)}</div></>
  const paragraphs = report.narrative?.paragraphs
  if (report.narrative && paragraphs?.length) {
    // The model chose where charts go; if it placed none, the main chart follows the first paragraph.
    const placed = paragraphs.some(p => p.chart !== 'none')
    return <article className="answer">
      {heading}
      <div className="answer-content">
        <div className="summary-meta"><span>{report.market_state ? <>状态：{report.market_state.label}{cite(report.market_state.evidence_ids)}</> : focused ? '近期行业相对表现' : '基于证据的初步观察'}</span><span className="author-label" title="正文、主要矛盾和待验证假设由 DeepSeek 撰写；服务端已核对每段引用的证据、数字与日期，以及研究边界">DeepSeek 撰写 · 引用与数字已核验</span><span className="confidence">置信度 {report.confidence.level}</span></div>
        <h2 className="state-title">{report.narrative.headline || report.narrative.summary}{cite(report.narrative.summary_evidence_ids || [])}</h2>
        <div className="prose">{paragraphs.map((p, i) => <React.Fragment key={i}><p>{p.text}{cite(p.evidence_ids)}</p>{p.chart !== 'none' ? chartOf(p.chart) : !placed && i === 0 ? chartOf() : null}</React.Fragment>)}</div>
        <div className="tension"><span>{focused ? '如何看排序' : '主要矛盾'}</span><p>{report.narrative.main_tension}{cite(report.narrative.tension_evidence_ids || [])}</p></div>
        {focused && ranked && <section><h3>哪些行业表现居前</h3><div className="table-scroll"><table><thead><tr><th>行业</th><th>近{report.window}日</th><th>近5日</th><th>相对沪深300</th></tr></thead><tbody>{ranked.data.ranking?.slice(0, 10).map(row => <tr key={row.code}><td>{row.rank}. {row.name}</td><td>{row.window_return_pct > 0 ? '+' : ''}{row.window_return_pct.toFixed(2)}%</td><td>{row.five_day_return_pct > 0 ? '+' : ''}{row.five_day_return_pct.toFixed(2)}%</td><td>{row.relative_return_pp > 0 ? '+' : ''}{row.relative_return_pp.toFixed(2)}个百分点</td></tr>)}</tbody></table></div></section>}
        {history}
        {hypotheses}
        {conditions}
        <details className="uncertainty evidence-details"><summary><Info size={16}/>证据与核验：{report.facts.length} 条已核验事实 · 七维状态 · 数据边界<ChevronDown size={15}/></summary><div>
          <div className="fact-list"><h3>已核验的事实</h3>{report.facts.map((fact, i) => <p key={i}>{fact.text}{cite(fact.evidence_ids)}</p>)}</div>
          {!!report.narrative.supplements?.length && <div className="interpretations"><h3>与本问题关系较远的已核验观点</h3>{report.narrative.supplements.map((item, i) => <div key={i}><span className="dimension-label">{item.dimension} · 代码补充</span><p>{item.text}{cite(item.evidence_ids)}</p></div>)}</div>}
          <h3>不确定性与数据边界</h3>{boundary}
        </div></details>
        {footer}
      </div>
    </article>
  }
  return <article className="answer">
    {heading}
    <div className="answer-content">
      {report.narrative ? <>
        <div className="summary-meta"><span>{focused ? '近期行业相对表现' : '基于证据的初步观察'}</span><span className="confidence">置信度 {report.confidence.level}</span><span className="author-label" title={report.narrative.author === 'deepseek' ? '摘要、矛盾、解读与假设由 DeepSeek 撰写；服务端已核对每条引用、数字与日期，以及研究边界' : 'DeepSeek 从代码生成的已核验观点中选择与排序'}>{report.narrative.author === 'deepseek' ? 'DeepSeek 撰写 · 引用与数字已核验' : 'DeepSeek 选择已核验观点'}</span></div>
        {report.market_state && <h2 className="state-title">{report.market_state.label}{cite(report.market_state.evidence_ids)}</h2>}
        <p className="answer-lead">{report.narrative.summary}{cite(report.narrative.summary_evidence_ids || [])}</p>
        <div className="tension"><span>{focused ? '如何看排序' : '主要矛盾'}</span><p>{report.narrative.main_tension}{cite(report.narrative.tension_evidence_ids || [])}</p></div>
      </> : <div className="notice"><Info size={17}/><p>{report.narrative_warning || '解释未通过校验，仅展示已验证事实。'}</p></div>}
      {focused && ranked ? <section><h3>哪些行业表现居前</h3><div className="table-scroll"><table><thead><tr><th>行业</th><th>近{report.window}日</th><th>近5日</th><th>相对沪深300</th></tr></thead><tbody>{ranked.data.ranking?.slice(0, 10).map(row => <tr key={row.code}><td>{row.rank}. {row.name}</td><td>{row.window_return_pct > 0 ? '+' : ''}{row.window_return_pct.toFixed(2)}%</td><td>{row.five_day_return_pct > 0 ? '+' : ''}{row.five_day_return_pct.toFixed(2)}%</td><td>{row.relative_return_pp > 0 ? '+' : ''}{row.relative_return_pp.toFixed(2)}个百分点</td></tr>)}</tbody></table></div><p className="source-note">{ranked.data.scope}有效{ranked.data.valid_count}/{ranked.data.expected_count}个序列。{cite([ranked.id])}</p></section> :
        <div className="fact-list"><h3>已核验的事实</h3>{report.facts.map((fact, i) => <p key={i}>{fact.text}{cite(fact.evidence_ids)}</p>)}</div>}
      <Suspense fallback={<div className="chart-loading">正在加载图表…</div>}><Chart evidence={focused ? report.evidence.filter(e => e.kind === 'sector_ranking') : report.evidence} onSource={source}/></Suspense>
      {report.evidence.filter(e => e.kind === 'historical_comparison').map(e => <section className="chart-block" key={e.id}><h3>{e.data.name} · 历史阶段对照</h3><div className="table-scroll"><table><thead><tr><th>时期</th><th>区间</th><th>首末变化</th><th>最大回撤</th></tr></thead><tbody>{[e.data.current, e.data.previous].map((p, i) => <tr key={i}><td>{i ? '对照' : '当前'}</td><td>{p?.start_date} — {p?.end_date}</td><td>{p?.window_return_pct.toFixed(2)}%</td><td>{p?.max_drawdown_pct.toFixed(2)}%</td></tr>)}</tbody></table></div><div className="chart-caption"><span>相同交易日长度 · 不外推未来表现</span><button onClick={() => source(e)}>查看来源</button></div></section>)}
      {report.narrative && !focused && <div className="interpretations"><h3>如何理解这些证据</h3>{report.narrative.interpretations.map((item, i) => <div key={i}><span className="dimension-label">{item.dimension} · 归纳{item.author === 'rules' ? ' · 代码补充' : ''}</span><p>{item.text}{cite(item.evidence_ids)}</p></div>)}</div>}
      {!!report.narrative?.supplements?.length && <details className="uncertainty evidence-details"><summary><Info size={16}/>与本问题关系较远的已核验观点<ChevronDown size={15}/></summary><div className="interpretations">{report.narrative.supplements.map((item, i) => <div key={i}><span className="dimension-label">{item.dimension} · 代码补充</span><p>{item.text}{cite(item.evidence_ids)}</p></div>)}</div></details>}
      {hypotheses}
      <div className="conditions"><h3>哪些条件会改变判断</h3>{report.transition_conditions.map((item, i) => <div key={i}><strong>{item.label}</strong><p>{item.condition}{cite([item.evidence_id])}</p></div>)}</div>
      <details className="uncertainty" open><summary><Info size={16}/>不确定性与数据边界<ChevronDown size={15}/></summary><div>
        {report.uncertainties.map((line, i) => <p key={i}>{line}</p>)}
        <p>{report.confidence.scope}</p>
        <dl className="dimension-status">{report.dimensions.map(item => <div key={item.name}><dt>{item.name}</dt><dd>{item.status}</dd></div>)}</dl>
      </div></details>
      <div className="answer-actions"><button onClick={copy}>{copied ? <Check size={15}/> : <Copy size={15}/>} {copied ? '已复制' : '复制研究摘要'}</button><span>来源可追溯 · 判断存在不确定性</span></div>
      <div className="followups"><span>继续研究</span>{report.followups.map(q => <button key={q} onClick={() => followup(q)}>{q}<ArrowUpRight size={15}/></button>)}</div>
    </div>
  </article>
}

function App() {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [selected, setSelected] = useState<Conversation | null>(null)
  const [jobs, setJobs] = useState<Job[]>([])
  const [question, setQuestion] = useState('')
  const [windowSize, setWindowSize] = useState(20)
  const [endDate, setEndDate] = useState('')
  const [showDate, setShowDate] = useState(false)
  const [search, setSearch] = useState('')
  const [sidebar, setSidebar] = useState(false)
  const [ready, setReady] = useState(false)
  const [loginRequired, setLoginRequired] = useState(false)
  const [accessCode, setAccessCode] = useState('')
  const [error, setError] = useState('')
  const [sending, setSending] = useState(false)
  const [events, setEvents] = useState<Progress[]>([])
  const [source, setSource] = useState<Evidence | null>(null)
  const [sourceJob, setSourceJob] = useState('')
  const [about, setAbout] = useState(false)
  const input = useRef<HTMLTextAreaElement>(null)
  const scroll = useRef<HTMLDivElement>(null)
  const nearBottom = useRef(true)
  const completedSeen = useRef(new Set<string>())
  const active = jobs.find(j => j.status === 'queued' || j.status === 'running')

  async function loadList() { setConversations(await api<Conversation[]>('/conversations')) }
  async function openConversation(id: string) {
    try {
      const value = await api<Conversation>('/conversations/' + id)
      setSelected(value); setJobs(value.jobs || []); setSidebar(false); setEvents([])
      localStorage.setItem('market_active_conversation', id)
      const last = value.jobs?.at(-1)
      if (last) { setWindowSize(last.request.window); setEndDate(last.request.end_date || '') }
      nearBottom.current = true
    } catch (e) { setError((e as Error).message) }
  }
  async function bootstrap(code = '') {
    try {
      await api('/session', { code })
      setLoginRequired(false); setReady(true)
      await loadList()
      const id = localStorage.getItem('market_active_conversation')
      if (id) await openConversation(id)
    } catch (e) { setLoginRequired(true); if (code) setError((e as Error).message) }
  }
  useEffect(() => { void bootstrap() }, [])
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault(); newConversation()
      }
    }
    document.addEventListener('keydown', listener)
    return () => document.removeEventListener('keydown', listener)
  }, [])
  useEffect(() => {
    if (!active?.id) return
    const id = active.id
    const stream = new EventSource('/api/research/' + id + '/events')
    let closed = false
    const refresh = async () => {
      try {
        const latest = await api<Job & { progress?: Progress[] }>('/research/' + id)
        if (closed) return
        if (latest.progress?.length) setEvents(old => [...old, ...latest.progress!.filter(p => !old.some(e => e.seq === p.seq))].sort((a, b) => a.seq - b.seq))
        setJobs(current => current.map(j => j.id === id ? { ...j, ...latest } : j))
        if (!['queued', 'running'].includes(latest.status)) { stream.close(); if (latest.result) setWindowSize(latest.result.window); await loadList() }
      } catch { /* EventSource reconnects; history can restore durable state. */ }
    }
    stream.onmessage = message => {
      const item: Progress = JSON.parse(message.data)
      setEvents(old => old.some(e => e.seq === item.seq) ? old : [...old, item])
      if (['completed', 'failed', 'cancelled'].includes(item.type)) void refresh()
    }
    stream.onerror = () => { void refresh() }
    const poll = setInterval(() => { void refresh() }, 3000)
    return () => { closed = true; stream.close(); clearInterval(poll) }
  }, [active?.id])
  useEffect(() => {
    const newest = jobs.at(-1)
    if (nearBottom.current && scroll.current) {
      if (newest?.result && !completedSeen.current.has(newest.id)) {
        completedSeen.current.add(newest.id)
        scroll.current.querySelectorAll('.answer').item(jobs.filter(j => j.result).length - 1)?.scrollIntoView({ block: 'start' })
      } else if (active) scroll.current.scrollTop = scroll.current.scrollHeight
    }
  }, [jobs, events])
  const draft = (text: string) => { setQuestion(text); input.current?.focus() }
  function newConversation() {
    setSelected(null); setJobs([]); setQuestion(''); setEvents([]); setSidebar(false); setError('')
    setEndDate(''); setWindowSize(20)
    localStorage.removeItem('market_active_conversation'); input.current?.focus()
  }
  async function submit() {
    if (!question.trim() || active || sending || !ready) return
    setSending(true); setError('')
    const text = question.trim()
    try {
      let current = selected
      if (!current) {
        current = await api<Conversation>('/conversations', { title: text.slice(0, 30) })
        setSelected(current); localStorage.setItem('market_active_conversation', current.id)
      }
      const request = { question: text, window: windowSize, end_date: endDate || null, client_request_id: crypto.randomUUID() }
      const created = await api<{ id: string; status: string }>('/conversations/' + current.id + '/research', request)
      setJobs(old => [...old, { ...created, request: { question: text, window: windowSize, end_date: endDate || undefined }, created: Date.now() / 1000 }])
      setQuestion(''); setEvents([]); nearBottom.current = true
      await loadList()
    } catch (e) { setError((e as Error).message) } finally { setSending(false) }
  }
  async function rename() {
    if (!selected) return
    const title = prompt('对话名称', selected.title)
    if (title?.trim()) {
      try { const updated = await api<Conversation>('/conversations/' + selected.id, { title: title.trim().slice(0, 120) }, 'PATCH'); setSelected(updated); await loadList() }
      catch (e) { setError((e as Error).message) }
    }
  }
  const lastEvent = [...events].reverse().find(e => e.label || e.purpose)
  const suggestions = [
    { icon: ChartNoAxesCombined, title: '市场概览', text: '最近20个交易日A股的行情结构和市场参与度如何？' },
    { icon: History, title: '大小盘比较', text: '比较最近20个交易日大小盘指数的表现，哪些证据一致，哪些存在分化？' },
    { icon: FileText, title: '风险与条件', text: '当前行情与市场宽度有哪些矛盾？哪些证据变化需要重新评估判断？' },
  ]
  return <div className="app-shell">
    {sidebar && <button className="sidebar-backdrop" aria-label="关闭历史栏" onClick={() => setSidebar(false)}/>}
    <aside className={'sidebar ' + (sidebar ? 'visible' : '')}>
      <div className="brand"><span className="brand-mark"><ChartNoAxesCombined size={21}/></span><span>市场研判</span><button className="icon-button mobile-only" onClick={() => setSidebar(false)} aria-label="折叠历史栏"><PanelLeftClose size={18}/></button></div>
      <button className="new-research" onClick={newConversation}><Plus size={17}/>新建研究<span>⌘ K</span></button>
      <label className="history-search"><Search size={15}/><input value={search} onChange={e => setSearch(e.target.value)} placeholder="搜索对话" aria-label="搜索历史对话"/></label>
      <div className="history-list"><div className="list-label">最近研究</div>
        {conversations.filter(c => c.title.includes(search)).map(c => <button key={c.id} className={'history-item ' + (selected?.id === c.id ? 'selected' : '')} onClick={() => openConversation(c.id)}><span>{c.title}</span><span className="history-dot"/></button>)}
        {!conversations.length && <p className="history-empty">从一个问题开始。<br/>你的研究会保存在这里。</p>}
        {conversations.length > 0 && !conversations.some(c => c.title.includes(search)) && <p className="history-empty">没有匹配的对话</p>}
      </div>
      <button className="sidebar-footer" onClick={() => setAbout(!about)}><Info size={16}/><span>数据来源与研究边界</span><ArrowUpRight size={14}/></button>
      <div className="user-label"><span className="user-avatar">研</span><div><strong>我的研究空间</strong><span>对话仅在当前会话下可见</span></div></div>
    </aside>
    <main>
      <header className="topbar"><div><button className="icon-button mobile-only" onClick={() => setSidebar(true)} aria-label="展开历史栏"><Menu size={20}/></button><span className="topbar-label">A 股研究</span>{selected && <><span className="separator">/</span><button className="conversation-title" onClick={rename} title="重命名对话">{selected.title}</button></>}</div><span className="model-label"><span/>DeepSeek</span></header>
      {about && <div className="about-panel"><button className="icon-button" onClick={() => setAbout(false)} aria-label="关闭研究边界"><X size={17}/></button><strong>先看证据，再理解市场。</strong><p>行情、宽度、行业与涨跌停来自扶摇，估值与事件来自 iFinD，均核对日期与口径。事实由代码计算；解读与待验证假设由 DeepSeek 撰写，并经服务端核对引用、数字与研究边界。缺失维度会明确说明，不作未来涨跌或操作判断。</p></div>}
      <div className="conversation-scroll" ref={scroll} onScroll={() => { const el = scroll.current; if (el) nearBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 160 }}>
        {loginRequired ? <section className="login"><span className="welcome-symbol"><ChartNoAxesCombined size={26}/></span><h1>进入研究空间</h1><p>请输入体验码，开始查看与研究市场。</p><form onSubmit={e => { e.preventDefault(); void bootstrap(accessCode) }}><input type="password" value={accessCode} onChange={e => setAccessCode(e.target.value)} aria-label="体验码" placeholder="体验码"/><button className="primary-button">进入</button></form>{error && <p role="alert">{error}</p>}</section> :
        jobs.length === 0 ? <section className="welcome"><span className="welcome-symbol"><ChartNoAxesCombined size={26}/></span><h1>从一个市场问题开始</h1><p>把行情、参与度与证据放在一起，<br className="mobile-only"/>理解市场正在发生什么。</p><div className="suggestions">{suggestions.map(({ icon: Icon, title, text }) => <button key={title} onClick={() => draft(text)}><Icon size={19}/><span>{title}</span><ArrowUpRight size={14}/></button>)}</div></section> :
        <div className="conversation-body">{jobs.map(job => <React.Fragment key={job.id}>
          <div className="user-message"><span>你</span><p>{job.request.question}</p></div>
          {job.result ? <Answer report={job.result} source={e => { setSource(e); setSourceJob(job.id) }} followup={draft}/> :
          ['queued', 'running'].includes(job.status) ? <div className="research-progress" role="status"><LoaderCircle size={19} className="spin"/><div><strong>{lastEvent?.label || lastEvent?.purpose || '正在准备研究'}</strong><span>核对数据、计算指标，并检查证据能否支持判断</span><details><summary>查看研究步骤</summary>{events.filter(e => e.label || e.purpose).map(e => <p key={e.seq}>{e.label || e.purpose}</p>)}</details></div></div> :
          <div className="notice job-notice"><Info size={18}/><div><strong>{job.status === 'cancelled' ? '研究已停止' : '本次研究未完成'}</strong><p>{errors[job.error || ''] || job.error || '未生成市场结论。你可以调整问题后重新研究。'}</p><button onClick={() => draft(job.request.question)}>编辑问题并重试<ArrowUpRight size={14}/></button></div></div>}
        </React.Fragment>)}</div>}
      </div>
      {!loginRequired && <div className={'composer-wrap ' + (!jobs.length ? 'composer-empty' : '')}>
        {error && <div className="error-bar" role="alert"><span>{error}</span><button className="icon-button" onClick={() => setError('')} aria-label="关闭错误提示"><X size={15}/></button></div>}
        <div className="composer">
          <textarea ref={input} aria-label="研究问题" value={question} onChange={e => setQuestion(e.target.value)} placeholder="问一个市场问题，例如：指数回落时，市场参与度有何变化？" rows={2} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void submit() } }}/>
          <div className="composer-toolbar"><div className="scope-controls">
            <label><span className="scope-dot"/>A 股</label><span className="scope-divider"/>
            <select aria-label="研究窗口" value={windowSize} onChange={e => setWindowSize(Number(e.target.value))}>{[5, 10, 20, 60].map(n => <option value={n} key={n}>近 {n} 个交易日</option>)}</select>
            <button onClick={() => setShowDate(!showDate)} title="选择截至日期" aria-label="选择截至日期"><CalendarDays size={15}/>{endDate || '最新交易日'}</button>
          </div>
          {active ? <button className="send-button stop" onClick={() => api('/research/' + active.id + '/cancel', {}).catch(e => setError(e.message))} aria-label="停止研究"><Square size={15} fill="currentColor"/></button> :
          <button className="send-button" onClick={submit} disabled={!question.trim() || sending || !ready} aria-label="发送研究问题">{sending ? <LoaderCircle size={19} className="spin"/> : <ArrowUp size={21}/>}</button>}
          </div>
          {showDate && <label className="date-selector">截至日期 <input type="date" value={endDate} onChange={e => setEndDate(e.target.value)}/><button onClick={() => { setEndDate(''); setShowDate(false) }}>恢复最新</button></label>}
        </div>
        <div className="composer-note"><span>研究观点存在不确定性，请核对证据与数据时点。</span><span className="keyboard-hint">Enter 发送 · Shift + Enter 换行</span></div>
      </div>}
    </main>
    <SourcePanel value={source} jobId={sourceJob} close={() => setSource(null)}/>
  </div>
}

createRoot(document.getElementById('root')!).render(<App/>)
