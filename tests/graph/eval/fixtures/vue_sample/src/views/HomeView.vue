<script setup lang="ts">
import { usePosts } from "../composables/usePosts";
import PostCard from "../components/PostCard.vue";

const { posts, refresh } = usePosts();
refresh();
</script>

<template>
  <div>
    <h1>Posts</h1>
    <PostCard v-for="p in posts" :key="p.id" :title="p.title" :authorId="1" />
  </div>
</template>
