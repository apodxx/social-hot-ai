<script setup>
import { onMounted, ref } from 'vue'

import { api } from '@/api/client'
import { formatDateTime, formatDuration, taskStatusLabel, taskStatusType } from '@/api/format'

const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const status = ref('')
const loading = ref(false)

const detailVisible = ref(false)
const active = ref(null)

async function load() {
  loading.value = true
  try {
    const params = { limit: pageSize.value, offset: (page.value - 1) * pageSize.value }
    if (status.value) params.status = status.value
    const response = await api.tasks(params)
    rows.value = response.items ?? []
    total.value = response.total ?? 0
  } catch {
    rows.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function search() {
  page.value = 1
  load()
}

function openDetail(row) {
  active.value = row
  detailVisible.value = true
}

/** The step list is an array of {stage, status, ...}; render whatever is present. */
function stepText(step) {
  if (typeof step === 'string') return step
  if (!step) return ''
  const stage = step.stage || step.name || ''
  const state = step.status || ''
  return [stage, state].filter(Boolean).join(' · ')
}

onMounted(load)
</script>

<template>
  <div class="sh-page">
    <div class="sh-page-header">
      <div>
        <h2 class="sh-page-title">任务记录</h2>
        <p class="sh-page-subtitle">每次流水线运行的阶段结果、耗时与错误</p>
      </div>
      <el-button :loading="loading" @click="load">
        <el-icon><Refresh /></el-icon>&nbsp;刷新
      </el-button>
    </div>

    <el-card shadow="never" class="sh-card">
      <div class="sh-toolbar">
        <el-select v-model="status" placeholder="全部状态" style="width: 160px" @change="search">
          <el-option label="全部状态" value="" />
          <el-option label="成功" value="success" />
          <el-option label="部分成功" value="partial" />
          <el-option label="失败" value="failed" />
        </el-select>
        <div class="sh-spacer" />
        <span class="sh-muted">共 {{ total }} 条</span>
      </div>

      <el-table v-loading="loading" :data="rows" size="small">
        <el-table-column prop="id" label="#" width="70" />
        <el-table-column prop="task_type" label="触发方式" width="110" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="taskStatusType(row.status)" size="small">
              {{ taskStatusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="开始" width="170">
          <template #default="{ row }">{{ formatDateTime(row.started_at) }}</template>
        </el-table-column>
        <el-table-column label="结束" width="170">
          <template #default="{ row }">{{ formatDateTime(row.finished_at) }}</template>
        </el-table-column>
        <el-table-column label="耗时" width="110">
          <template #default="{ row }">{{ formatDuration(row.duration_ms) }}</template>
        </el-table-column>
        <el-table-column label="阶段" min-width="200">
          <template #default="{ row }">
            <el-tag
              v-for="(step, index) in row.steps"
              :key="index"
              size="small"
              effect="plain"
              style="margin-right: 4px"
            >
              {{ stepText(step) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="错误" width="110">
          <template #default="{ row }">
            <el-button v-if="row.error_message" link type="danger" @click="openDetail(row)">
              查看
            </el-button>
            <span v-else class="sh-muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <el-button link type="primary" @click="openDetail(row)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        style="margin-top: 14px; justify-content: flex-end"
        layout="total, prev, pager, next"
        :total="total"
        v-model:current-page="page"
        v-model:page-size="pageSize"
        @current-change="load"
      />
    </el-card>

    <el-drawer v-model="detailVisible" title="任务详情" size="640px">
      <template v-if="active">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="任务 ID">{{ active.id }}</el-descriptions-item>
          <el-descriptions-item label="触发方式">{{ active.task_type }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="taskStatusType(active.status)" size="small">
              {{ taskStatusLabel(active.status) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="耗时">
            {{ formatDuration(active.duration_ms) }}
          </el-descriptions-item>
        </el-descriptions>

        <el-alert
          v-if="active.error_message"
          style="margin-top: 12px"
          type="error"
          :closable="false"
          show-icon
          title="错误信息"
          :description="active.error_message"
        />

        <el-divider content-position="left">阶段结果</el-divider>
        <pre style="background: #fafafa; border: 1px solid #ebeef5; border-radius: 6px; padding: 12px; overflow-x: auto; font-size: 12px">{{
          JSON.stringify(active.steps, null, 2)
        }}</pre>

        <el-divider content-position="left">汇总</el-divider>
        <pre style="background: #fafafa; border: 1px solid #ebeef5; border-radius: 6px; padding: 12px; overflow-x: auto; font-size: 12px">{{
          JSON.stringify(active.summary, null, 2)
        }}</pre>
      </template>
    </el-drawer>
  </div>
</template>
