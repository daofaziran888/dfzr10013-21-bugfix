<template>
  <section class="page" data-module="report">
    <header class="page-head">
      <div>
        <h2>经营分析报告管理</h2>
        <p class="page-desc">维护经营分析的模板选择、关键发现与分发管理，为经营分析报告提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记分析报告</button>
        <button class="btn" type="button" @click="exportRows">导出经营分析报告清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无经营分析报告数据，可先登记分析报告</td>
        </tr>
      </tbody>
    </table>

    <section class="scheme-panel">
      <h3>引用方案分析结论数据集（跨模块取数）</h3>
      <p class="page-desc">
        输入方案分析批次号，消费该批次自身的情景覆盖、权重、转换依据与敏感区间；
        排序与敏感性同批绑定，不会混入其他批次的排名。
      </p>
      <form class="filter-bar" @submit.prevent="fetchSchemeDataset">
        <label class="filter-item">
          <span>方案分析批次号</span>
          <input v-model="schemeRunId" placeholder="如 SA-20261116-001" />
        </label>
        <button class="btn primary" type="submit">取数并生成报告素材</button>
      </form>
      <p v-if="schemeError" class="error-text">{{ schemeError }}</p>
      <div v-if="schemeDataset">
        <p :class="schemeDataset.rankable ? 'ok-text' : 'warn-text'">
          <strong>{{ schemeDataset.conclusion.headline }}</strong>
        </p>
        <p class="muted">
          来源批次 {{ schemeDataset.run_id }} · 基准 {{ schemeDataset.base_currency }}/{{ schemeDataset.base_period_unit }}
          · 结论性质：{{ schemeDataset.conclusion.robust ? '稳健结论' : '依赖假设的推荐' }}
        </p>
        <table class="data-table compact">
          <thead><tr><th>选项</th><th>情景完整</th><th>风险完整</th><th>参与排名</th><th>无法比较的缺口</th></tr></thead>
          <tbody>
            <tr v-for="c in schemeDataset.coverage_summary" :key="c.option_id">
              <td>{{ c.option_id }}（{{ c.name }}）</td>
              <td>{{ c.scenario_complete ? '是' : '否' }}</td>
              <td>{{ c.risk_complete ? '是' : '否' }}</td>
              <td>{{ c.included_in_ranking ? '是' : '否' }}</td>
              <td>
                <span v-for="(u, i) in c.uncovered_items" :key="i" class="gap-chip">
                  {{ u.type === 'scenario' ? u.label : `风险:${u.metric}` }}
                </span>
                <span v-if="!c.uncovered_items.length">—</span>
              </td>
            </tr>
          </tbody>
        </table>
        <h4>权重与敏感区间</h4>
        <ul class="check-list">
          <li v-for="(w, k) in schemeDataset.weights" :key="k">
            {{ schemeDataset.weight_labels[k] ?? k }}：{{ w }}
          </li>
        </ul>
        <ul class="check-list">
          <li v-for="(e, i) in schemeDataset.sensitivity.flip_events" :key="i" class="level-critical">
            {{ e.message }}
          </li>
          <li v-if="!schemeDataset.sensitivity.flip_events.length" class="ok-text">扫描区间内未发生名次翻转</li>
        </ul>
        <h4>转换依据（可复核）</h4>
        <table class="data-table compact">
          <thead><tr><th>币种</th><th>系数</th><th>周期</th><th>系数</th><th>月数</th></tr></thead>
          <tbody>
            <tr v-for="(b, i) in schemeDataset.conversion_basis" :key="i">
              <td>{{ b.currency.from }} → {{ b.currency.to }}</td>
              <td>{{ b.currency.factor }}</td>
              <td>{{ b.period.from_unit }} → {{ b.period.to_unit }}</td>
              <td>{{ b.period.factor }}</td>
              <td>{{ b.period.period_months }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条经营分析报告记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/report'
const columns = ["报告编号", "报告主题", "分析周期", "报告模板", "关键发现", "数据附件", "分发对象", "报告状态"]
const actions = ["生成报告", "分发报告", "归档报告"]
const statuses = ["草稿", "已生成", "已分发", "已归档"]
const stats = [{"label": "分析报告总数", "value": 0}, {"label": "已生成报告", "value": 0}, {"label": "已分发报告", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '分析报告登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('经营分析报告动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '经营分析报告操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('分析报告列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '经营分析报告列表读取失败'
  }
}

const schemeRunId = ref('')
const schemeDataset = ref<Record<string, any> | null>(null)
const schemeError = ref('')

async function fetchSchemeDataset() {
  schemeError.value = ''
  schemeDataset.value = null
  if (!schemeRunId.value.trim()) {
    schemeError.value = '请输入方案分析批次号'
    return
  }
  try {
    const response = await request(`${ENDPOINT}/scheme-datasets/${schemeRunId.value.trim()}`)
    const payload = await response.json()
    if (!response.ok) {
      throw new Error(payload.detail ?? '方案数据集取数失败')
    }
    schemeDataset.value = payload.dataset
  } catch (error) {
    schemeError.value = error instanceof Error ? error.message : '方案数据集取数失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.scheme-panel {
  margin-top: 18px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
}
.scheme-panel h3 { margin: 6px 0; font-size: 15px; }
.scheme-panel h4 { margin: 12px 0 6px; font-size: 13px; }
table.compact th, table.compact td { padding: 4px 6px; font-size: 12px; }
.check-list { list-style: none; padding-left: 0; font-size: 12px; }
.check-list li { padding: 3px 0; border-bottom: 1px dashed var(--border); }
.level-critical { color: #b42318; }
.ok-text { color: #067647; font-size: 13px; }
.warn-text { color: #b54708; font-size: 13px; }
.muted { color: var(--muted); font-size: 12px; }
.gap-chip { display: inline-block; background: #fff4ed; color: #b54708; border-radius: 4px; padding: 1px 6px; margin: 1px 3px 1px 0; font-size: 11px; }
</style>
