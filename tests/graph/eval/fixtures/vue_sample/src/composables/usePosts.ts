import { ref } from "vue";
import { usePostsStore } from "../stores/posts";

export function usePosts() {
  const store = usePostsStore();
  const loading = ref(false);

  async function refresh() {
    loading.value = true;
    await store.fetchPosts();
    loading.value = false;
  }

  return { posts: store.posts, loading, refresh };
}
