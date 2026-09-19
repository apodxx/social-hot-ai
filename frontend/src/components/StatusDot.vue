<script setup>
/** A tiny status dot + label, used for the backend components in the header. */
const props = defineProps({
  status: { type: String, default: '' },
  label: { type: String, default: '' },
  detail: { type: String, default: '' },
})

const COLOR = {
  connected: '#67c23a',
  error: '#f56c6c',
  not_configured: '#e6a23c',
  skipped: '#909399',
}
</script>

<template>
  <el-tooltip :content="detail || label" placement="bottom" :disabled="!detail">
    <span style="display: inline-flex; align-items: center; gap: 6px">
      <span
        :style="{
          width: '8px',
          height: '8px',
          borderRadius: '50%',
          background: COLOR[status] || '#909399',
          display: 'inline-block',
        }"
      />
      <span style="font-size: 13px">{{ label }}</span>
    </span>
  </el-tooltip>
</template>
