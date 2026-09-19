import { defineStore } from 'pinia'

import { api, errorMessage } from '@/api/client'

const COMPONENT_LABELS = {
  tikhub: 'TikHub 采集',
  database: '数据库',
  deepseek: 'DeepSeek',
}

const STATUS_LABELS = {
  connected: '正常',
  error: '异常',
  not_configured: '未配置',
  skipped: '未检测',
}

/**
 * Shared backend health and dashboard counters.
 *
 * Both the header (a status dot on every page) and the dashboard need them, so the
 * requests live here rather than in a view: switching pages must not re-probe
 * TikHub. The probe is a free metadata call, but it is still a network round trip.
 */
export const useSystemStore = defineStore('system', {
  state: () => ({
    loading: false,
    loaded: false,
    error: '',
    phase: null,
    components: {},
    stats: null,
    schedule: null,
  }),

  getters: {
    componentList(state) {
      return Object.entries(state.components).map(([key, value]) => ({
        key,
        label: COMPONENT_LABELS[key] ?? key,
        statusLabel: STATUS_LABELS[value?.status] ?? value?.status ?? '-',
        ...value,
      }))
    },
    healthy(state) {
      return state.components?.tikhub?.status === 'connected'
    },
  },

  actions: {
    async refresh({ probe = true } = {}) {
      this.loading = true
      this.error = ''
      // The three calls are independent; a failure in one must not blank the others.
      const [status, stats, schedule] = await Promise.allSettled([
        api.status(probe),
        api.stats(true),
        api.schedule(),
      ])

      if (status.status === 'fulfilled') {
        this.components = status.value.components ?? {}
        this.phase = status.value.phase ?? null
      } else {
        this.error = errorMessage(status.reason)
      }
      if (stats.status === 'fulfilled') this.stats = stats.value.stats ?? null
      if (schedule.status === 'fulfilled') this.schedule = schedule.value.schedule ?? null

      this.loaded = true
      this.loading = false
    },

    async refreshStats() {
      try {
        const response = await api.stats(true)
        this.stats = response.stats ?? null
      } catch {
        // A stale counter is better than an error banner on a page that loaded.
      }
    },
  },
})
