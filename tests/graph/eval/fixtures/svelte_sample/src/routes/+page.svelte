<script lang="ts">
  import PostCard from "$lib/components/PostCard.svelte";
  import type { PageData } from "./$types";

  export let data: PageData;
</script>

<h1>Posts</h1>
{#each data.posts as post}
  <PostCard title={post.title} author={post.author} />
{/each}
