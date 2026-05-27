import React from "react";
import type { Post } from "../lib/posts";

export function PostCard({ post }: { post: Post }) {
  return (
    <li>
      <h3>{post.title}</h3>
      <p>{post.body}</p>
    </li>
  );
}
