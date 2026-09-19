<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import CostConfirmDialog from '@/components/CostConfirmDialog.vue'
import ImageGenPanel from '@/components/ImageGenPanel.vue'
import { api } from '@/api/client'
import { useSystemStore } from '@/stores/system'
import {
  PLATFORMS,
  confidencePercent,
  formatDateTime,
  formatNumber,
  imageSrc,
  platformLabel,
  platformType,
  statusLabel,
  statusType,
} from '@/api/format'

const router = useRouter()
const system = useSystemStore()

const filters = reactive({
  platform: '',
  q: '',
  /** '' = 全部，'true'/'false' = 有/没有对应的 AI 标记 */
  recommended: '',
  selected: '',
  /** '' = 全部，'search' / 'hot' = 按来源筛选 */
  origin: '',
  withImages: '',
  /** Exact keyword a search result was collected under (not a title substring). */
  sourceKeyword: '',
})

const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)

const drawerVisible = ref(false)
const active = ref(null)

/** How many of the open item's images have a local copy (the durable one). */
const downloadedImages = computed(
  () => (active.value?.media?.images ?? []).filter((image) => image.local_path).length,
)

// --- per-item actions: nothing is spent until a button is clicked -------------
const analyzeDialogVisible = ref(false)
const analyzeRunning = ref(false)
const rewriteDialogVisible = ref(false)
const rewriteRunning = ref(false)

const activeTitle = computed(() => (active.value?.title ?? '').slice(0, 30))

// --- 推送到 QQ（会把内容发到外部，所以默认只预览） --------------------------
const pushDialogVisible = ref(false)
const pushPreview = ref(null)
const pushing = ref(false)
const pushLoading = ref(false)

async function previewPush() {
  pushLoading.value = true
  try {
    pushPreview.value = await api.pushItem(active.value.id, { dryRun: true })
    pushDialogVisible.value = true
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '预览失败')
  } finally {
    pushLoading.value = false
  }
}

async function confirmPush() {
  pushing.value = true
  // 先关对话框：发送可能几秒到几十秒，让用户对着一个转圈的模态框等，体验很差。
  // 结果用 toast + pushPreview 面板反馈（面板会就地更新成发送结果）。
  pushDialogVisible.value = false
  try {
    const response = await api.pushItem(active.value.id, { dryRun: false })
    if (response.ok) {
      ElMessage.success(`已发送 ${response.messages_sent} 条 QQ 消息`)
    } else {
      ElMessage.error(response.error || '部分内容发送失败')
    }
    pushPreview.value = response
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '发送失败')
  } finally {
    pushing.value = false
  }
}

const pushMessageCount = computed(() =>
  (pushPreview.value?.parts ?? []).reduce((sum, part) => sum + 1 + (part.image_count || 0), 0),
)

/** Re-read one row and keep the drawer open on it. */
async function refreshActive() {
  const id = active.value?.id
  if (!id) return
  await load()
  const match = rows.value.find((row) => row.id === id)
  if (match) active.value = match
  await loadPending()
}

async function confirmAnalyzeItem() {
  analyzeRunning.value = true
  try {
    const response = await api.runAnalysisOne(active.value.id)
    const tokens = (response.run?.tokens ?? {}).total_tokens ?? 0
    ElMessage.success(`分析完成：消耗 ${tokens} tokens`)
    analyzeDialogVisible.value = false
    await refreshActive()
  } catch {
    // Reported by the interceptor.
  } finally {
    analyzeRunning.value = false
  }
}

async function confirmRewriteItem() {
  rewriteRunning.value = true
  try {
    const response = await api.runRewriteOne(active.value.id)
    const rewrite = (response.rewrites ?? [])[0]
    ElMessage.success(`二创完成：状态 ${rewrite?.status ?? '已生成'}`)
    rewriteDialogVisible.value = false
    await refreshActive()
  } catch {
    // Reported by the interceptor (a 409 means "分析这一步还没做").
  } finally {
    rewriteRunning.value = false
  }
}

// --- keyword search (§12): the only way to pull in a topic of your own choosing ---
const searchVisible = ref(false)
const searchDialogVisible = ref(false)
const searching = ref(false)
const searchForm = reactive({
  keyword: '',
  platforms: ['xiaohongshu', 'douyin', 'weibo'],
  limit: 20,
})

const billedCallCount = () => searchForm.platforms.length
const searchCost = () =>
  `将对 ${searchForm.platforms
    .map((name) => platformLabel(name))
    .join('、')} 各发起 1 次搜索请求，共 ${billedCallCount()} 次计费调用；` +
  '返回的图片会下载到本地素材库（不额外计费）。'

function askToSearch() {
  if (!searchForm.keyword.trim()) {
    ElMessage.warning('请输入要搜索的话题')
    return
  }
  if (!searchForm.platforms.length) {
    ElMessage.warning('请至少选择一个平台')
    return
  }
  searchDialogVisible.value = true
}

// --- the configured domain keywords (the watch stage). With the scheduler off this
// --- is the only manual trigger, so it lives next to the topic search.
const watchDialogVisible = ref(false)
const watchRunning = ref(false)

// --- the gap between "collected" and "analysed" ------------------------------
// Reported by the operator: 191 rows collected, "未分析" down the whole column. The
// analysis was not broken — nothing had run it. This banner makes that visible and
// offers the next step with a real count and cost.
const pending = ref(null)
const pendingLoading = ref(false)
// Named "batch" to keep it distinct from the per-item dialog in the drawer: one runs the
//分析 stage over many rows, the other analyses exactly one.
const batchAnalyzeDialogVisible = ref(false)
const batchAnalyzing = ref(false)

async function loadPending() {
  pendingLoading.value = true
  try {
    const response = await api.pendingAnalysis()
    pending.value = response.pending
  } catch {
    pending.value = null
  } finally {
    pendingLoading.value = false
  }
}

async function confirmAnalyze() {
  batchAnalyzing.value = true
  try {
    const response = await api.runAnalysis()
    const run = response.run ?? response
    ElMessage.success(
      `分析完成：处理 ${run.analysed ?? 0} 条，入选 ${run.selected ?? 0} 条，` +
        `消耗 token ${(run.tokens ?? {}).total_tokens ?? 0}`,
    )
    batchAnalyzeDialogVisible.value = false
    await Promise.all([loadPending(), load()])
  } catch {
    // Reported by the interceptor.
  } finally {
    batchAnalyzing.value = false
  }
}
const watchInfo = computed(() => {
  const schedule = system.schedule ?? {}
  return {
    keywords: schedule.watch_keywords ?? [],
    platforms: schedule.watch_platforms ?? [],
    calls: schedule.watch_billed_calls_per_run ?? 0,
  }
})

function askToWatch() {
  if (!watchInfo.value.keywords.length) {
    ElMessage.warning('未配置领域搜索词，请到「设置 → 定时领域搜索」添加')
    return
  }
  watchDialogVisible.value = true
}

async function confirmWatch() {
  watchRunning.value = true
  try {
    const response = await api.runWatch()
    const run = response.run ?? {}
    ElMessage.success(
      `领域搜索完成：${run.keywords?.length ?? 0} 个词，采集 ${run.fetched ?? 0} 条，` +
        `入库 ${run.inserted ?? 0} 条，图片 ${run.images_downloaded ?? 0} 张，` +
        `计费调用 ${run.billed_calls ?? 0} 次`,
    )
    watchDialogVisible.value = false
    filters.origin = 'search'
    load()
  } catch {
    // Reported by the interceptor.
  } finally {
    watchRunning.value = false
  }
}

async function confirmSearch() {
  searching.value = true
  try {
    const response = await api.searchTopic({
      keyword: searchForm.keyword.trim(),
      platforms: searchForm.platforms,
      limit: searchForm.limit,
    })
    const run = response.run ?? {}
    ElMessage.success(
      `采集 ${run.total_fetched ?? 0} 条，入库 ${run.stored?.inserted ?? 0} 条，` +
        `图片下载 ${run.images?.downloaded ?? 0} 张`,
    )
    searchDialogVisible.value = false
    searchVisible.value = false
    // Show exactly what this search collected: filter by the keyword it was stored
    // under, not by a title substring (only a few titles contain the keyword).
    filters.sourceKeyword = searchForm.keyword.trim()
    filters.origin = 'search'
    search()
  } catch {
    // The interceptor reported it; the dialog stays open so the keyword is not lost.
  } finally {
    searching.value = false
  }
}

function buildParams() {
  const params = { limit: pageSize.value, offset: (page.value - 1) * pageSize.value }
  if (filters.platform) params.platform = filters.platform
  if (filters.q) params.q = filters.q
  if (filters.recommended !== '') params.recommended = filters.recommended === 'true'
  if (filters.selected !== '') params.selected = filters.selected === 'true'
  if (filters.origin) params.origin = filters.origin
  if (filters.withImages !== '') params.with_images = filters.withImages === 'true'
  if (filters.sourceKeyword) params.source_keyword = filters.sourceKeyword
  return params
}

async function load() {
  loading.value = true
  try {
    const response = await api.stored(buildParams())
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

function resetFilters() {
  filters.platform = ''
  filters.q = ''
  filters.recommended = ''
  filters.selected = ''
  filters.origin = ''
  filters.withImages = ''
  filters.sourceKeyword = ''
  search()
}

function openDetail(row) {
  active.value = row
  drawerVisible.value = true
}

async function copyUrl(row) {
  if (!row.url) {
    ElMessage.warning('该条没有链接')
    return
  }
  try {
    await navigator.clipboard.writeText(row.url)
    ElMessage.success('链接已复制')
  } catch {
    ElMessage.warning('浏览器拒绝了剪贴板访问，请手动复制')
  }
}

onMounted(() => {
  load()
  loadPending()
})
</script>

<template>
  <div class="sh-page">
    <div class="sh-page-header">
      <div>
        <h2 class="sh-page-title">热点列表</h2>
        <p class="sh-page-subtitle">
          已入库的热点及其 AI 处理状态。读取本地数据不产生费用；"搜索话题"会调用 TikHub 计费接口。
        </p>
      </div>
      <div style="display: flex; gap: 8px">
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>&nbsp;刷新
        </el-button>
        <el-button type="primary" @click="searchVisible = true">
          <el-icon><Search /></el-icon>&nbsp;搜索话题
        </el-button>
        <el-button @click="askToWatch">
          <el-icon><Aim /></el-icon>&nbsp;领域搜索
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="pending && pending.would_be_sent > 0"
      class="sh-card"
      type="info"
      :closable="false"
      show-icon
    >
      <template #title>
        有 {{ pending.pending_rows }} 条还没有 AI 分析，其中 {{ pending.would_be_sent }} 条会被送去分析
      </template>
      <template #default>
        <div style="margin-top: 6px; line-height: 1.7">
          {{ pending.note }}
          <br />
          预计消耗约 <strong>¥{{ pending.estimated_cny }}</strong>（{{ pending.would_be_sent }} 条 × 约 ¥0.0023）。
          <el-button link type="primary" @click="batchAnalyzeDialogVisible = true">立即分析</el-button>
        </div>
      </template>
    </el-alert>

    <el-alert
      v-else-if="pending && pending.pending_rows > 0 && pending.would_be_sent === 0"
      class="sh-card"
      type="warning"
      :closable="false"
      show-icon
      :title="pending.note"
    />

    <el-card shadow="never" class="sh-card">
      <div class="sh-toolbar">
        <el-select v-model="filters.platform" placeholder="全部平台" clearable style="width: 140px" @change="search">
          <el-option
            v-for="platform in PLATFORMS"
            :key="platform.value"
            :label="platform.label"
            :value="platform.value"
          />
        </el-select>
        <el-input
          v-model="filters.q"
          placeholder="标题关键词"
          clearable
          style="width: 220px"
          @keyup.enter="search"
          @clear="search"
        />
        <el-select v-model="filters.recommended" placeholder="AI 推荐" style="width: 140px" @change="search">
          <el-option label="全部" value="" />
          <el-option label="仅看推荐" value="true" />
          <el-option label="仅看未推荐" value="false" />
        </el-select>
        <el-select v-model="filters.selected" placeholder="入选状态" style="width: 140px" @change="search">
          <el-option label="全部" value="" />
          <el-option label="已入选二创" value="true" />
          <el-option label="未入选" value="false" />
        </el-select>
        <el-select v-model="filters.origin" placeholder="来源" style="width: 130px" @change="search">
          <el-option label="全部来源" value="" />
          <el-option label="热搜榜单" value="hot" />
          <el-option label="关键词搜索" value="search" />
        </el-select>
        <el-select v-model="filters.withImages" placeholder="图片" style="width: 120px" @change="search">
          <el-option label="不限图片" value="" />
          <el-option label="只看有图" value="true" />
          <el-option label="只看无图" value="false" />
        </el-select>
        <el-button type="primary" @click="search">查询</el-button>
        <el-button @click="resetFilters">重置</el-button>
        <div class="sh-spacer" />
        <el-tag
          v-if="filters.sourceKeyword"
          closable
          type="success"
          @close="filters.sourceKeyword = ''; search()"
        >
          搜索词：{{ filters.sourceKeyword }}
        </el-tag>
        <span class="sh-muted">共 {{ total }} 条</span>
      </div>

      <el-table v-loading="loading" :data="rows" size="small" @row-click="openDetail">
        <el-table-column label="图" width="72">
          <template #default="{ row }">
            <el-image
              v-if="row.media?.images?.length"
              :src="imageSrc(row.media.images[0])"
              :preview-src-list="row.media.images.map(imageSrc)"
              preview-teleported
              fit="cover"
              style="width: 46px; height: 46px; border-radius: 4px"
              @click.stop
            >
              <template #error>
                <div class="sh-muted" style="font-size: 11px">失效</div>
              </template>
            </el-image>
            <span v-else class="sh-muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="标题" min-width="260">
          <template #default="{ row }">
            <div class="sh-title-cell">
              <span class="sh-title-main">
                {{ row.title }}
                <el-tag
                  v-if="row.media?.video?.url"
                  type="danger"
                  size="small"
                  effect="plain"
                  style="margin-left: 4px"
                >
                  视频
                </el-tag>
                <el-tag
                  v-else-if="row.content_type === 'note'"
                  type="success"
                  size="small"
                  effect="plain"
                  style="margin-left: 4px"
                >
                  图文
                </el-tag>
              </span>
              <span v-if="row.description" class="sh-muted">
                {{ row.description.slice(0, 60) }}{{ row.description.length > 60 ? '…' : '' }}
              </span>
              <span class="sh-muted">
                {{ row.author || '未知作者' }} · {{ formatDateTime(row.created_at) }}
              </span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="平台" width="90">
          <template #default="{ row }">
            <el-tag :type="platformType(row.platform)" size="small">
              {{ platformLabel(row.platform) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="94">
          <template #default="{ row }">
            <el-tag v-if="row.origin === 'search'" type="success" size="small" effect="plain">
              搜索
            </el-tag>
            <span v-else class="sh-muted">榜单</span>
          </template>
        </el-table-column>
        <el-table-column label="排名" width="70">
          <template #default="{ row }">{{ row.rank ?? '-' }}</template>
        </el-table-column>
        <el-table-column label="热度" width="90">
          <template #default="{ row }">{{ formatNumber(row.hot_value) }}</template>
        </el-table-column>
        <el-table-column label="AI 分析" width="170">
          <template #default="{ row }">
            <template v-if="row.analysis">
              <el-tag
                :type="row.analysis.recommended ? 'success' : 'info'"
                size="small"
                effect="plain"
              >
                {{ row.analysis.recommended ? '推荐' : '未推荐' }}
              </el-tag>
              <span class="sh-muted" style="margin-left: 6px">
                置信 {{ confidencePercent(row.analysis.confidence) }}
              </span>
              <el-tag
                v-if="row.analysis.needs_verification"
                type="warning"
                size="small"
                effect="plain"
                style="margin-left: 4px"
              >
                需核实
              </el-tag>
            </template>
            <span v-else class="sh-muted">未分析</span>
          </template>
        </el-table-column>
        <el-table-column label="入选" width="80">
          <template #default="{ row }">
            <el-tag v-if="row.analysis?.selected" type="primary" size="small">已入选</el-tag>
            <span v-else class="sh-muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="二创状态" width="120">
          <template #default="{ row }">
            <el-tag v-if="row.rewrite" :type="statusType(row.rewrite.status)" size="small">
              {{ statusLabel(row.rewrite.status) }}
            </el-tag>
            <span v-else class="sh-muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="openDetail(row)">详情</el-button>
            <el-button
              v-if="row.rewrite"
              link
              type="primary"
              @click.stop="router.push(`/rewrites/${row.id}`)"
            >
              看二创
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        style="margin-top: 14px; justify-content: flex-end"
        layout="total, sizes, prev, pager, next"
        :total="total"
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :page-sizes="[20, 50, 100]"
        @current-change="load"
        @size-change="search"
      />
    </el-card>

    <el-drawer v-model="drawerVisible" title="热点详情" size="520px">
      <template v-if="active">
        <h3 style="margin-top: 0">{{ active.title }}</h3>
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="平台">
            {{ platformLabel(active.platform) }}
          </el-descriptions-item>
          <el-descriptions-item label="排名 / 热度">
            {{ active.rank ?? '-' }} / {{ formatNumber(active.hot_value) }}
          </el-descriptions-item>
          <el-descriptions-item label="作者">
            {{ active.author || '未知' }}
          </el-descriptions-item>
          <el-descriptions-item label="首次采集">
            {{ formatDateTime(active.created_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="最近更新">
            {{ formatDateTime(active.updated_at) }}
          </el-descriptions-item>
        </el-descriptions>

        <div style="margin: 12px 0">
          <el-button size="small" @click="copyUrl(active)">复制链接</el-button>
          <el-link
            v-if="active.url"
            :href="active.url"
            target="_blank"
            type="primary"
            style="margin-left: 8px"
          >
            打开原链接
          </el-link>
        </div>

        <!-- 正文：榜单来源本来就没有，所以空的时候要解释原因而不是留白 -->
        <el-divider content-position="left">帖子内容</el-divider>
        <div v-if="active.description" class="sh-copy-block">
          <div class="sh-draft">{{ active.description }}</div>
          <div v-if="active.detail_fetched" class="sh-muted" style="margin-top: 8px">
            正文来自详情抓取（§16）
          </div>
        </div>
        <el-alert
          v-else
          type="info"
          :closable="false"
          show-icon
          :title="active.origin === 'hot' ? '这条没有正文：榜单来源是「词条」' : '这条没有正文'"
          :description="
            active.origin === 'hot'
              ? '微博/抖音热搜返回的是词条本身，不带正文和图片；小红书首页推荐流的卡片通常也不带。想要正文与图片，请用「搜索话题」采集真实帖子，或开启详情抓取（DETAIL_FETCH_ENABLED，每条 1 次计费调用）。'
              : '搜索来源一般带正文；这条可能是视频帖，或供应商未返回描述。'
          "
        />

        <el-divider content-position="left">
          图片
          <span v-if="active.media?.images?.length" class="sh-muted">
            （{{ active.media.images.length }} 张，点击可放大）
          </span>
        </el-divider>
        <template v-if="active.media?.images?.length">
          <el-image
            v-for="(image, index) in active.media.images"
            :key="index"
            :src="imageSrc(image)"
            :preview-src-list="active.media.images.map(imageSrc)"
            :initial-index="index"
            preview-teleported
            fit="cover"
            style="width: 96px; height: 96px; margin: 0 8px 8px 0; border-radius: 4px"
          >
            <template #error>
              <div class="sh-muted" style="font-size: 11px; line-height: 96px; text-align: center">
                失效
              </div>
            </template>
          </el-image>
          <div class="sh-muted">
            已下载到本地素材库 {{ downloadedImages }} / {{ active.media.images.length }} 张
            <template v-if="downloadedImages < active.media.images.length">
              —— 其余只存了链接（受每条下载上限限制，或下载失败）
            </template>
            <template v-else>—— 本地副本不会因平台链接过期而失效</template>
          </div>
        </template>
        <el-empty v-else description="这条没有图片" :image-size="60" />

        <template v-if="active.media?.video?.url">
          <el-divider content-position="left">视频</el-divider>
          <div style="display: flex; gap: 12px; align-items: flex-start">
            <el-image
              v-if="active.media.video.cover_url"
              :src="active.media.video.cover_url"
              fit="cover"
              style="width: 96px; height: 96px; border-radius: 4px; flex: none"
            />
            <div>
              <el-link :href="active.media.video.url" target="_blank" type="primary">
                打开视频链接
              </el-link>
              <div class="sh-muted" style="margin-top: 6px">
                视频**只保存链接，没有下载**（按既定策略：先存下来，不处理视频）。
                链接可能随平台策略失效。
              </div>
            </div>
          </div>
        </template>

        <el-divider content-position="left">
          AI 分析
          <span class="sh-muted">（只分析这一条）</span>
        </el-divider>
        <div style="margin-bottom: 10px">
          <el-button
            v-if="!active.analysis"
            type="primary"
            size="small"
            :loading="analyzeRunning"
            @click="analyzeDialogVisible = true"
          >
            <el-icon><MagicStick /></el-icon>&nbsp;AI 分析这一条
          </el-button>
          <el-button
            v-else
            size="small"
            :loading="analyzeRunning"
            @click="analyzeDialogVisible = true"
          >
            <el-icon><Refresh /></el-icon>&nbsp;重新分析
          </el-button>
          <span class="sh-muted" style="margin-left: 8px">
            {{ active.analysis ? '已分析，可重跑' : '尚未分析——点按钮才会调用，不点不花钱' }}
          </span>
        </div>
        <template v-if="active.analysis">
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="话题">
              {{ active.analysis.topic || '-' }}
            </el-descriptions-item>
            <el-descriptions-item label="摘要">
              {{ active.analysis.summary || '-' }}
            </el-descriptions-item>
            <el-descriptions-item label="为什么火">
              {{ active.analysis.why_hot || '-' }}
            </el-descriptions-item>
            <el-descriptions-item label="切入角度">
              {{ active.analysis.content_angle || '-' }}
            </el-descriptions-item>
            <el-descriptions-item label="推荐 / 置信">
              {{ active.analysis.recommended ? '推荐' : '未推荐' }} /
              {{ confidencePercent(active.analysis.confidence) }}
            </el-descriptions-item>
            <el-descriptions-item label="需核实">
              {{ active.analysis.needs_verification ? '是' : '否' }}
            </el-descriptions-item>
          </el-descriptions>
          <el-alert
            v-if="active.analysis.needs_verification"
            style="margin-top: 10px"
            type="warning"
            :closable="false"
            show-icon
            title="该分析被标记为需要核实"
            description="基于它的二创不会进入「可发布」，发布前请先核对事实。"
          />
        </template>
        <el-empty v-else description="尚未分析" :image-size="70" />

        <el-divider content-position="left">
          二创
          <span class="sh-muted">（只二创这一条）</span>
        </el-divider>        <div style="margin-bottom: 10px">
          <el-button
            v-if="!active.rewrite"
            type="primary"
            size="small"
            :disabled="!active.analysis"
            :loading="rewriteRunning"
            @click="rewriteDialogVisible = true"
          >
            <el-icon><EditPen /></el-icon>&nbsp;二创这一条
          </el-button>
          <el-button
            v-else
            size="small"
            :loading="rewriteRunning"
            @click="rewriteDialogVisible = true"
          >
            <el-icon><Refresh /></el-icon>&nbsp;重新二创
          </el-button>
          <span v-if="!active.analysis" class="sh-muted" style="margin-left: 8px">
            需要先「AI 分析」——二创的提示词由分析结论构成
          </span>
          <span v-else class="sh-muted" style="margin-left: 8px">
            {{ active.rewrite ? '已生成，可重跑' : '点按钮才会调用，不点不花钱' }}
          </span>
        </div>
        <template v-if="active.rewrite">
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="状态">
              <el-tag :type="statusType(active.rewrite.status)" size="small">
                {{ statusLabel(active.rewrite.status) }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="需人工核实">
              {{ active.rewrite.needs_verification ? '是' : '否' }}
            </el-descriptions-item>
            <el-descriptions-item label="风险标记">
              <!-- Rendered as wrapped lines, not el-tag: el-tag is white-space:nowrap,
                   so these long Chinese risk sentences demanded a huge value column and
                   squeezed the label column down to one character per line. -->
              <template v-if="active.rewrite.risk_flags?.length">
                <div
                  v-for="flag in active.rewrite.risk_flags"
                  :key="flag"
                  style="white-space: normal; word-break: break-word; line-height: 1.6"
                >
                  · {{ flag }}
                </div>
              </template>
              <span v-else class="sh-muted">无</span>
            </el-descriptions-item>
          </el-descriptions>
          <div style="margin-top: 12px">
            <el-button type="primary" size="small" @click="router.push(`/rewrites/${active.id}`)">
              查看三平台文案
            </el-button>
          </div>
        </template>
        <el-empty v-else description="尚未二创" :image-size="70" />

        <!-- 图片二创：不必先有文案，任何有素材的条目都能直接生成 -->
        <el-divider content-position="left">图片</el-divider>
        <ImageGenPanel :hot-content-id="active.id" :default-caption="active.title" compact />

        <!-- 推送到 QQ：先预览，确认后再发 -->
        <el-divider content-position="left">推送到 QQ</el-divider>
        <el-button :loading="pushLoading" @click="previewPush">
          <el-icon><Promotion /></el-icon>&nbsp;预览并发送到 QQ
        </el-button>
        <div class="sh-muted" style="margin-top: 6px">
          分两条：二创的图文一条、原帖的图文一条。**先预览**，确认后才真的发送。
          图片会自动把 webp 转成 jpg（QQ 图片只支持 png/jpg）。
        </div>
      </template>
    </el-drawer>

    <!-- Search for a topic of your own choosing (billed). -->
    <el-drawer v-model="searchVisible" title="搜索话题" size="480px">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="与热搜榜单的区别"
        description="榜单返回的是词条（没有图片和视频）；搜索返回真实帖子，带图片、作者和互动数据。每个平台一次计费请求。"
        style="margin-bottom: 16px"
      />
      <el-form label-width="90px">
        <el-form-item label="关键词">
          <el-input
            v-model="searchForm.keyword"
            placeholder="例如：露营装备、考研英语、减肥餐"
            maxlength="64"
            show-word-limit
            @keyup.enter="askToSearch"
          />
        </el-form-item>
        <el-form-item label="平台">
          <el-checkbox-group v-model="searchForm.platforms">
            <el-checkbox v-for="platform in PLATFORMS" :key="platform.value" :value="platform.value">
              {{ platform.label }}
            </el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="每平台条数">
          <el-input-number v-model="searchForm.limit" :min="1" :max="50" />
          <div class="sh-muted" style="margin-top: 4px">
            条数不影响费用（1 个平台 = 1 次调用），只影响入库量与图片下载量。
          </div>
        </el-form-item>
      </el-form>
      <el-button type="primary" @click="askToSearch">
        <el-icon><Search /></el-icon>&nbsp;开始搜索
      </el-button>
      <div class="sh-muted" style="margin-top: 8px">
        会先弹出费用确认，确认后才真正调用。
      </div>
    </el-drawer>

    <CostConfirmDialog
      v-model="searchDialogVisible"
      title="确认搜索（计费）"
      :description="`按关键词「${searchForm.keyword}」搜索 ${billedCallCount()} 个平台，结果会入库并去重。`"
      :cost="searchCost()"
      confirm-text="确认搜索"
      :loading="searching"
      @confirm="confirmSearch"
    />

    <CostConfirmDialog
      v-model="watchDialogVisible"
      title="确认运行领域搜索（计费）"
      :description="`按设置的领域词批量搜索：${watchInfo.keywords.join('、')}。结果会入库、去重并下载图片。`"
      :cost="`${watchInfo.keywords.length} 个关键词 × ${watchInfo.platforms.length} 个平台 = ${watchInfo.calls} 次 TikHub 计费调用（每次约 $0.0078）。关键词可在「设置 → 定时领域搜索」修改。`"
      confirm-text="确认运行"
      :loading="watchRunning"
      @confirm="confirmWatch"
    />

    <CostConfirmDialog
      v-model="batchAnalyzeDialogVisible"
      title="确认运行 AI 分析（计费）"
      :description="`将分析 ${pending?.would_be_sent ?? 0} 条内容（按领域相关性排序），选出值得二创的条目。`"
      :cost="`消耗 DeepSeek token，预计约 ¥${pending?.estimated_cny ?? 0}。只分析不采集——采集是另一笔费用。`"
      confirm-text="确认分析"
      :loading="batchAnalyzing"
      @confirm="confirmAnalyze"
    />

    <CostConfirmDialog
      v-model="analyzeDialogVisible"
      title="确认分析这一条（计费）"
      :description="`将对「${activeTitle}」单独跑一次 DeepSeek 分析。会绕过领域过滤与候选上限——是你指定的，就分析它。`"
      cost="消耗 DeepSeek token，实测约 ¥0.003（一条）。只处理这一条，不会顺带分析其他内容。"
      confirm-text="确认分析"
      :loading="analyzeRunning"
      @confirm="confirmAnalyzeItem"
    />

    <CostConfirmDialog
      v-model="rewriteDialogVisible"
      title="确认二创这一条（计费）"
      :description="`将为「${activeTitle}」生成小红书 / 微博 / 抖音三平台草稿。即使该条没有被批量流程「入选」，也会照做。`"
      cost="消耗 DeepSeek token，实测约 ¥0.017（三个平台一次生成）。生成结果是草稿，需要人工审核，系统不会自动发布。"
      confirm-text="确认二创"
      :loading="rewriteRunning"
      @confirm="confirmRewriteItem"
    />

    <!-- 推送到 QQ：预览对话框。真的发送是对话框里的第二个按钮。 -->
    <el-dialog v-model="pushDialogVisible" title="推送到 QQ 群" width="720px">
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="发送前请确认内容"
        :description="`将发送 ${pushMessageCount} 条 QQ 消息（每条含 1 条文本，图片各占 1 条 —— 富媒体接口一次只能带一张图）。点「确认发送」才会真的发出。`"
      />
      <div v-for="(part, index) in pushPreview?.parts ?? []" :key="index" style="margin-top: 14px">
        <div style="font-weight: 600; margin-bottom: 4px">
          第 {{ index + 1 }} 条：{{ part.label }}
          <span class="sh-muted">
            （文本 {{ part.text.length }} 字，图片 {{ part.image_count }} 张）
          </span>
        </div>
        <pre class="sh-draft" style="white-space: pre-wrap; max-height: 220px; overflow: auto">{{ part.text }}</pre>
        <div v-if="part.image_paths?.length" style="display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px">
          <el-image
            v-for="path in part.image_paths"
            :key="path"
            :src="`/${path}`"
            :preview-src-list="part.image_paths.map((item) => `/${item}`)"
            preview-teleported
            fit="cover"
            style="width: 72px; height: 72px; border-radius: 4px"
          />
        </div>
        <div v-for="skipped in part.skipped ?? []" :key="skipped" class="sh-muted">
          [跳过] {{ skipped }}
        </div>
      </div>
      <template #footer>
        <el-button @click="pushDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="pushing" @click="confirmPush">确认发送到 QQ</el-button>
      </template>
    </el-dialog>
  </div>
</template>
