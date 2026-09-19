<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import CostConfirmDialog from '@/components/CostConfirmDialog.vue'
import StatusDot from '@/components/StatusDot.vue'
import { api } from '@/api/client'
import {
  PLATFORMS,
  formatDateTime,
  formatDuration,
  taskStatusLabel,
  taskStatusType,
} from '@/api/format'
import { useSystemStore } from '@/stores/system'

const router = useRouter()
const system = useSystemStore()

const stats = computed(() => system.stats ?? {})
const platformCounts = computed(() => stats.value.by_platform ?? {})

// Selecting stages is the difference between a free run and a paid one, so the
// choice is explicit and the cost line is derived from the selection.
const STAGES = [
  { value: 'fetch', label: '采集热点', cost: 'TikHub 3 次调用（计费）' },
  { value: 'analyze', label: 'AI 分析', cost: 'DeepSeek 逐条分析（计费）' },
  { value: 'detail', label: '抓取详情正文', cost: 'TikHub 详情调用（计费，默认关闭）' },
  { value: 'rewrite', label: 'AI 二创', cost: 'DeepSeek 生成三平台文案（计费）' },
  { value: 'notify', label: '发送通知', cost: '仅通过官方接口推送，不产生 AI 费用' },
]
const selectedStages = ref(['fetch', 'analyze', 'rewrite'])
const dialogVisible = ref(false)
const running = ref(false)

const costSummary = computed(() => {
  const picked = STAGES.filter((stage) => selectedStages.value.includes(stage.value))
  if (!picked.length) return '未选择任何阶段'
  return picked.map((stage) => `${stage.label}：${stage.cost}`).join('；')
})

const hasBilledStage = computed(() =>
  selectedStages.value.some((stage) => ['fetch', 'analyze', 'detail', 'rewrite'].includes(stage)),
)

const recentTasks = ref([])
const tasksLoading = ref(false)

async function loadTasks() {
  tasksLoading.value = true
  try {
    const response = await api.tasks({ limit: 5 })
    recentTasks.value = response.items ?? []
  } catch {
    recentTasks.value = []
  } finally {
    tasksLoading.value = false
  }
}

function askToRun() {
  if (!selectedStages.value.length) {
    ElMessage.warning('请至少选择一个阶段')
    return
  }
  dialogVisible.value = true
}

async function confirmRun() {
  running.value = true
  try {
    const response = await api.runPipeline({ stages: selectedStages.value.join(',') })
    const status = response.run?.status
    if (status === 'success') ElMessage.success('运行完成')
    else if (status === 'partial') ElMessage.warning('部分阶段失败，已保留成功部分，详见任务记录')
    else ElMessage.error('运行失败，详见任务记录')
    dialogVisible.value = false
    await Promise.all([system.refresh({ probe: false }), loadTasks()])
  } catch {
    // The interceptor already showed the message; keep the dialog open so the
    // operator can retry without re-selecting stages.
  } finally {
    running.value = false
  }
}

onMounted(() => {
  if (!system.loaded) system.refresh({ probe: true })
  loadTasks()
})
</script>

<template>
  <div class="sh-page">
    <div class="sh-page-header">
      <div>
        <h2 class="sh-page-title">总览</h2>
        <p class="sh-page-subtitle">
          今日采集与 AI 处理情况（AI 统计为累计值，不受"今日"限制）
        </p>
      </div>
      <div style="display: flex; gap: 8px">
        <el-button :loading="system.loading" @click="system.refresh({ probe: true })">
          <el-icon><Refresh /></el-icon>&nbsp;刷新
        </el-button>
        <el-button type="primary" @click="askToRun">
          <el-icon><VideoPlay /></el-icon>&nbsp;运行流水线
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="system.error"
      class="sh-card"
      type="error"
      :closable="false"
      show-icon
      :title="`后端状态获取失败：${system.error}`"
    />

    <el-row :gutter="16" class="sh-card">
      <el-col :span="6">
        <el-card shadow="never">
          <div class="sh-muted">今日采集</div>
          <div style="font-size: 26px; font-weight: 600">{{ stats.total_items ?? '-' }}</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div class="sh-muted">已 AI 分析</div>
          <div style="font-size: 26px; font-weight: 600">{{ stats.analyses ?? '-' }}</div>
          <div class="sh-muted">推荐 {{ stats.recommended ?? 0 }} 条</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div class="sh-muted">已入选二创</div>
          <div style="font-size: 26px; font-weight: 600">{{ stats.selected ?? '-' }}</div>
          <div class="sh-muted">已生成 {{ stats.rewrites ?? 0 }} 篇</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never">
          <div class="sh-muted">待人工审核</div>
          <div
            style="font-size: 26px; font-weight: 600"
            :style="{ color: (stats.needs_review ?? 0) > 0 ? '#e6a23c' : undefined }"
          >
            {{ stats.needs_review ?? '-' }}
          </div>
          <div class="sh-muted">可直接发布 {{ stats.ready_to_publish ?? 0 }} 篇</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" class="sh-card">
      <el-col :span="8">
        <el-card shadow="never" style="height: 100%">
          <template #header>今日各平台</template>
          <div
            v-for="platform in PLATFORMS"
            :key="platform.value"
            style="display: flex; justify-content: space-between; padding: 6px 0"
          >
            <span>{{ platform.label }}</span>
            <span style="font-weight: 600">{{ platformCounts[platform.value] ?? 0 }}</span>
          </div>
          <el-divider style="margin: 10px 0" />
          <div style="display: flex; justify-content: space-between">
            <span class="sh-muted">跨平台话题组</span>
            <span>{{ stats.topic_groups ?? 0 }}</span>
          </div>
        </el-card>
      </el-col>

      <el-col :span="8">
        <el-card shadow="never" style="height: 100%">
          <template #header>依赖组件</template>
          <div
            v-for="component in system.componentList"
            :key="component.key"
            style="display: flex; justify-content: space-between; padding: 6px 0"
          >
            <StatusDot
              :status="component.status"
              :label="component.label"
              :detail="component.detail"
            />
            <span class="sh-muted">{{ component.latency_ms ? `${component.latency_ms} ms` : '' }}</span>
          </div>
          <template v-if="stats.needs_verification">
            <el-divider style="margin: 10px 0" />
            <div class="sh-muted">
              {{ stats.needs_verification }} 条分析被标记为"需核实"，基于它的二创不会进入"可发布"
            </div>
          </template>
        </el-card>
      </el-col>

      <el-col :span="8">
        <el-card shadow="never" style="height: 100%">
          <template #header>定时任务</template>
          <div style="display: flex; justify-content: space-between; padding: 6px 0">
            <span>调度器</span>
            <el-tag :type="system.schedule?.running ? 'success' : 'info'" size="small">
              {{ system.schedule?.running ? '运行中' : '未运行' }}
            </el-tag>
          </div>
          <div style="display: flex; justify-content: space-between; padding: 6px 0">
            <span>每日运行时间</span>
            <span>{{ (system.schedule?.times || []).join('、') || '未配置' }}</span>
          </div>
          <div
            v-for="job in system.schedule?.jobs || []"
            :key="job.id"
            style="padding: 6px 0"
          >
            <div class="sh-muted">下次运行</div>
            <div>{{ formatDateTime(job.next_run_time) }}</div>
          </div>
          <div v-if="!system.schedule?.enabled" class="sh-muted" style="padding-top: 6px">
            定时任务已关闭（SCHEDULER_ENABLED=false），只能手动触发
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="sh-card">
      <template #header>
        <div style="display: flex; align-items: center">
          <span>运行流水线</span>
          <div class="sh-spacer" />
          <el-button link type="primary" @click="router.push('/tasks')">查看全部任务</el-button>
        </div>
      </template>
      <p class="sh-muted" style="margin-top: 0">
        阶段可自由组合：只读数据不需要运行流水线。带"计费"的阶段会真实消耗 TikHub
        配额或 DeepSeek token，点击"运行"后会再次确认。
      </p>
      <el-checkbox-group v-model="selectedStages">
        <el-checkbox v-for="stage in STAGES" :key="stage.value" :value="stage.value">
          {{ stage.label }}
        </el-checkbox>
      </el-checkbox-group>
      <el-alert
        v-if="hasBilledStage"
        style="margin-top: 12px"
        type="warning"
        :closable="false"
        show-icon
        title="所选阶段会产生费用"
        :description="costSummary"
      />
      <el-alert
        v-else
        style="margin-top: 12px"
        type="info"
        :closable="false"
        show-icon
        title="所选阶段不产生 AI / 采集费用"
        :description="costSummary"
      />
      <div style="margin-top: 12px">
        <el-button type="primary" @click="askToRun">运行</el-button>
      </div>
    </el-card>

    <el-card shadow="never" class="sh-card">
      <template #header>最近任务</template>
      <el-table v-loading="tasksLoading" :data="recentTasks" size="small">
        <el-table-column prop="id" label="#" width="70" />
        <el-table-column label="类型" width="110">
          <template #default="{ row }">{{ row.task_type }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="taskStatusType(row.status)" size="small">
              {{ taskStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="开始时间" width="170">
          <template #default="{ row }">{{ formatDateTime(row.started_at) }}</template>
        </el-table-column>
        <el-table-column label="耗时" width="110">
          <template #default="{ row }">{{ formatDuration(row.duration_ms) }}</template>
        </el-table-column>
        <el-table-column label="错误" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="sh-muted">{{ row.error_message || '' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <el-button link type="primary" @click="router.push('/tasks')">详情</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <CostConfirmDialog
      v-model="dialogVisible"
      title="确认运行流水线"
      description="将按顺序执行所选阶段，并在任务记录中留下一条记录。"
      :cost="costSummary"
      confirm-text="确认运行"
      :loading="running"
      @confirm="confirmRun"
    />
  </div>
</template>
