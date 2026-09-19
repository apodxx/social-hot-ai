<script setup>
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'

import StatusDot from '@/components/StatusDot.vue'
import { useSystemStore } from '@/stores/system'
import { formatDateTime } from '@/api/format'

const route = useRoute()
const system = useSystemStore()

const activeMenu = computed(() => {
  // Keep the parent item highlighted on detail routes.
  if (route.path.startsWith('/topics')) return '/topics'
  if (route.path.startsWith('/rewrites')) return '/rewrites'
  return route.path
})

onMounted(() => {
  // One probe per page load, not per route change.
  system.refresh({ probe: true })
})
</script>

<template>
  <el-container style="height: 100%">
    <el-aside width="200px" style="background: var(--sh-sidebar)">
      <div
        style="
          color: #fff;
          font-weight: 600;
          font-size: 16px;
          padding: 18px 20px 12px;
          letter-spacing: 0.5px;
        "
      >
        SocialHot AI
        <div style="font-size: 11px; color: #8b98a8; font-weight: 400; margin-top: 4px">
          全网热点 · AI 二创
        </div>
      </div>
      <el-menu
        :default-active="activeMenu"
        router
        background-color="#1f2d3d"
        text-color="#c0c4cc"
        active-text-color="#ffffff"
      >
        <el-menu-item index="/">
          <el-icon><DataBoard /></el-icon><span>总览</span>
        </el-menu-item>
        <el-menu-item index="/hot">
          <el-icon><List /></el-icon><span>热点列表</span>
        </el-menu-item>
        <el-menu-item index="/topics">
          <el-icon><Connection /></el-icon><span>话题聚合</span>
        </el-menu-item>
        <el-menu-item index="/rewrites">
          <el-icon><EditPen /></el-icon><span>二创审核</span>
        </el-menu-item>
        <el-menu-item index="/promo">
          <el-icon><Promotion /></el-icon><span>项目推广</span>
        </el-menu-item>
        <el-menu-item index="/knowledge">
          <el-icon><Reading /></el-icon><span>知识科普</span>
        </el-menu-item>
        <el-menu-item index="/tasks">
          <el-icon><Timer /></el-icon><span>任务记录</span>
        </el-menu-item>
        <el-menu-item index="/settings">
          <el-icon><Setting /></el-icon><span>设置</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header
        style="
          background: #fff;
          border-bottom: 1px solid #e4e7ed;
          display: flex;
          align-items: center;
          gap: 20px;
          height: 56px;
        "
      >
        <span style="font-weight: 600">{{ route.meta?.title || '' }}</span>
        <div class="sh-spacer" />
        <StatusDot
          v-for="component in system.componentList"
          :key="component.key"
          :status="component.status"
          :label="component.label"
          :detail="component.detail"
        />
        <span v-if="system.schedule?.timezone" class="sh-muted">
          调度 {{ system.schedule.timezone }}
        </span>
        <el-tag v-if="system.phase" size="small" type="info">Phase {{ system.phase }}</el-tag>
        <span v-if="system.stats?.latest_task" class="sh-muted">
          最近任务 {{ formatDateTime(system.stats.latest_task.started_at) }}
        </span>
      </el-header>

      <el-main style="padding: 0; overflow-y: auto">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>
