export type SectorRow = { rank: number; code: string; name: string; window_return_pct: number; five_day_return_pct: number; relative_return_pp: number }
export type Evidence = {
  id: string
  kind: 'index_history' | 'market_breadth' | 'historical_comparison' | 'sentiment' | 'sector_ranking' | 'valuation' | 'events'
  data: {
    name?: string; code?: string; date?: string; start_date?: string; end_date?: string
    instrument_type?: 'index' | 'industry' | 'stock'; snapshot?: boolean
    window_return_pct?: number; max_drawdown_pct?: number; last_close?: number; ma20?: number; ma60?: number
    series?: { date: string; close: number; normalized: number }[]
    advancers?: number; decliners?: number; unchanged?: number; eligible_count?: number
    universe_count?: number; excluded_count?: number; coverage_pct?: number; advancing_ratio_pct?: number
    quality?: string; sample_definition?: string; date_link?: string; limitations?: string[]
    excluded_reasons?: Record<string, number>
    turnover_history?: { date: string; turnover_cny: number; valid_count: number }[]
    limit_up_count?: number; limit_down_count?: number; return_difference_pp?: number
    current?: { start_date: string; end_date: string; window_return_pct: number; max_drawdown_pct: number }
    previous?: { start_date: string; end_date: string; window_return_pct: number; max_drawdown_pct: number }
    ranking?: SectorRow[]; expected_count?: number; valid_count?: number; window?: number; scope?: string
    failures?: { code: string; name: string; reason: string }[]
    benchmark?: { name: string; return_pct: number }
    items?: { metric: string; value: number; raw_value: string; unit: string; date: string; date_source: string; unit_source: string }[]
    missing_metrics?: { metric: string; reason: string }[]
    records?: { title: string; excerpt: string; published_at: string; url: string; contains_plan: boolean; source_scope: string }[]
  }
  provenance: {
    source: string; endpoint: string; observation_date: string; retrieved_at: string
    raw_fields: string[]; calculation_version: string; unit: string; request_id?: string; cache_used?: boolean
    source_labels?: string[]
  }
}
export type Report = {
  run_id: string; as_of: string; window: number; status: string
  presentation?: 'research' | 'sector_ranking' | 'message'
  narrative: {
    author?: 'deepseek' | 'selection' | 'rules'
    summary: string; summary_evidence_ids?: string[]; main_tension: string; tension_evidence_ids?: string[]
    interpretations: { text: string; evidence_ids: string[]; dimension: string; author?: 'deepseek' | 'rules' }[]
    hypotheses?: { text: string; check: string; evidence_ids: string[] }[]
    headline?: string
    paragraphs?: { text: string; evidence_ids: string[]; chart: 'none' | 'indices' | 'breadth' | 'sectors' }[]
    supplements?: { text: string; evidence_ids: string[]; dimension: string }[]
  } | null
  narrative_warning?: string
  notices?: string[]
  market_state?: { label: string; evidence_ids: string[]; scope: string }
  facts: { text: string; evidence_ids: string[] }[]
  confidence: { level: string; scope: string; reasons: string[] }
  dimensions: { name: string; status: string }[]
  uncertainties: string[]
  transition_conditions: { label: string; condition: string; baseline: number; unit: string; evidence_id: string }[]
  followups: string[]
  evidence: Evidence[]
}
export type Job = {
  id: string; status: string; error?: string
  request: { question: string; window: number; end_date?: string }
  result?: Report; created: number
}
export type Conversation = { id: string; title: string; updated?: number; jobs?: Job[] }
export type Progress = { seq: number; type: string; label?: string; purpose?: string; tool?: string; as_of?: string; status?: string }
