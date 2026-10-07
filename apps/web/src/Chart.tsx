import { useEffect, useMemo, useRef, useState } from 'react'
import * as echarts from 'echarts/core'
import { LineChart, BarChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent, DataZoomComponent, AriaComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { ChartNoAxesCombined, Table2 } from 'lucide-react'
import type { Evidence } from './types'

echarts.use([LineChart, BarChart, GridComponent, LegendComponent, TooltipComponent, DataZoomComponent, AriaComponent, CanvasRenderer])
const palette = ['#547564', '#6a9bcc', '#d97757', '#8b7cb6']
export function Chart({ evidence, onSource }: { evidence: Evidence[]; onSource: (e: Evidence) => void }) {
  const container = useRef<HTMLDivElement>(null)
  const [table, setTable] = useState(false)
  const indices = evidence.filter(e => e.kind === 'index_history')
  const hasStock = indices.some(e => e.data.instrument_type === 'stock')
  const breadth = evidence.find(e => e.kind === 'market_breadth' && e.data.quality !== 'insufficient_coverage')
  const ranking = evidence.find(e => e.kind === 'sector_ranking')
  const leaders = ranking?.data.ranking?.slice(0, 10) || []
  const [view, setView] = useState<'indices' | 'breadth' | 'sectors'>(ranking ? 'sectors' : indices.length ? 'indices' : 'breadth')
  const labels = view === 'sectors' ? leaders.map(row => row.name) : view === 'indices' ? indices[0]?.data.series?.map(x => x.date) || [] : ['上涨', '下跌', '平盘']
  const option = useMemo(() => {
    if (view === 'sectors') return {
      aria: { enabled: true },
      grid: { left: '3%', right: '14%', top: 20, bottom: 25, containLabel: true },
      tooltip: { trigger: 'axis', renderMode: 'richText' },
      xAxis: { type: 'value', name: '%', axisLabel: { color: '#666' }, splitLine: { lineStyle: { color: '#edf0eb' } } },
      yAxis: { type: 'category', inverse: true, data: labels, axisLabel: { color: '#666' }, axisTick: { show: false }, axisLine: { lineStyle: { color: '#c5ccc6' } } },
      series: [{ type: 'bar', barMaxWidth: 16, label: { show: true, position: 'right', color: '#53634b' },
        data: leaders.map(row => ({ value: Number(row.window_return_pct.toFixed(2)), itemStyle: { color: row.window_return_pct >= 0 ? '#b96d5d' : '#648a71', borderRadius: 3 } })) }],
      animation: !matchMedia('(prefers-reduced-motion: reduce)').matches, animationDuration: 250,
    }
    if (view === 'indices') return {
      aria: { enabled: true },
      grid: { left: '3%', right: '4%', top: 55, bottom: labels.length > 20 ? 65 : 30, containLabel: true },
      legend: { top: 4, icon: 'roundRect', itemWidth: 12, itemHeight: 3, textStyle: { color: '#535e58', fontSize: 12 } },
      tooltip: { trigger: 'axis', renderMode: 'richText' },
      xAxis: { type: 'category', data: labels.map(s => s.slice(5)), boundaryGap: false, axisLabel: { color: '#666' }, axisLine: { lineStyle: { color: '#c5ccc6' } }, axisTick: { show: false } },
      yAxis: { type: 'value', scale: true, name: '基期 = 100', nameTextStyle: { color: '#666' }, axisLabel: { color: '#666' }, splitLine: { lineStyle: { color: '#edf0eb' } } },
      dataZoom: labels.length > 20 ? [{ type: 'slider', height: 16, bottom: 12, borderColor: 'transparent', fillerColor: '#dfe8df', handleStyle: { color: '#638071' } }] : [],
      series: indices.map((e, i) => ({ name: e.data.name, type: 'line', data: e.data.series?.map(x => Number(x.normalized.toFixed(2))), symbol: 'none', connectNulls: false, lineStyle: { width: 2.3 }, itemStyle: { color: palette[i % palette.length] } })),
      animation: !matchMedia('(prefers-reduced-motion: reduce)').matches,
      animationDuration: 250,
    }
    return {
      aria: { enabled: true },
      grid: { left: '3%', right: '4%', top: 30, bottom: 20, containLabel: true },
      tooltip: { trigger: 'axis', renderMode: 'richText' },
      xAxis: { type: 'category', data: labels, axisLabel: { color: '#666' }, axisTick: { show: false }, axisLine: { lineStyle: { color: '#c5ccc6' } } },
      yAxis: { type: 'value', name: '家', axisLabel: { color: '#666' }, splitLine: { lineStyle: { color: '#edf0eb' } } },
      series: [{ type: 'bar', barWidth: '32%', itemStyle: { borderRadius: [3, 3, 0, 0] }, label: { show: true, position: 'top', color: '#535e58' }, data: [
        { value: breadth?.data.advancers, itemStyle: { color: '#c66b60' } },
        { value: breadth?.data.decliners, itemStyle: { color: '#648a71' } },
        { value: breadth?.data.unchanged, itemStyle: { color: '#b0aea5' } },
      ] }],
      animation: !matchMedia('(prefers-reduced-motion: reduce)').matches,
      animationDuration: 250,
    }
  }, [view, evidence])
  useEffect(() => {
    if (table || !container.current) return
    const chart = echarts.init(container.current)
    chart.setOption(option)
    const observer = new ResizeObserver(() => chart.resize())
    observer.observe(container.current)
    return () => { observer.disconnect(); chart.dispose() }
  }, [option, table])
  const source = view === 'sectors' ? ranking : view === 'indices' ? indices[0] : breadth
  if (!indices.length && !breadth && !ranking) return null
  const caption = view === 'sectors' ? ranking?.data.start_date + ' — ' + ranking?.data.end_date + ' · 累计变化排名 · 有效 ' + ranking?.data.valid_count + '/' + ranking?.data.expected_count + ' 个样本' : view === 'indices'
    ? source?.data.start_date + ' — ' + source?.data.end_date + (hasStock ? ' · 个股为前复权收盘价、指数为点位，首日 = 100' : ' · 指数点位，不含分红再投资')
    : source?.data.date + ' · 有效样本 ' + source?.data.eligible_count?.toLocaleString() + ' 家 · 覆盖 ' + source?.data.coverage_pct?.toFixed(2) + '%'
  return <section className="chart-block" aria-label="研究图表">
    <div className="chart-toolbar">
      <div className="chart-tabs">
        {ranking && <button className={view === 'sectors' ? 'selected' : ''} onClick={() => setView('sectors')}>行业累计表现 · 前10</button>}
        {indices.length > 0 && <button className={view === 'indices' ? 'selected' : ''} onClick={() => setView('indices')}>{hasStock ? '个股与大盘相对表现' : '指数相对表现'}</button>}
        {breadth && <button className={view === 'breadth' ? 'selected' : ''} onClick={() => setView('breadth')}>末日市场宽度</button>}
      </div>
      <button className="icon-button" aria-label={table ? '查看图表' : '查看数据表'} title={table ? '查看图表' : '查看数据表'} onClick={() => setTable(!table)}>{table ? <ChartNoAxesCombined size={17}/> : <Table2 size={17}/>}</button>
    </div>
    {table ? <div className="table-scroll"><table><thead><tr><th>{view === 'indices' ? '日期' : '类别'}</th>{view === 'indices' ? indices.map(e => <th key={e.id}>{e.data.name}（基期100）</th>) : <th>{view === 'sectors' ? '区间变化（%）' : '家数'}</th>}</tr></thead>
      <tbody>{view === 'sectors' ? leaders.map(row => <tr key={row.code}><td>{row.name}</td><td>{row.window_return_pct.toFixed(2)}%</td></tr>) : view === 'indices' ? labels.map((date, i) => <tr key={date}><td>{date}</td>{indices.map(e => <td key={e.id}>{e.data.series?.[i]?.normalized.toFixed(2) ?? '缺失'}</td>)}</tr>) : labels.map((label, i) => <tr key={label}><td>{label}</td><td>{[breadth?.data.advancers, breadth?.data.decliners, breadth?.data.unchanged][i]}</td></tr>)}</tbody></table></div> :
      <div ref={container} className="chart-canvas" style={view === 'sectors' ? { height: 350 } : undefined} role="img" aria-label={view === 'sectors' ? '行业样本累计表现前十名；数值是过去区间变化，不是未来预测' : view === 'indices' ? '同一基期归一化的指数走势，详细值可切换数据表查看' : '核验样本上涨、下跌和平盘家数，详细值可切换数据表查看'}/>}
    <div className="chart-caption"><span>{caption}</span>{source && <button onClick={() => onSource(source)}>扶摇 · 查看来源</button>}</div>
  </section>
}
