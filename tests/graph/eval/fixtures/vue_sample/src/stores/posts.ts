import { defineStore } from "pinia";
import { ref } from "vue";
import { fetchAllPosts } from "../services/api";

export const usePostsStore = defineStore("posts", () => {
  const posts = ref<{ id: number; title: string }[]>([]);

  async function fetchPosts() {
    posts.value = await fetchAllPosts();
  }

  return { posts, fetchPosts };
});
