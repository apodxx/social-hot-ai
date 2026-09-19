<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { api } from '@/api/client'
import ImageGenPanel from '@/components/ImageGenPanel.vue'
import {
  copyText,
  formatDateTime,
  formatNumber,
  imageSrc,
  platformLabel,
  platformType,
  statusLabel,
  statusType,
} from '@/api/format'

const route = useRoute()
const router = useRouter()

const item = ref(null)
const loading = ref(false)
const error = ref('')
const activeTab = ref('xiaohongshu')

const xhs = computed(() => item.value?.xiaohongshu ?? {})
const weibo = computed(() => item.value?.weibo ?? {})
const douyin = computed(() => item.value?.douyin ?? {})

/** The 图文 layout plan, when the source item had pictures. */
const layout = computed(() => item.value?.layout ?? {})
const layoutSlots = computed(() => layout.value?.slots ?? [])
/** Images of the source item — the list the layout plan indexes into. */
const images = computed(() => item.value?.source?.images ?? [])

function slotFor(index) {
  return layoutSlots.value.find((slot) => Number(slot.index) === Number(index)) ?? null
}

// 图片二创现在由 <ImageGenPanel /> 负责（同一个组件也用在热点列表的详情抽屉里），
// 所以这里不再持有价格、估算和生成状态——重复一份必然会两边走偏。

/** The exact text an operator would paste into each platform. */
const drafts = computed(() => {
  const value = item.value
  if (!value) return {}
  const hashtags = (tags) => (tags || []).map((tag) => (tag.startsWith('#') ? tag : `#${tag}#`)).join(' ')
  return {
    xiaohongshu: [
      value.xiaohongshu?.title,
      value.xiaohongshu?.content,
      value.xiaohongshu?.ending,
      hashtags(value.xiaohongshu?.hashtags),
    ]
      .filter(Boolean)
      .join('\n\n'),
    weibo: [
      value.weibo?.opening,
      value.weibo?.content,
      hashtags(value.weibo?.hashtags),
    ]
      .filter(Boolean)
      .join('\n\n'),
    douyin: [
      value.douyin?.hook ? `【开场钩子】${value.douyin.hook}` : '',
      value.douyin?.script,
      (value.douyin?.scenes || []).length
        ? `【分镜建议】\n${(value.douyin.scenes || []).map((scene, index) => `${index + 1}. ${scene}`).join('\n')}`
        : '',
      value.douyin?.subtitles ? `【字幕】${value.douyin.subtitles}` : '',
      value.douyin?.cta ? `【结尾引导】${value.douyin.cta}` : '',
    ]
      .filter(Boolean)
      .join('\n\n'),
  }
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const response = await api.rewrite(route.params.id)
    item.value = response.item
  } catch (requestError) {
    error.value = requestError?.response?.data?.detail || '未找到该二创内容'
  } finally {
    loading.value = false
  }
}

async function copy(platform) {
  const text = drafts.value[platform]
  if (!text) {
    ElMessage.warning('该平台暂无内容')
    return
  }
  if (await copyText(text)) ElMessage.success(`已复制${platformLabel(platform)}文案`)
  else ElMessage.warning('浏览器拒绝了剪贴板访问，请手动选中复制')
}

onMounted(load)
</script>

<template>
  <div class="sh-page">
    <div class="sh-page-header">
      <div>
        <h2 class="sh-page-title">二创详情</h2>
        <p class="sh-page-subtitle">复制文案后请自行到对应平台发布，系统不会代为发布</p>
      </div>
      <el-button @click="router.push('/rewrites')">
        <el-icon><ArrowLeft /></el-icon>&nbsp;返回列表
      </el-button>
    </div>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error" />

    <template v-else>
      <el-alert
        v-if="item?.needs_verification"
        class="sh-card"
        type="warning"
        :closable="false"
        show-icon
        title="内容涉及需要核实的信息，发布前请自行确认"
        :description="item?.verification_note || ''"
      />
      <el-alert
        v-if="item?.risk_flags?.length"
        class="sh-card"
        type="error"
        :closable="false"
        show-icon
        title="存在风险标记"
        :description="`${item.risk_flags.join('、')} —— 发布前请重点检查这些表述`"
      />

      <el-card v-loading="loading" shadow="never" class="sh-card">
        <template #header>
          <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
            <span>来源热点</span>
            <el-tag v-if="item" :type="statusType(item.status)" size="small">
              {{ statusLabel(item.status) }}
            </el-tag>
            <div class="sh-spacer" />
            <span v-if="item?.copy_similarity !== null && item?.copy_similarity !== undefined" class="sh-muted">
              机械复制相似度 {{ (item.copy_similarity * 100).toFixed(1) }}%
            </span>
          </div>
        </template>
        <template v-if="item">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="标题">
              {{ item.source?.title || `#${item.hot_content_id}` }}
            </el-descriptions-item>
            <el-descriptions-item label="来源平台">
              <el-tag :type="platformType(item.source?.platform)" size="small">
                {{ platformLabel(item.source?.platform) }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="原始作者">
              {{ item.source?.author || '未知' }}
            </el-descriptions-item>
            <el-descriptions-item label="原始热度">
              {{ formatNumber(item.source?.hot_value) }}
            </el-descriptions-item>
            <el-descriptions-item label="话题摘要" :span="2">
              {{ item.summary || '-' }}
            </el-descriptions-item>
            <el-descriptions-item label="为什么火" :span="2">
              {{ item.why_hot || '-' }}
            </el-descriptions-item>
            <el-descriptions-item label="切入角度" :span="2">
              {{ item.angle || '-' }}
            </el-descriptions-item>
          </el-descriptions>
          <div style="margin-top: 10px">
            <el-link
              v-if="item.source?.url"
              :href="item.source.url"
              target="_blank"
              type="primary"
            >
              查看原内容
            </el-link>
            <span class="sh-muted" style="margin-left: 10px">
              模型 {{ item.model }} · 尝试 {{ item.attempts }} 次 · token
              {{ item.tokens?.prompt ?? 0 }}/{{ item.tokens?.completion ?? 0 }} · 更新于
              {{ formatDateTime(item.updated_at) }}
            </span>
          </div>
        </template>
      </el-card>

      <ImageGenPanel
        :hot-content-id="item.hot_content_id"
        :default-caption="sourceTitle"
      />
      <el-card v-if="item && Object.keys(layout).length" shadow="never" class="sh-card">
        <template #header>
          <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
            <span>配图方案（图文排版）</span>
            <el-tag type="info" size="small" effect="plain">
              模型未看到图片内容，仅做结构安排
            </el-tag>
          </div>
        </template>

        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="封面">
            第 {{ Number(layout.cover_index ?? 0) + 1 }} 张
          </el-descriptions-item>
          <el-descriptions-item label="封面大字">
            {{ layout.cover_text || '-' }}
          </el-descriptions-item>
          <el-descriptions-item label="配图思路" :span="2">
            {{ layout.caption_strategy || '-' }}
          </el-descriptions-item>
        </el-descriptions>

        <el-table :data="layoutSlots" size="small" style="margin-top: 12px">
          <el-table-column label="图" width="76">
            <template #default="{ row }">
              <el-image
                v-if="images[row.index]"
                :src="imageSrc(images[row.index])"
                :preview-src-list="images.map(imageSrc)"
                :initial-index="Number(row.index)"
                preview-teleported
                fit="cover"
                style="width: 48px; height: 48px; border-radius: 4px"
              />
              <span v-else class="sh-muted">#{{ Number(row.index) + 1 }}</span>
            </template>
          </el-table-column>
          <el-table-column label="顺序" width="70">
            <template #default="{ row }">第 {{ Number(row.index) + 1 }} 张</template>
          </el-table-column>
          <el-table-column prop="role" label="作用" width="140" />
          <el-table-column prop="overlay" label="图上文字" min-width="140" />
          <el-table-column prop="caption" label="配文" min-width="180" />
        </el-table>

        <el-alert
          v-if="layout.note"
          style="margin-top: 12px"
          type="info"
          :closable="false"
          title="排版提醒"
          :description="layout.note"
        />

        <div v-if="images.length" style="margin-top: 14px">
          <div class="sh-muted" style="margin-bottom: 6px">
            原素材 {{ images.length }} 张（点击可放大）：
          </div>
          <el-image
            v-for="(image, index) in images"
            :key="index"
            :src="imageSrc(image)"
            :preview-src-list="images.map(imageSrc)"
            :initial-index="index"
            preview-teleported
            fit="cover"
            style="width: 84px; height: 84px; margin-right: 8px; border-radius: 4px"
          />
        </div>
      </el-card>

      <el-card v-if="item" shadow="never" class="sh-card">
        <el-tabs v-model="activeTab">
          <el-tab-pane label="小红书" name="xiaohongshu">
            <div class="sh-copy-block">
              <h3 style="margin: 0 0 8px">{{ xhs.title || '（无标题）' }}</h3>
              <div class="sh-draft">{{ xhs.content || '（无正文）' }}</div>
              <div v-if="xhs.ending" class="sh-draft" style="margin-top: 10px">{{ xhs.ending }}</div>
              <div style="margin-top: 10px">
                <el-tag
                  v-for="tag in xhs.hashtags || []"
                  :key="tag"
                  size="small"
                  effect="plain"
                  style="margin-right: 4px"
                >
                  {{ tag }}
                </el-tag>
              </div>
            </div>
            <el-button type="primary" @click="copy('xiaohongshu')">复制小红书文案</el-button>
          </el-tab-pane>

          <el-tab-pane label="微博" name="weibo">
            <div class="sh-copy-block">
              <div v-if="weibo.opening" class="sh-draft" style="font-weight: 600">
                {{ weibo.opening }}
              </div>
              <div class="sh-draft" style="margin-top: 8px">{{ weibo.content || '（无正文）' }}</div>
              <div style="margin-top: 10px">
                <el-tag
                  v-for="tag in weibo.hashtags || []"
                  :key="tag"
                  size="small"
                  effect="plain"
                  style="margin-right: 4px"
                >
                  {{ tag }}
                </el-tag>
              </div>
            </div>
            <el-button type="primary" @click="copy('weibo')">复制微博文案</el-button>
          </el-tab-pane>

          <el-tab-pane label="抖音" name="douyin">
            <div class="sh-copy-block">
              <div class="sh-draft">
                <strong>开场钩子：</strong>{{ douyin.hook || '（无）' }}
              </div>
              <el-divider style="margin: 10px 0" />
              <div class="sh-draft">{{ douyin.script || '（无口播脚本）' }}</div>
              <template v-if="douyin.scenes?.length">
                <el-divider style="margin: 10px 0" />
                <div style="font-weight: 600; margin-bottom: 6px">分镜建议</div>
                <ol style="margin: 0; padding-left: 20px; line-height: 1.8">
                  <li v-for="(scene, index) in douyin.scenes" :key="index">{{ scene }}</li>
                </ol>
              </template>
              <template v-if="douyin.subtitles">
                <el-divider style="margin: 10px 0" />
                <div><strong>字幕：</strong>{{ douyin.subtitles }}</div>
              </template>
              <div v-if="douyin.cta" style="margin-top: 10px">
                <strong>结尾引导：</strong>{{ douyin.cta }}
              </div>
            </div>
            <el-button type="primary" @click="copy('douyin')">复制抖音脚本</el-button>
          </el-tab-pane>
        </el-tabs>
      </el-card>
    </template>
  </div>
</template>
