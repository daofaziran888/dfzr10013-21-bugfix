<template>
  <section class="page" data-module="scheme-analysis">
    <header class="page-head">
      <div>
        <h2>方案分析（决策建议推送）</h2>
        <p class="page-desc">
          读取决策选项的情景收益、成本与风险明细，统一量纲后计算情景结果，生成方案对比结论数据集；
          缺失项单独留痕不按零处理，排序与敏感性同批绑定，供报告端取数。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="loadSample">填充示例数据</button>
        <button class="btn primary" type="button" @click="dryRun">试算（不落库）</button>
        <button class="btn primary" type="button" @click="createRun">发起正式分析</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">已出具批次</span>
        <strong class="stat-value">{{ runs.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">可排名批次</span>
        <strong class="stat-value">{{ rankableCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">当前批次状态</span>
        <strong class="stat-value">{{ current ? statusText(current.status) : '—' }}</strong>
      </article>
    </div>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <div class="grid-2">
      <!-- 左：输入区 -->
      <div class="panel">
        <h3>分析输入</h3>
        <div class="form-grid">
          <label><span>分析主题</span><input v-model="form.subject" placeholder="如：新产线投资方案对比" /></label>
          <label><span>基准币种</span><input v-model="form.base_currency" /></label>
          <label><span>基准周期单位</span>
            <select v-model="form.base_period_unit">
              <option value="month">月（month）</option>
              <option value="quarter">季度（quarter）</option>
              <option value="year">年（year）</option>
            </select>
          </label>
          <label><span>汇率表（JSON，外币→基准币）</span>
            <input v-model="ratesText" placeholder='如 {"USD": 7.0}' />
          </label>
        </div>

        <h4>决策选项</h4>
        <table class="data-table compact">
          <thead><tr><th>选项编号</th><th>选项名称</th><th></th></tr></thead>
          <tbody>
            <tr v-for="(opt, i) in form.options" :key="i">
              <td><input v-model="opt.option_id" /></td>
              <td><input v-model="opt.name" /></td>
              <td><button class="link" type="button" @click="form.options.splice(i, 1)">删除</button></td>
            </tr>
          </tbody>
        </table>
        <button class="btn" type="button" @click="form.options.push({ option_id: '', name: '' })">新增选项</button>

        <h4>情景数据（概率 / 收益 / 成本 / 币种 / 周期）</h4>
        <table class="data-table compact">
          <thead>
            <tr>
              <th>选项</th><th>情景</th><th>概率</th><th>收益</th><th>成本</th>
              <th>金额单位</th><th>周期</th><th>周期单位</th><th>状态</th><th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, i) in form.scenarios" :key="i">
              <td><input v-model="row.option_id" list="option-ids" /></td>
              <td>
                <select v-model="row.scenario">
                  <option value="悲观">悲观</option>
                  <option value="中性">中性</option>
                  <option value="乐观">乐观</option>
                </select>
              </td>
              <td><input v-model.number="row.probability" type="number" step="0.01" :disabled="row.status === 'missing'" /></td>
              <td><input v-model.number="row.benefit" type="number" :disabled="row.status === 'missing'" /></td>
              <td><input v-model.number="row.cost" type="number" :disabled="row.status === 'missing'" /></td>
              <td><input v-model="row.currency" placeholder="万元 / CNY / USD" :disabled="row.status === 'missing'" /></td>
              <td><input v-model.number="row.period" type="number" :disabled="row.status === 'missing'" /></td>
              <td>
                <select v-model="row.period_unit" :disabled="row.status === 'missing'">
                  <option value="月">月</option>
                  <option value="quarter">季度</option>
                  <option value="year">年</option>
                </select>
              </td>
              <td>
                <select v-model="row.status">
                  <option value="estimated">已估计</option>
                  <option value="missing">尚无可靠估计</option>
                </select>
              </td>
              <td><button class="link" type="button" @click="form.scenarios.splice(i, 1)">删除</button></td>
            </tr>
          </tbody>
        </table>
        <button class="btn" type="button" @click="addScenarioRow">新增情景行</button>

        <h4>风险量度（相关风险填同一相关组，合并时不重复计入）</h4>
        <table class="data-table compact">
          <thead>
            <tr><th>选项</th><th>风险指标</th><th>量度值</th><th>量度类型</th><th>币种</th><th>相关组</th><th>状态</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="(row, i) in form.risks" :key="i">
              <td><input v-model="row.option_id" list="option-ids" /></td>
              <td><input v-model="row.metric" /></td>
              <td><input v-model.number="row.value" type="number" :disabled="row.status === 'missing'" /></td>
              <td>
                <select v-model="row.measure">
                  <option value="score">评分(score)</option>
                  <option value="probability">概率(probability)</option>
                  <option value="amount">金额(amount)</option>
                </select>
              </td>
              <td><input v-model="row.currency" placeholder="金额类填写" :disabled="row.status === 'missing'" /></td>
              <td><input v-model="row.correlation_group" placeholder="如 g1" /></td>
              <td>
                <select v-model="row.status">
                  <option value="estimated">已估计</option>
                  <option value="missing">尚无可靠估计</option>
                </select>
              </td>
              <td><button class="link" type="button" @click="form.risks.splice(i, 1)">删除</button></td>
            </tr>
          </tbody>
        </table>
        <button class="btn" type="button" @click="form.risks.push({ option_id: '', metric: '', value: null, measure: 'score', currency: null, correlation_group: null, status: 'estimated' })">新增风险行</button>
        <datalist id="option-ids">
          <option v-for="opt in form.options" :key="opt.option_id" :value="opt.option_id" />
        </datalist>
      </div>

      <!-- 右：批次与结果 -->
      <div class="panel">
        <h3>已出具分析批次（不可变）</h3>
        <table class="data-table compact">
          <thead>
            <tr><th>批次号</th><th>主题</th><th>状态</th><th>可排名</th><th>首位方案</th><th>派生自</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="item in runs" :key="item.run_id">
              <td>{{ item.run_id }}</td>
              <td>{{ item.subject }}</td>
              <td>{{ statusText(item.status) }}</td>
              <td>{{ item.rankable ? '是' : '否' }}</td>
              <td>{{ item.top_option ?? '—' }}</td>
              <td>{{ item.derived_from_run_id ?? '—' }}</td>
              <td><button class="link" type="button" @click="openRun(item.run_id)">查看</button></td>
            </tr>
            <tr v-if="!runs.length"><td colspan="7" class="empty-state">暂无批次，发起正式分析后生成</td></tr>
          </tbody>
        </table>

        <div v-if="current">
          <h3>批次 {{ current.run_id }} 结论数据集</h3>
          <p :class="current.rankable ? 'ok-text' : 'warn-text'"><strong>{{ current.conclusion?.headline }}</strong></p>
          <p class="muted">结论性质：{{ current.conclusion?.robust ? '稳健结论' : '依赖假设的推荐' }}
            ；综合得分仅用于排序，不代表确定收益</p>

          <h4>核对项（缺最差情景 / 相关风险 / 概率误差 / 轻微翻转）</h4>
          <ul class="check-list">
            <li v-for="(c, i) in current.checks" :key="i" :class="`level-${c.level}`">
              [{{ c.level }}] {{ c.message }}
            </li>
            <li v-if="!current.checks?.length" class="muted">无核对项</li>
          </ul>

          <template v-if="current.rankable">
            <h4>方案排序（与本批次情景明细绑定）</h4>
            <table class="data-table compact">
              <thead>
                <tr><th>名次</th><th>选项</th><th>综合得分</th><th>期望月均净收益</th><th>维度得分</th></tr>
              </thead>
              <tbody>
                <tr v-for="row in current.ranking" :key="row.option_id">
                  <td>{{ row.rank }}</td>
                  <td>{{ row.option_id }}（{{ row.name }}）</td>
                  <td>{{ row.composite_score }}</td>
                  <td>{{ formatMoney(row.expected_per_month) }}</td>
                  <td>
                    <span v-for="(v, k) in row.dimension_scores" :key="k" class="dim-chip">
                      {{ dimLabel(k) }}: {{ v }}
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>

            <h4>共同可比情景对比（{{ current.base_currency }} · 月均）</h4>
            <table class="data-table compact">
              <thead>
                <tr><th>情景</th><th v-for="opt in current.options" :key="opt.option_id">{{ opt.option_id }} 净收益/月</th></tr>
              </thead>
              <tbody>
                <tr v-for="row in current.comparison_table" :key="row.scenario">
                  <td>{{ row.label }}</td>
                  <td v-for="opt in current.options" :key="opt.option_id">
                    {{ row[opt.option_id] ? formatMoney(row[opt.option_id].net_base_per_month) : '缺失（未按零处理）' }}
                  </td>
                </tr>
              </tbody>
            </table>

            <h4>敏感性：名次翻转来源与敏感区间</h4>
            <p v-if="!current.sensitivity.flip_events?.length" class="ok-text">全部权重与概率扫描区间内名次未翻转，结论稳健。</p>
            <ul class="check-list">
              <li v-for="(e, i) in current.sensitivity.flip_events" :key="i" class="level-critical">
                {{ e.message }}
              </li>
            </ul>
            <table class="data-table compact">
              <thead><tr><th>扫描对象</th><th>翻转区间</th><th>首次翻转</th></tr></thead>
              <tbody>
                <tr v-for="s in current.sensitivity.weight_sweeps" :key="s.dimension">
                  <td>权重 · {{ s.dimension_label }}</td>
                  <td>{{ s.flip_interval ?? '不翻转' }}</td>
                  <td>{{ s.first_flip?.at_weight ?? '—' }}</td>
                </tr>
                <tr v-for="s in current.sensitivity.probability_sweeps" :key="s.scenario">
                  <td>概率 · {{ s.scenario_label }}</td>
                  <td>{{ s.flip_interval ?? '不翻转' }}</td>
                  <td>{{ s.first_flip?.at_delta ?? '—' }}</td>
                </tr>
              </tbody>
            </table>
            <p v-if="current.sensitivity.fragile_to_small_weight_change" class="error-text">
              ⚠ 权重轻微变动（±0.05 以内）即翻转，推荐依赖假设，不能视为稳健结论。
            </p>

            <h4>调整权重复算（生成派生批次，原批次不回写）</h4>
            <div class="form-grid">
              <label v-for="dim in dimensions" :key="dim.key">
                <span>{{ dim.label }}</span>
                <input v-model.number="weightDraft[dim.key]" type="number" step="0.05" min="0" max="1" />
              </label>
            </div>
            <p class="muted">权重之和：{{ weightSum }}（须等于 1）</p>
            <button class="btn primary" type="button" :disabled="Math.abs(weightSum - 1) > 1e-6" @click="reweight">
              用调整后权重生成派生批次
            </button>
            <p v-if="lastFlip !== null" :class="lastFlip ? 'warn-text' : 'ok-text'">
              派生批次相对本批次排名{{ lastFlip ? '已翻转' : '未翻转' }}
            </p>
          </template>

          <h4>情景覆盖与缺口（缺失项不按零值/零风险处理）</h4>
          <table class="data-table compact">
            <thead><tr><th>选项</th><th>情景完整</th><th>风险完整</th><th>参与排名</th><th>未覆盖项</th></tr></thead>
            <tbody>
              <tr v-for="c in current.coverage_summary" :key="c.option_id">
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

          <h4>量纲转换依据（可复核）</h4>
          <table class="data-table compact">
            <thead><tr><th>币种转换</th><th>系数</th><th>周期转换</th><th>系数</th><th>周期月数</th></tr></thead>
            <tbody>
              <tr v-for="(b, i) in current.conversion_basis" :key="i">
                <td>{{ b.currency.from }} → {{ b.currency.to }}（{{ b.currency.rate_source }}）</td>
                <td>{{ b.currency.factor }}</td>
                <td>{{ b.period.from_unit }} → {{ b.period.to_unit }}</td>
                <td>{{ b.period.factor }}</td>
                <td>{{ b.period.period_months }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

const ENDPOINT = '/api/decision/scheme-analysis'

interface ScenarioRow {
  option_id: string
  scenario: string
  probability: number | null
  benefit: number | null
  cost: number | null
  currency: string | null
  period: number | null
  period_unit: string | null
  status: string
}
interface RiskRow {
  option_id: string
  metric: string
  value: number | null
  measure: string
  currency: string | null
  correlation_group: string | null
  status: string
}

const form = reactive({
  subject: '新产线投资方案对比',
  base_currency: 'CNY',
  base_period_unit: 'month',
  options: [
    { option_id: 'A', name: '自建产线' },
    { option_id: 'B', name: '外包合作' },
  ] as Array<{ option_id: string; name: string }>,
  scenarios: [] as ScenarioRow[],
  risks: [] as RiskRow[],
})
const ratesText = ref('{"USD": 7.0}')

const dimensions = [
  { key: 'expected_return', label: '期望净收益（月均）' },
  { key: 'downside_protection', label: '下行保护（悲观情景净收益）' },
  { key: 'risk_control', label: '风险控制（风险指数反向）' },
]
const weightDraft = reactive<Record<string, number>>({
  expected_return: 0.5,
  downside_protection: 0.25,
  risk_control: 0.25,
})
const weightSum = computed(() =>
  Number((weightDraft.expected_return + weightDraft.downside_protection + weightDraft.risk_control).toFixed(6)),
)

interface RunSummary {
  run_id: string
  subject: string
  status: string
  rankable: boolean
  top_option: string | null
  derived_from_run_id: string | null
  created_at: string
}

const runs = ref<RunSummary[]>([])
const current = ref<Record<string, any> | null>(null)
const errorMessage = ref('')
const lastFlip = ref<boolean | null>(null)
const rankableCount = computed(() => runs.value.filter((r) => r.rankable).length)

function addScenarioRow() {
  form.scenarios.push({
    option_id: form.options[0]?.option_id ?? '',
    scenario: '悲观',
    probability: null,
    benefit: null,
    cost: null,
    currency: '万元',
    period: 12,
    period_unit: '月',
    status: 'estimated',
  })
}

function buildPayload() {
  let rates: Record<string, number> = {}
  try {
    rates = ratesText.value.trim() ? JSON.parse(ratesText.value) : {}
  } catch {
    throw new Error('汇率表 JSON 格式不正确')
  }
  return {
    subject: form.subject,
    base_currency: form.base_currency,
    base_period_unit: form.base_period_unit,
    currency_rates: rates,
    options: form.options,
    scenarios: form.scenarios,
    risks: form.risks,
  }
}

async function post(path: string, body: unknown) {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const data = await response.json()
  if (!response.ok) {
    const detail = data.detail
    if (detail && typeof detail === 'object' && detail.errors) {
      const lines = detail.errors.map((e: Record<string, string>) => `· [${e.code}] ${e.message}`)
      throw new Error(`${detail.message ?? '计算失败'}\n${lines.join('\n')}`)
    }
    throw new Error(typeof detail === 'string' ? detail : '请求失败')
  }
  return data
}

async function dryRun() {
  errorMessage.value = ''
  try {
    const data = await post(`${ENDPOINT}/dry-run`, buildPayload())
    data.run_id = '(试算结果，未落库)'
    current.value = data
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '试算失败'
  }
}

async function createRun() {
  errorMessage.value = ''
  try {
    current.value = await post(`${ENDPOINT}/runs`, buildPayload())
    await loadRuns()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分析失败，未发布批次'
  }
}

async function reweight() {
  if (!current.value?.run_id || String(current.value.run_id).startsWith('(')) {
    errorMessage.value = '请先打开一个已保存批次再调整权重'
    return
  }
  errorMessage.value = ''
  try {
    const data = await post(`${ENDPOINT}/runs/${current.value.run_id}/reweight`, {
      weights: { ...weightDraft },
    })
    lastFlip.value = Boolean(data.ranking_flipped)
    current.value = data
    await loadRuns()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '权重调整失败'
  }
}

async function loadRuns() {
  const response = await request(`${ENDPOINT}/runs`)
  if (response.ok) {
    const data = await response.json()
    runs.value = data.items ?? []
  }
}

async function openRun(runId: string) {
  errorMessage.value = ''
  lastFlip.value = null
  try {
    const response = await request(`${ENDPOINT}/runs/${runId}`)
    if (!response.ok) throw new Error('批次读取失败')
    const data = await response.json()
    current.value = data
    if (data.weights) Object.assign(weightDraft, data.weights)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批次读取失败'
  }
}

function loadSample() {
  form.subject = '新产线投资方案对比'
  form.scenarios = [
    { option_id: 'A', scenario: '悲观', probability: 0.2, benefit: 800, cost: 700, currency: '万元', period: 12, period_unit: '月', status: 'estimated' },
    { option_id: 'A', scenario: '中性', probability: 0.5, benefit: 1500, cost: 900, currency: '万元', period: 12, period_unit: '月', status: 'estimated' },
    { option_id: 'A', scenario: '乐观', probability: 0.3, benefit: 2400, cost: 1100, currency: '万元', period: 12, period_unit: '月', status: 'estimated' },
    { option_id: 'B', scenario: '悲观', probability: 0.2, benefit: 500, cost: 350, currency: 'CNY万元', period: 4, period_unit: 'quarter', status: 'estimated' },
    { option_id: 'B', scenario: '中性', probability: 0.5, benefit: 1100, cost: 600, currency: 'CNY万元', period: 4, period_unit: 'quarter', status: 'estimated' },
    { option_id: 'B', scenario: '乐观', probability: 0.3, benefit: 1600, cost: 800, currency: 'CNY万元', period: 4, period_unit: 'quarter', status: 'estimated' },
  ]
  form.risks = [
    { option_id: 'A', metric: '交付延期率', value: 0.3, measure: 'probability', currency: null, correlation_group: 'g1', status: 'estimated' },
    { option_id: 'A', metric: '供应中断率', value: 0.25, measure: 'probability', currency: null, correlation_group: 'g1', status: 'estimated' },
    { option_id: 'A', metric: '合规罚没', value: 80, measure: 'amount', currency: '万元', correlation_group: null, status: 'estimated' },
    { option_id: 'B', metric: '交付延期率', value: 0.15, measure: 'probability', currency: null, correlation_group: 'g1', status: 'estimated' },
    { option_id: 'B', metric: '供应中断率', value: 0.12, measure: 'probability', currency: null, correlation_group: 'g1', status: 'estimated' },
    { option_id: 'B', metric: '合规罚没', value: 30, measure: 'amount', currency: '万元', correlation_group: null, status: 'estimated' },
  ]
}

function statusText(status: string) {
  return { rankable: '可排名', unrankable: '资料不全（仅缺口说明）', failed: '计算失败' }[status] ?? status
}
function dimLabel(key: string | number) {
  return dimensions.find((d) => d.key === String(key))?.label ?? String(key)
}
function formatMoney(value: number | null) {
  if (value === null || value === undefined) return '—'
  return `${Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`
}

onMounted(loadRuns)
</script>

<style scoped>
.grid-2 { display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr); gap: 14px; align-items: start; }
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }
.panel h3 { margin: 8px 0; font-size: 15px; }
.panel h4 { margin: 14px 0 6px; font-size: 13px; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-bottom: 8px; }
.form-grid label span, .panel label span { display: block; font-size: 12px; color: var(--muted); }
input, select { width: 100%; padding: 4px 6px; border: 1px solid var(--border); border-radius: 4px; font-size: 12px; }
table.compact th, table.compact td { padding: 4px 6px; font-size: 12px; }
table.compact input, table.compact select { min-width: 70px; }
.check-list { list-style: none; padding-left: 0; font-size: 12px; }
.check-list li { padding: 3px 0; border-bottom: 1px dashed var(--border); }
.level-warning { color: #b54708; }
.level-critical { color: #b42318; }
.level-info { color: var(--muted); }
.ok-text { color: #067647; font-size: 13px; }
.warn-text { color: #b54708; font-size: 13px; }
.muted { color: var(--muted); font-size: 12px; }
.dim-chip, .gap-chip { display: inline-block; background: #eef2ff; border-radius: 4px; padding: 1px 6px; margin: 1px 3px 1px 0; font-size: 11px; }
.gap-chip { background: #fff4ed; color: #b54708; }
.page-actions { display: flex; gap: 8px; }
</style>
