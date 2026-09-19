<script setup>
/**
 * The confirmation gate in front of every action that costs money.
 *
 * The standing rule for this project is that spend is never implicit: a button that
 * calls TikHub or DeepSeek must state what it will cost and wait for a deliberate
 * second action. Free actions (reading stored rows) never open this dialog.
 */
import { computed } from 'vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  title: { type: String, default: '确认执行' },
  /** What the action does, in one or two sentences. */
  description: { type: String, default: '' },
  /** The concrete spend warning, e.g. "将调用 TikHub 3 次（计费）". */
  cost: { type: String, default: '' },
  /** False for a side-effecting action that costs nothing (e.g. a test message). */
  billed: { type: Boolean, default: true },
  confirmText: { type: String, default: '确认执行' },
  loading: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue', 'confirm'])

const visible = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value),
})

/**
 * 确认后**立即关闭**对话框。
 *
 * 之前只 emit 事件、等父组件在成功回调里把 visible 设回 false——于是**请求一失败，
 * 对话框就永久留在屏幕上**（运营方反馈"点了确认框一直存在"）。由对话框自己负责关闭，
 * 成功与失败都关，失败信息照旧用 ElMessage 提示，行为才可预期。
 */
function confirm() {
  visible.value = false
  emit('confirm')
}
</script>

<template>
  <el-dialog v-model="visible" :title="title" width="520px" append-to-body>
    <p v-if="description" style="margin-top: 0; line-height: 1.7">{{ description }}</p>
    <el-alert
      v-if="cost"
      :type="billed ? 'warning' : 'info'"
      :closable="false"
      show-icon
      :title="billed ? '会产生费用' : '不会产生费用'"
      :description="cost"
    />
    <!-- 可选的额外选项（如「额外要求」「配图张数」）。不传插槽时什么都不渲染，
         所以现有调用点不需要改动。 -->
    <slot />
    <template #footer>
      <el-button :disabled="loading" @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="loading" @click="confirm">
        {{ confirmText }}
      </el-button>
    </template>
  </el-dialog>
</template>
