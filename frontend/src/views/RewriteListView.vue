<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { api } from '@/api/client'
import {
  formatDateTime,
  platformLabel,
  platformType,
  statusLabel,
  statusType,
} from '@/api/format'

const router = useRouter()

const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const status = ref('')
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const params = { limit: pageSize.value, offset: (page.value - 1) * pageSize.value }
    if (status.value) params.status = status.value
    const response = await api.rewrites(params)
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

onMounted(load)
</script>

<template>
  <div class="sh-page">
    <div class="sh-page-header">
      <div>
        <h2 class="sh-page-title">二创审核</h2>
        <p class="sh-page-subtitle">
          AI 生成的三平台文案。系统只负责生成与提醒，<strong>不会自动发布</strong>。
        </p>
      </div>
      <el-button :loading="loading" @click="load">
        <el-icon><Refresh /></el-icon>&nbsp;刷新
      </el-button>
    </div>

    <el-card shadow="never" class="sh-card">
      <div class="sh-toolbar">
        <el-select v-model="status" placeholder="全部状态" style="width: 170px" @change="search">
          <el-option label="全部状态" value="" />
          <el-option label="可发布" value="READY_TO_PUBLISH" />
          <el-option label="需人工审核" value="NEEDS_REVIEW" />
        </el-select>
        <div class="sh-spacer" />
        <span class="sh-muted">共 {{ total }} 篇</span>
      </div>

      <el-table v-loading="loading" :data="rows" size="small">
        <el-table-column label="来源热点" min-width="260">
          <template #default="{ row }">
            <el-link type="primary" @click="router.push(`/rewrites/${row.hot_content_id}`)">
              {{ row.source?.title || `#${row.hot_content_id}` }}
            </el-link>
            <div class="sh-muted">
              {{ row.summary || '暂无摘要' }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="来源平台" width="100">
          <template #default="{ row }">
            <el-tag :type="platformType(row.source?.platform)" size="small">
              {{ platformLabel(row.source?.platform) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
            <div v-if="row.needs_verification" class="sh-muted">需核实</div>
          </template>
        </el-table-column>
        <el-table-column label="风险标记" width="140">
          <template #default="{ row }">
            <el-tag
              v-for="flag in row.risk_flags"
              :key="flag"
              type="danger"
              size="small"
              effect="plain"
              style="margin-right: 4px"
            >
              {{ flag }}
            </el-tag>
            <span v-if="!row.risk_flags?.length" class="sh-muted">无</span>
          </template>
        </el-table-column>
        <el-table-column label="生成时间" width="170">
          <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <el-button link type="primary" @click="router.push(`/rewrites/${row.hot_content_id}`)">
              审核
            </el-button>
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
  </div>
</template>
