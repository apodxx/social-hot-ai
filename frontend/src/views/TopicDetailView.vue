<script setup>
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api } from '@/api/client'
import { formatDateTime, formatNumber, platformLabel, platformType } from '@/api/format'

const route = useRoute()
const router = useRouter()

const topic = ref(null)
const members = ref([])
const loading = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const response = await api.topic(route.params.id)
    topic.value = response.topic
    members.value = response.members ?? []
  } catch (requestError) {
    error.value = requestError?.response?.data?.detail || '话题不存在'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="sh-page">
    <div class="sh-page-header">
      <div>
        <h2 class="sh-page-title">话题详情</h2>
        <p class="sh-page-subtitle">同一话题在各平台的热点条目</p>
      </div>
      <el-button @click="router.push('/topics')">
        <el-icon><ArrowLeft /></el-icon>&nbsp;返回列表
      </el-button>
    </div>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error" />

    <el-card v-else v-loading="loading" shadow="never" class="sh-card">
      <template v-if="topic">
        <h3 style="margin-top: 0">{{ topic.topic }}</h3>
        <p class="sh-muted" style="margin-top: 0">{{ topic.summary || '暂无摘要' }}</p>
        <div style="margin-bottom: 8px">
          <el-tag
            v-for="platform in topic.platforms"
            :key="platform"
            :type="platformType(platform)"
            size="small"
            effect="plain"
            style="margin-right: 6px"
          >
            {{ platformLabel(platform) }}
          </el-tag>
          <el-tag v-if="topic.is_cross_platform" type="success" size="small">跨平台</el-tag>
        </div>
        <div class="sh-muted">
          关联 {{ topic.related_contents }} 条 · 更新于 {{ formatDateTime(topic.updated_at) }}
        </div>
      </template>

      <el-divider content-position="left">成员条目</el-divider>

      <el-table :data="members" size="small">
        <el-table-column label="标题" min-width="260">
          <template #default="{ row }">
            <el-link v-if="row.url" :href="row.url" target="_blank" type="primary">
              {{ row.title }}
            </el-link>
            <span v-else>{{ row.title }}</span>
            <div class="sh-muted">{{ row.author || '未知作者' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="平台" width="90">
          <template #default="{ row }">
            <el-tag :type="platformType(row.platform)" size="small">
              {{ platformLabel(row.platform) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="排名" width="70">
          <template #default="{ row }">{{ row.rank ?? '-' }}</template>
        </el-table-column>
        <el-table-column label="热度" width="90">
          <template #default="{ row }">{{ formatNumber(row.hot_value) }}</template>
        </el-table-column>
        <el-table-column label="采集时间" width="170">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>
