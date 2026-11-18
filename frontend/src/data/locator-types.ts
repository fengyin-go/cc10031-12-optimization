/** 定位工作台的数据契约：与后端 /api/locator-workbench 返回保持一致。 */

export type SelectorBoundary = {
  kind: string
  value: string
}

export type LocatorCacheEntry = {
  model_id: number | null
  fingerprint: string
  rule_id: number
  rule_name: string
  outcome: 'hit' | 'fallback_hit' | 'miss' | 'conflict'
  matched_selector: string
  attempts: number
  stale: boolean
  invalidated_at: string | null
  updated_at: string
}

export type InvalidRule = {
  rule_id: number
  rule_name: string
  expression: string
  fallback: string
  reason: string
  retryable: boolean
}

export type ExecutionRecord = {
  record_id: number | null
  session_id: string
  source: string
  rule_id: number
  rule_name: string
  page_model_id: number | null
  page_fingerprint: string
  previous_fingerprint: string | null
  expression: string
  fallback: string
  outcome: 'hit' | 'fallback_hit' | 'miss' | 'conflict'
  success: boolean
  status_code: number
  status_label: string
  matched_selector: string
  probes: number
  structure_changed: boolean
  cached_reused: boolean
  rebuild: boolean
  stale_published: boolean
  note: string
  recorded_at: string
}

export type RetryBoundaryItem = {
  page_model_id: number | null
  rule_id: number
  rule_name: string
  expression: string
  fallback: string
  last_fingerprint: string
  status_code: number
  status_label: string
  attempted_at: string
}

export type WindowHistoryItem = {
  rule_id: number
  rule_name: string
  attempts: number
  probes: number
  last_outcome: string | null
  last_status_code: number | null
  last_status_label: string | null
  last_fingerprint: string | null
  reusable: boolean
}

export type WorkbenchPanel = {
  model_id: number
  model_name: string
  target_site: string
  fingerprint: string
  previous_fingerprint: string | null
  structure_changed: boolean
  summary: string
  boundaries: SelectorBoundary[]
  boundary_count: number
  cache: { entries: LocatorCacheEntry[]; valid: number; stale: number }
  invalid_rules: InvalidRule[]
  retry_boundary: RetryBoundaryItem[]
  retry_candidate_count: number
  recent_executions: ExecutionRecord[]
  window_history: WindowHistoryItem[]
  status_labels: Record<string, string>
}

export type WorkbenchSnapshot = {
  dataset: string
  count: number
  panels: WorkbenchPanel[]
  ledger: { total: number; conflicts: number; stale_published: number }
  cache: { total: number; valid: number; stale: number }
  invalid_total: number
  result_total: number
}

export type WindowRecompute = {
  page_model_id: number
  page_model_name: string
  fingerprint: string
  window: number
  records_examined: number
  history: WindowHistoryItem[]
  reusable_boundaries: SelectorBoundary[]
  reusable_boundary_count: number
  stale_rules: WindowHistoryItem[]
  stale_rule_count: number
  retry_candidates: number
  scanned_whole_group: boolean
}

export type RetryResult = {
  ok: boolean
  retried: number
  recovered: number
  conflicts: number
  probes: number
  scanned_whole_group: boolean
  remaining_invalid: number
  policy: string
  results: ExecutionRecord[]
}

export type WorkItemRecord = {
  rule_id: number
  rule_name: string
  expression: string
  fallback: string
  candidate_paths: { stage: string; expression: string }[]
  page_model_id: number | null
  page_fingerprint: string
  boundaries: SelectorBoundary[]
  boundary_count: number
  status_code: number
  status_label: string
  structure_changed: boolean
  reuse_cache: boolean
  rebuild: boolean
}
