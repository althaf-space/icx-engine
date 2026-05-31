import React from "react";
import { PostCard } from "../components/PostCard";
import { usePosts } from "../hooks/usePosts";

export function HomePage() {
  const posts = usePosts();
  return (
    <section>
      <h1>Latest</h1>
      {posts.map((post) => (
        <PostCard key={post.id} post={post} />
      ))}
    </section>
  );
}
