<script setup lang="ts">
import UserAvatar from "./UserAvatar.vue";

defineProps<{ title: string; authorId: number }>();
</script>

<template>
  <div class="post-card">
    <UserAvatar :userId="authorId" />
    <h3>{{ title }}</h3>
  </div>
</template>
