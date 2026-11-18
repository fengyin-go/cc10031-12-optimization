<template>
  <section class="page locator-workbench" data-testid="locator-workbench">
    <header class="page-head">
      <div>
        <h2>定位工作台</h2>
        <p class="page-desc">
          按页面模型查看缓存指纹、失效策略与最近执行记录；结构未变只重试候选路径，
          结构变化后批量重新解析。
        </p>
      </div>
      <div class="page-actions">
        <label class="policy-picker">
          <span>冲突处理</span>
          <select v-model="policy" data-testid="policy">
            <option value="force_rebuild">强制重建全部定位</option>
            <option value="probe_then_decide">先探测旧回退再决定</option>
          </select>
        </label>
        <button class="btn" data-testid="refresh" type="button" @click="reload">刷新</button>
        <button
          class="btn primary"
          data-testid="retry-invalid"
          type="button"
          :disabled="retrying || !totalInvalid"
          @click="retryInvalid"
        >
          {{ retrying ? '重试中…' : `重试仍失效（${totalInvalid}）` }}
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">页面模型</span>
        <strong class="stat-value">{{ snapshot?.count ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">缓存有效 / 失效</span>
        <strong class="stat-value">{{ snapshot?.cache.valid ?? 0 }} / {{ snapshot?.cache.stale ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">失效策略</span>
        <strong class="stat-value">{{ totalInvalid }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">执行记录</span>
        <strong class="stat-value">{{ snapshot?.ledger.total ?? 0 }}</strong>
      </article>
    </div>

    <p v-if="retryMessage" class="retry-banner" data-testid="retry-result">{{ retryMessage }}</p>
    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <div v-if="!panels.length" class="empty-state panel-empty">
      暂无页面模型，先在「页面建模」建立目标站点结构。
    </div>

    <article
      v-for="panel in panels"
      :key="panel.model_id"
      class="model-panel"
      :data-testid="`model-panel-${panel.model_id}`"
    >
      <header class="panel-head">
        <div>
          <h3>
            {{ panel.model_name }}
            <span class="fingerprint" data-testid="fingerprint">指纹 {{ panel.fingerprint }}</span>
            <span v-if="panel.structure_changed" class="tag warn">结构已变化</span>
          </h3>
          <p class="panel-sub">{{ panel.summary }}</p>
        </div>
        <div class="panel-actions">
          <span class="panel-meta">边界 {{ panel.boundary_count }}</span>
          <span class="panel-meta">缓存 {{ panel.cache.valid }} 有效 / {{ panel.cache.stale }} 失效</span>
          <button class="btn" type="button" @click="recompute(panel)">重算窗口</button>
        </div>
      </header>

      <p v-if="panel.previous_fingerprint" class="fingerprint-change">
        上次指纹 {{ panel.previous_fingerprint }} → 当前 {{ panel.fingerprint }}，
        旧命中不再发布。
      </p>

      <div class="panel-grid">
        <section class="panel-block">
          <h4>失效策略</h4>
          <ul v-if="panel.invalid_rules.length" class="rule-list">
            <li v-for="rule in panel.invalid_rules" :key="rule.rule_id">
              <span class="rule-name">{{ rule.rule_name }}</span>
              <code>{{ rule.expression || '—' }}</code>
              <span class="tag" :class="rule.retryable ? 'warn' : ''">
                {{ rule.retryable ? '可重试' : rule.reason }}
              </span>
            </li>
          </ul>
          <p v-else class="muted">暂无失效策略</p>
        </section>

        <section class="panel-block">
          <h4>可复用选择器边界</h4>
          <p v-if="panel.boundaries.length" class="boundary-list">
            <code v-for="boundary in panel.boundaries.slice(0, 12)" :key="`${boundary.kind}-${boundary.value}`">
              {{ boundaryLabel(boundary) }}
            </code>
          </p>
          <p v-else class="muted">页面结构未定义可复用边界</p>
        </section>

        <section class="panel-block">
          <h4>可重试边界</h4>
          <ul v-if="panel.retry_boundary.length" class="rule-list">
            <li v-for="item in panel.retry_boundary" :key="item.rule_id">
              <span class="rule-name">{{ item.rule_name }}</span>
              <code>{{ item.expression || '—' }}</code>
              <span class="tag">{{ item.status_label }}</span>
            </li>
          </ul>
          <p v-else class="muted">无命中时这里会显示可重试边界</p>
        </section>
      </div>

      <section class="panel-block">
        <h4>最近执行记录</h4>
        <table class="data-table compact">
          <thead>
            <tr>
              <th>规则</th><th>结果</th><th>处理终态</th><th>探测次数</th>
              <th>结构变化</th><th>缓存复用</th><th>旧命中发布</th><th>说明</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="record in panel.recent_executions" :key="record.record_id ?? record.recorded_at">
              <td>{{ record.rule_name }}</td>
              <td>{{ outcomeLabel(record.outcome) }}</td>
              <td>{{ record.status_code }} {{ record.status_label }}</td>
              <td>{{ record.probes }}</td>
              <td>{{ record.structure_changed ? '是' : '否' }}</td>
              <td>{{ record.cached_reused ? '是' : '否' }}</td>
              <td>{{ record.stale_published ? '是' : '否' }}</td>
              <td class="note-cell">{{ record.note }}</td>
            </tr>
            <tr v-if="!panel.recent_executions.length">
              <td colspan="8" class="empty-state">尚无执行记录，重试后这里会出现候选路径结果</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section v-if="windows[panel.model_id]" class="panel-block">
        <h4>窗口重算结果（{{ windows[panel.model_id].window }} 条）</h4>
        <p class="muted">
          检查 {{ windows[panel.model_id].records_examined }} 条历史，可复用边界
          {{ windows[panel.model_id].reusable_boundary_count }} 个，待重试规则
          {{ windows[panel.model_id].stale_rule_count }} 个；未重复扫描整组选择器。
        </p>
      </section>
    </article>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  loadLocatorWindow,
  loadLocatorWorkbench,
  retryInvalidLocators,
} from '@/api/backend-service'
import type {
  SelectorBoundary,
  WindowRecompute,
  WorkbenchPanel,
  WorkbenchSnapshot,
} from '@/data/locator-types'

const snapshot = ref<WorkbenchSnapshot | null>(null)
const windows = ref<Record<number, WindowRecompute>>({})
const policy = ref('force_rebuild')
const errorMessage = ref('')
const retryMessage = ref('')
const retrying = ref(false)

const panels = computed<WorkbenchPanel[]>(() => snapshot.value?.panels ?? [])
const totalInvalid = computed(() =>
  panels.value.reduce((sum, panel) => sum + panel.retry_boundary.length, 0),
)

const OUTCOMES: Record<string, string> = {
  hit: '主策略命中',
  fallback_hit: '回退命中',
  miss: '未命中',
  conflict: '结构冲突',
}

function outcomeLabel(outcome: string): string {
  return OUTCOMES[outcome] ?? outcome
}

function boundaryLabel(boundary: SelectorBoundary): string {
  const prefix = boundary.kind === 'id' ? '#' : boundary.kind === 'css' ? '.' : ''
  return `${prefix}${boundary.value}`
}

async function reload() {
  errorMessage.value = ''
  try {
    snapshot.value = await loadLocatorWorkbench()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '定位工作台读取失败'
  }
}

async function recompute(panel: WorkbenchPanel) {
  errorMessage.value = ''
  try {
    windows.value = {
      ...windows.value,
      [panel.model_id]: await loadLocatorWindow(panel.model_id),
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '窗口重算失败'
  }
}

async function retryInvalid() {
  retrying.value = true
  errorMessage.value = ''
  retryMessage.value = ''
  try {
    const result = await retryInvalidLocators(undefined, policy.value)
    retryMessage.value =
      `已重试 ${result.retried} 个失效元素，恢复 ${result.recovered} 个，` +
      `冲突 ${result.conflicts} 个，探测 ${result.probes} 次，` +
      `仍失效 ${result.remaining_invalid} 个（未重复扫描整组选择器）`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '失效元素重试失败'
  } finally {
    retrying.value = false
  }
}

onMounted(reload)
</script>

<style scoped>
.locator-workbench .policy-picker {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--muted);
}
.policy-picker select {
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 13px;
}
.model-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
  margin-bottom: 14px;
}
.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}
.panel-head h3 {
  margin: 0;
  font-size: 15px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.panel-sub { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
.panel-actions { display: flex; align-items: center; gap: 10px; }
.panel-meta { font-size: 12px; color: var(--muted); }
.fingerprint {
  font-family: monospace;
  font-size: 12px;
  color: var(--muted);
  background: #f1f5f9;
  border-radius: 4px;
  padding: 2px 6px;
}
.fingerprint-change {
  margin: 8px 0 0;
  font-size: 12px;
  color: #b45309;
  background: #fffbeb;
  border: 1px solid #fde68a;
  border-radius: 6px;
  padding: 6px 8px;
}
.panel-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 12px;
  margin: 12px 0;
}
.panel-block { margin-top: 10px; }
.panel-block h4 { margin: 0 0 8px; font-size: 13px; }
.rule-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.rule-list li { display: flex; align-items: center; gap: 8px; font-size: 12px; flex-wrap: wrap; }
.rule-name { font-weight: 600; }
.rule-list code, .boundary-list code {
  font-family: monospace;
  background: #f1f5f9;
  border-radius: 4px;
  padding: 1px 5px;
}
.boundary-list { display: flex; flex-wrap: wrap; gap: 6px; margin: 0; }
.tag {
  font-size: 11px;
  border-radius: 999px;
  padding: 1px 8px;
  background: #eef2ff;
  color: #4338ca;
}
.tag.warn { background: #fef3c7; color: #b45309; }
.muted { color: var(--muted); font-size: 12px; margin: 0; }
.compact th, .compact td { padding: 6px 8px; font-size: 12px; }
.note-cell { color: var(--muted); }
.panel-empty { background: #fff; border: 1px dashed var(--border); border-radius: 10px; padding: 24px; }
.retry-banner {
  margin: 0 0 10px;
  font-size: 12px;
  background: #ecfdf5;
  border: 1px solid #a7f3d0;
  color: #047857;
  border-radius: 6px;
  padding: 6px 10px;
}
</style>
