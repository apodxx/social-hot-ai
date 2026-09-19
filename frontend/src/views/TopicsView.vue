<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { api } from '@/api/client'
import { formatDateTime, platformLabel, platformType } from '@/api/format'

const router = useRouter()

const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const crossPlatformOnly = ref(false)
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const response = await api.topics({
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value,
      cross_platform_only: crossPlatformOnly.value,
    })
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
        <h2 class="sh-page-title">话题聚合</h2>
        <p class="sh-page-subtitle">
          去重第 4 层：跨平台话题聚类结果。相似度阈值较高，跨平台命中较少属正常现象。
        </p>
      </div>
      <el-button :loading="loading" @click="load">
        <el-icon><Refresh /></el-icon>&nbsp;刷新
      </el-button>
    </div>

    <el-card shadow="never" class="sh-card">
      <div class="sh-toolbar">
        <el-switch
          v-model="crossPlatformOnly"
          active-text="只看跨平台"
          @change="search"
        />
        <div class="sh-spacer" />
        <span class="sh-muted">共 {{ total }} 个话题</span>
      </div>

      <el-table v-if="total" v-loading="loading" :data="rows" size="small">
        <el-table-column label="话题" min-width="260">
          <template #default="{ row }">
            <el-link type="primary" @click="router.push(`/topics/${row.id}`)">
              {{ row.topic }}
            </el-link>
            <div class="sh-muted">{{ row.summary || '暂无摘要' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="涉及平台" width="200">
          <template #default="{ row }">
            <el-tag
              v-for="platform in row.platforms"
              :key="platform"
              :type="platformType(platform)"
              size="small"
              effect="plain"
              style="margin-right: 4px"
            >
              {{ platformLabel(platform) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="跨平台" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.is_cross_platform" type="success" size="small">是</el-tag>
            <span v-else class="sh-muted">否</span>
          </template>
        </el-table-column>
        <el-table-column label="关联内容" width="90">
          <template #default="{ row }">{{ row.related_contents }}</template>
        </el-table-column>
        <el-table-column label="更新时间" width="170">
          <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90">
          <template #default="{ row }">
            <el-button link type="primary" @click="router.push(`/topics/${row.id}`)">
              查看
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-empty
        v-else-if="!loading"
        description="暂无话题聚合结果"
        :image-size="90"
      >
        <template #description>
          <div style="max-width: 620px; line-height: 1.8; color: #909399">
            <p style="margin: 0 0 6px">暂无话题聚合结果</p>
            <p style="margin: 0; font-size: 12px">
              去重第 4 层使用词面相似度（阈值 0.82）判断"同一话题"。不同平台的表述差异
              很大，实测最高相似度约 0.35，因此跨平台成组很少甚至为 0 —— 这是当前算法的
              已知局限，不代表采集失败。热点列表与二创流程不受影响。
            </p>
          </div>
        </template>
      </el-empty>

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
