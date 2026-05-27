import type { PageServerLoad } from "./$types";
import { fetchPosts } from "$lib/services/api";

export const load: PageServerLoad = async () => {
  const posts = await fetchPosts();
  return { posts };
};
