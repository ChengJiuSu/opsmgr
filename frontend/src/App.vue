<template>
  <router-view />
  <!-- 敏感操作二次验证：全局一个框，由响应拦截器按需唤起 -->
  <SudoDialog />
</template>

<script setup>
import SudoDialog from './components/SudoDialog.vue'
</script>
