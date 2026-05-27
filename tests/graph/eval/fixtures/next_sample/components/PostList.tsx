import React from "react";
import { listPosts, Post } from "../lib/posts";
import { PostCard } from "./PostCard";

export async function PostList() {
  const posts: Post[] = await listPosts();
  return (
    <ul>
      {posts.map((post) => (
        <PostCard key={post.id} post={post} />
      ))}
    </ul>
  );
}
