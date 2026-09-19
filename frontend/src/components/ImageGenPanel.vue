<script setup>
/**
 * 图片二创面板：给某一条热点生成配图。
 *
 * 抽成组件是因为它出现在两个地方——热点列表的详情抽屉、二创详情页。复制两份的话两边
 * 一定会走偏，而且这块正好是本项目最贵的操作（每张约 $0.034–0.039），价格显示必须
 * 只有一处实现。
 *
 * 三条与后端一致的约束在这里也成立：
 * - **不点不花钱**：进入页面只调免费的 `/api/image/estimate`，绝不自动生成；
 * - **1 张 1K 是策略**：没有张数/尺寸选项（后端也没有这些参数）；
 * - **价格含输入图**：显示的是 `estimated_total_usd`——只报输出价会少报约 16%。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import CostConfirmDialog from '@/components/CostConfirmDialog.vue'
import { api } from '@/api/client'
import { formatDateTime } from '@/api/format'

const props = defineProps({
  hotContentId: { type: Number, required: true },
  /** Used as the image's co-text when the item has no rewrite title. */
  defaultCaption: { type: String, default: '' },
  /** Narrow container (a drawer) — smaller gallery and tighter copy. */
  compact: { type: Boolean, default: false },
})

const emit = defineEmits(['generated'])

const estimate = ref(null)
const generations = ref([])
const loading = ref(false)
const confirmVisible = ref(false)
const generating = ref(false)
const overlayText = ref('')
const goal = ref('')

const price = computed(() => {
  const value = estimate.value
  if (!value) return '未知'
  const total = value.estimated_total_usd ?? value.price_usd_per_image
  return `$${Number(total).toFixed(5)}（≈¥${value.estimated_total_cny ?? value.price_cny_per_image}）`
})

const referenceCount = computed(() => estimate.value?.reference_images_available ?? 0)
const canGenerate = computed(
  () => Boolean(estimate.value?.configured) && referenceCount.value > 0,
)

const totalSpent = computed(() =>
  generations.value.reduce((sum, item) => sum + (item.estimated_usd || 0), 0),
)

async function load() {
  if (!props.hotContentId) return
  loading.value = true
  try {
    const [estimateResponse, generationResponse] = await Promise.all([
      api.imageEstimate(props.hotContentId),
      api.imageGenerations({ hot_content_id: props.hotContentId, limit: 6 }),
    ])
    estimate.value = estimateResponse
    generations.value = generationResponse.items ?? []
  } catch {
    estimate.value = null
    generations.value = []
  } finally {
    loading.value = false
  }
}

function ask() {
  if (!canGenerate.value) {
    ElMessage.warning(
      estimate.value?.configured
        ? '这条没有已下载的本地图片素材，不能作为参考图。请先用「搜索话题」采集并下载图片。'
        : '图片二创未配置：请到「设置 → 图片二创」填入 DashScope API Key。',
    )
    return
  }
  confirmVisible.value = true
}

async function confirmGenerate() {
  generating.value = true
  try {
    const response = await api.generateImage({
      hot_content_id: props.hotContentId,
      goal: goal.value,
      caption: props.defaultCaption,
      overlay_text: overlayText.value,
      keep_subject: true,
    })
    ElMessage.success(
      `已生成 ${response.local_paths?.length ?? 0} 张，$ ${response.estimated_usd}（¥${response.estimated_cny}）`,
    )
    confirmVisible.value = false
    await load()
    emit('generated', response)
  } catch {
    // Reported by the interceptor (422 = 没有素材, 502 = 供应商失败).
  } finally {
    generating.value = false
  }
}

// The panel is reused across rows, so it must reload when the row changes.
watch(() => props.hotContentId, load, { immediate: true })
</script>

<template>
  <el-card v-loading="loading" shadow="never" :class="{ 'sh-card': !compact }">
    <template #header>
      <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap">
        <span>图片二创（千问图像）</span>
        <el-tag type="danger" size="small" effect="dark">每次 1 张 · 1K</el-tag>
        <div class="sh-spacer" />
        <span class="sh-muted">{{ price }}</span>
      </div>
    </template>

    <el-alert
      v-if="estimate && !estimate.configured"
      type="warning"
      :closable="false"
      show-icon
      title="尚未配置图片生成"
      description="请到「设置 → 图片二创」填入 DashScope API Key 与工作空间 Endpoint。"
    />
    <el-alert
      v-else-if="estimate && !canGenerate"
      type="info"
      :closable="false"
      show-icon
      title="这条没有可用的参考图"
      description="图片模型只能用已下载到本地素材库的图——平台原链接会过期，供应商也访问不到本机。请先用「搜索话题」采集并下载图片。"
    />

    <template v-if="estimate?.configured">
      <el-form label-width="80px" style="margin-top: 8px">
        <el-form-item label="画面方向">
          <el-input
            v-model="goal"
            placeholder="可选：这张图要表达什么（留空按标题自动生成）"
            maxlength="120"
          />
        </el-form-item>
        <el-form-item label="图上文字">
          <el-input
            v-model="overlayText"
            placeholder="可选：要排在画面上的字，例如「真香警告」"
            maxlength="30"
          />
        </el-form-item>
      </el-form>

      <el-button
        class="sh-generate-image"
        type="primary"
        :disabled="!canGenerate"
        :loading="generating"
        @click="ask"
      >
        <el-icon><Picture /></el-icon>&nbsp;生成图片
      </el-button>
      <span class="sh-muted" style="margin-left: 8px">
        参考图 {{ referenceCount }} 张（已下载的素材）· 结果存到 media/generated/
      </span>

      <div v-if="generations.length" style="margin-top: 14px">
        <div class="sh-muted" style="margin-bottom: 6px">
          已生成 {{ generations.length }} 次，累计约 ${{ totalSpent.toFixed(4) }}
        </div>
        <div v-for="generation in generations" :key="generation.id" style="margin-bottom: 12px">
          <div style="display: flex; gap: 8px; flex-wrap: wrap">
            <el-image
              v-for="(path, index) in generation.local_paths"
              :key="path"
              :src="`/${path}`"
              :preview-src-list="generation.local_paths.map((item) => `/${item}`)"
              :initial-index="index"
              preview-teleported
              fit="cover"
              :style="{
                width: compact ? '96px' : '120px',
                height: compact ? '96px' : '120px',
                borderRadius: '6px',
              }"
            />
          </div>
          <div class="sh-muted" style="margin-top: 4px">
            #{{ generation.id }} · {{ generation.status }} · ${{ generation.estimated_usd }}
            · {{ formatDateTime(generation.created_at) }}
            <span v-if="generation.error"> · {{ generation.error }}</span>
          </div>
        </div>
      </div>
    </template>

    <CostConfirmDialog
      v-model="confirmVisible"
      title="确认生成图片（按张计费）"
      :description="`以这条的 ${referenceCount} 张已下载素材为参考，按文案方向重新生成一张新图。这是本项目最贵的操作。`"
      :cost="`每次固定 1 张、1K 档：${price}（含 ${referenceCount} 张参考图的输入费）。按输出张数与像素档位计费，不按 token —— 约等于一次文字二创的 15 倍。张数与尺寸由策略锁定。生成结果会立即下载到本地素材库。`"
      confirm-text="确认生成"
      :loading="generating"
      @confirm="confirmGenerate"
    />
  </el-card>
</template>
