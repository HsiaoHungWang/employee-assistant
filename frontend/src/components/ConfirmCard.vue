<script setup>
// 寫入類工具的確認關卡：內容由後端程式算好（天數、餘額），不是模型說了算
defineProps({ action: Object, state: String })
defineEmits(['decide'])
</script>

<template>
  <div class="confirm" :class="state">
    <div class="confirm-title">{{ action.title }}</div>
    <dl>
      <template v-for="d in action.details" :key="d.label">
        <dt>{{ d.label }}</dt>
        <dd>{{ d.value }}</dd>
      </template>
    </dl>
    <p v-if="action.warning" class="warning">⚠ {{ action.warning }}</p>
    <div v-if="state === 'pending'" class="confirm-actions">
      <button class="primary" @click="$emit('decide', true)">確認送出</button>
      <button @click="$emit('decide', false)">取消</button>
    </div>
    <p v-else class="muted">
      {{ { approved: '✓ 已確認', rejected: '已取消', expired: '已失效（你送出了新訊息）' }[state] }}
    </p>
  </div>
</template>
