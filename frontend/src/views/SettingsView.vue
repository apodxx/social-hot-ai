<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import CostConfirmDialog from '@/components/CostConfirmDialog.vue'
import StatusDot from '@/components/StatusDot.vue'
import { api } from '@/api/client'
import { normaliseBool } from '@/api/format'
import { useSystemStore } from '@/stores/system'

const system = useSystemStore()

const GROUP_LABELS = {
  tikhub: 'TikHub 采集',
  deepseek: 'DeepSeek 分析',
  hot: '热点采集',
  analysis: 'AI 分析',
  detail: '详情抓取',
  rewrite: 'AI 二创',
  scheduler: '定时任务',
  notification: '通知',
  mcp: 'MCP 服务（供 AI 客户端调用）',
  media: '图片素材库（关键词搜索）',
  interest: '领域偏好（决定推给你的方向）',
  watch: '定时领域搜索（会计费）',
}

const loading = ref(false)
const saving = ref(false)
const settings = ref(null)
/** key → the value being edited (secrets start empty: blank means "keep"). */
const form = reactive({})
/** key → the value as loaded, so only real changes are sent. */
const original = reactive({})
const restartNeeded = ref([])
const envPath = ref('')
/** Which accordion sections are open; `v-model` must bind a ref, not an expression. */
const openGroups = ref([])

const profile = reactive({
  account_name: '',
  field: '',
  target_audience: '',
  style: '',
  tone: '',
  preferred_topics: [],
  forbidden: [],
})
const profilePath = ref('')
const profileSaving = ref(false)
const promptPreview = ref('')

const notification = ref(null)
const testDialogVisible = ref(false)
const testing = ref(false)

const groups = computed(() => settings.value?.groups ?? {})

const changedKeys = computed(() =>
  Object.entries(form).filter(([key, value]) => {
    // A blank secret is "leave it alone", not a change.
    if (value === '' && settings.value?.secret_keys?.includes(key)) return false
    return String(value) !== String(original[key] ?? '')
  }),
)

async function load() {
  loading.value = true
  try {
    const response = await api.settings()
    settings.value = response.settings
    envPath.value = response.settings.path
    Object.keys(form).forEach((key) => delete form[key])
    Object.keys(original).forEach((key) => delete original[key])
    for (const entries of Object.values(response.settings.groups ?? {})) {
      for (const entry of entries) {
        // Bools are normalised on load so the switch reflects the real state and a
        // page nobody touched reports zero changes (see `normaliseBool`).
        let value = ''
        if (entry.kind === 'secret') value = ''
        else if (entry.kind === 'bool') value = normaliseBool(entry.value)
        else value = entry.value ?? ''
        form[entry.key] = value
        original[entry.key] = value
      }
    }
    openGroups.value = Object.keys(response.settings.groups ?? {})
  } catch {
    // The interceptor reported it; keep whatever was already on screen.
  } finally {
    loading.value = false
  }
}

async function loadProfile() {
  try {
    const response = await api.profile()
    Object.assign(profile, {
      account_name: response.profile?.account_name ?? '',
      field: response.profile?.field ?? '',
      target_audience: response.profile?.target_audience ?? '',
      style: response.profile?.style ?? '',
      tone: response.profile?.tone ?? '',
      preferred_topics: response.profile?.preferred_topics ?? [],
      forbidden: response.profile?.forbidden ?? [],
    })
    profilePath.value = response.path ?? ''
    promptPreview.value = response.prompt_block ?? ''
  } catch {
    // A missing or invalid profile is not fatal: the form just starts empty.
  }
}

async function loadNotification() {
  try {
    const response = await api.notificationStatus()
    notification.value = response.notification
  } catch {
    notification.value = null
  }
}

/**
 * A readable summary of one channel's configuration.
 *
 * There is no `detail` key on a channel description (that field belongs to a *send
 * result*), so the column has to be built from the fields each channel actually
 * reports — otherwise it renders as a permanently blank column.
 */
function channelNote(channel) {
  if (!channel) return ''
  const parts = []
  if (channel.msgtype) parts.push(`消息类型 ${channel.msgtype}`)
  if (channel.webhook_host) parts.push(channel.webhook_host)
  if (channel.target_kind) parts.push(`目标类型 ${channel.target_kind}`)
  if (channel.base_url) parts.push(channel.base_url)
  if (channel.verified === false) parts.push('未经真实凭据验证')
  if (channel.note) parts.push(channel.note)
  if (channel.name === 'log') parts.push('仅写日志，不实际投递')
  return parts.join(' · ')
}

async function saveSettings() {
  const values = Object.fromEntries(changedKeys.value)
  if (!Object.keys(values).length) {
    ElMessage.info('没有需要保存的修改')
    return
  }
  saving.value = true
  try {
    const response = await api.updateSettings(values)
    if (response.restart_required) {
      restartNeeded.value = response.changed ?? []
      ElMessage.warning('已保存，需要重启后端服务后生效')
    } else {
      ElMessage.success('已保存')
    }
    await load()
    await system.refresh({ probe: false })
    await loadNotification()
  } catch {
    // The interceptor reported it; the form keeps the operator's input.
  } finally {
    saving.value = false
  }
}

async function saveProfile() {
  profileSaving.value = true
  try {
    const response = await api.updateProfile({ ...profile })
    promptPreview.value = response.prompt_block ?? ''
    ElMessage.success('账号定位已保存并立即生效（无需重启）')
  } catch {
    // Reported by the interceptor.
  } finally {
    profileSaving.value = false
  }
}

async function confirmTest() {
  testing.value = true
  try {
    const response = await api.sendNotificationTest()
    const results = response.results ?? []
    const failed = results.filter((result) => !result.ok)
    if (!results.length || failed.length === results.length) {
      ElMessage.error('发送失败，请检查通道配置')
    } else if (failed.length) {
      ElMessage.warning('部分通道发送失败')
    } else {
      ElMessage.success('测试消息已发送')
    }
    testDialogVisible.value = false
    await loadNotification()
  } catch {
    // Reported by the interceptor.
  } finally {
    testing.value = false
  }
}

async function confirmRestartNoted() {
  try {
    await ElMessageBox.confirm(
      '环境变量在进程启动时读取。请手动重启后端服务（Ctrl+C 后重新执行启动命令），然后刷新本页确认状态。',
      '如何生效',
      { confirmButtonText: '知道了', showCancelButton: false },
    )
  } catch {
    // Dismissing is fine.
  }
}

onMounted(async () => {
  await Promise.all([load(), loadProfile(), loadNotification()])
  if (!system.loaded) system.refresh({ probe: true })
})
</script>

<template>
  <div class="sh-page">
    <div class="sh-page-header">
      <div>
        <h2 class="sh-page-title">设置</h2>
        <p class="sh-page-subtitle">密钥只写入不读取：留空表示保持原值</p>
      </div>
      <el-button :loading="loading" @click="load">
        <el-icon><Refresh /></el-icon>&nbsp;重新加载
      </el-button>
    </div>

    <el-alert
      v-if="restartNeeded.length"
      class="sh-card"
      type="warning"
      :closable="false"
      show-icon
      title="以下环境变量已写入 .env，需要重启后端才生效"
    >
      <template #default>
        <div style="margin-top: 6px">
          已修改：{{ restartNeeded.join('、') }}
          <el-button link type="primary" @click="confirmRestartNoted">如何生效？</el-button>
        </div>
      </template>
    </el-alert>

    <el-card v-loading="loading" shadow="never" class="sh-card">
      <template #header>
        <div style="display: flex; align-items: center">
          <span>环境变量</span>
          <div class="sh-spacer" />
          <span class="sh-muted">{{ envPath }}</span>
        </div>
      </template>

      <el-collapse v-model="openGroups">
        <el-collapse-item
          v-for="(entries, group) in groups"
          :key="group"
          :name="group"
          :title="GROUP_LABELS[group] || group"
        >
          <el-form label-width="180px" label-position="left">
            <el-form-item v-for="entry in entries" :key="entry.key" :label="entry.label">
              <div style="width: 100%">
                <el-switch
                  v-if="entry.kind === 'bool'"
                  :model-value="form[entry.key]"
                  active-value="true"
                  inactive-value="false"
                  @update:model-value="(value) => (form[entry.key] = value)"
                />
                <el-input
                  v-else-if="entry.kind === 'secret'"
                  v-model="form[entry.key]"
                  type="password"
                  show-password
                  :placeholder="entry.masked ? `已配置：${entry.masked}（留空保持不变）` : '未配置'"
                />
                <el-input v-else v-model="form[entry.key]" />
                <div class="sh-muted" style="margin-top: 4px">
                  <code>{{ entry.key }}</code>
                  <span v-if="entry.help"> · {{ entry.help }}</span>
                  <span v-if="entry.kind === 'secret'"> · 出于安全考虑不会回显完整值</span>
                </div>
              </div>
            </el-form-item>
          </el-form>
        </el-collapse-item>
      </el-collapse>

      <div style="margin-top: 14px; display: flex; align-items: center; gap: 10px">
        <el-button type="primary" :loading="saving" :disabled="!changedKeys.length" @click="saveSettings">
          保存修改<span v-if="changedKeys.length">（{{ changedKeys.length }} 项）</span>
        </el-button>
        <span class="sh-muted">保存后需要重启后端服务才生效</span>
      </div>
    </el-card>

    <el-card shadow="never" class="sh-card">
      <template #header>
        <div style="display: flex; align-items: center">
          <span>账号定位</span>
          <div class="sh-spacer" />
          <el-tag type="success" size="small">保存后立即生效</el-tag>
        </div>
      </template>
      <el-form label-width="120px">
        <el-form-item label="账号名称">
          <el-input v-model="profile.account_name" placeholder="例如：AI 工具研究所" />
        </el-form-item>
        <el-form-item label="领域">
          <el-input v-model="profile.field" placeholder="例如：AI / 科技" />
        </el-form-item>
        <el-form-item label="目标人群">
          <el-input v-model="profile.target_audience" placeholder="例如：18-30 岁职场人" />
        </el-form-item>
        <el-form-item label="风格">
          <el-input v-model="profile.style" placeholder="例如：通俗、口语化" />
        </el-form-item>
        <el-form-item label="语气">
          <el-input v-model="profile.tone" placeholder="例如：自然、不夸张" />
        </el-form-item>
        <el-form-item label="偏好话题">
          <el-select
            v-model="profile.preferred_topics"
            multiple
            filterable
            allow-create
            default-first-option
            placeholder="输入后回车添加"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="禁止内容">
          <el-select
            v-model="profile.forbidden"
            multiple
            filterable
            allow-create
            default-first-option
            placeholder="输入后回车添加，例如：虚假信息"
            style="width: 100%"
          />
        </el-form-item>
      </el-form>
      <el-button type="primary" :loading="profileSaving" @click="saveProfile">保存账号定位</el-button>
      <div class="sh-muted" style="margin-top: 6px">
        文件：{{ profilePath }}（每次运行时重新读取，无需重启）
      </div>
      <el-collapse v-if="promptPreview" style="margin-top: 12px">
        <el-collapse-item title="查看写入提示词的账号定位片段">
          <pre style="white-space: pre-wrap; font-size: 12px; margin: 0">{{ promptPreview }}</pre>
        </el-collapse-item>
      </el-collapse>
    </el-card>

    <el-card shadow="never" class="sh-card">
      <template #header>
        <div style="display: flex; align-items: center">
          <span>通知通道</span>
          <div class="sh-spacer" />
          <el-tag :type="notification?.ready ? 'success' : 'info'" size="small">
            {{ notification?.ready ? '可用' : '不可用' }}
          </el-tag>
        </div>
      </template>

      <template v-if="notification">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="已启用">
            {{ notification.enabled ? '是' : '否' }}
          </el-descriptions-item>
          <el-descriptions-item label="配置的通道">
            {{ (notification.configured_channels || []).join('、') || '无' }}
          </el-descriptions-item>
          <el-descriptions-item label="可用通道">
            {{ (notification.usable || []).join('、') || '无' }}
          </el-descriptions-item>
          <el-descriptions-item label="单条上限">
            {{ notification.max_chars }} 字符（超出会自动分段）
          </el-descriptions-item>
        </el-descriptions>

        <el-table :data="notification.channels" size="small" style="margin-top: 12px">
          <el-table-column prop="name" label="通道" width="140" />
          <el-table-column label="已配置" width="100">
            <template #default="{ row }">
              <el-tag :type="row.configured ? 'success' : 'info'" size="small" effect="plain">
                {{ row.configured ? '是' : '否' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="说明" show-overflow-tooltip>
            <template #default="{ row }">
              <span :class="{ 'sh-muted': !channelNote(row) }">
                {{ channelNote(row) || '-' }}
              </span>
            </template>
          </el-table-column>
        </el-table>

        <el-alert
          v-if="notification.blocked_by?.length"
          style="margin-top: 12px"
          type="info"
          :closable="false"
          show-icon
          title="为什么不可用"
          :description="notification.blocked_by.join('；')"
        />

        <div style="margin-top: 12px">
          <el-button :disabled="!notification.enabled" @click="testDialogVisible = true">
            发送测试消息
          </el-button>
        </div>
      </template>
      <el-empty v-else description="无法获取通知状态" :image-size="70" />
    </el-card>

    <el-card shadow="never" class="sh-card">
      <template #header>系统状态</template>
      <el-descriptions :column="1" border size="small">
        <el-descriptions-item v-for="component in system.componentList" :key="component.key" :label="component.label">
          <StatusDot :status="component.status" :label="component.statusLabel" />
          <span class="sh-muted" style="margin-left: 10px">{{ component.detail }}</span>
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <CostConfirmDialog
      v-model="testDialogVisible"
      title="发送通知测试消息"
      description="将通过已配置的通道发送一条测试消息，用于确认通知链路是否通畅。"
      cost="不产生 TikHub 或 DeepSeek 费用"
      :billed="false"
      confirm-text="发送"
      :loading="testing"
      @confirm="confirmTest"
    />
  </div>
</template>
