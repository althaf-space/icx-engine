<template>
  <div class="post-card">
    <h3>{{ post.title }}</h3>
  </div>
</template>

<script>
export default {
  name: 'PostCard',
  props: ['user'],
  data() {
    return {
      posts: [],
    };
  },
  methods: {
    loadPosts() {
      this.posts = [];
    },
  },
  mounted() {
    this.loadPosts();
  },
};
</script>
