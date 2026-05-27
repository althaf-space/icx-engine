import React from "react";

export interface Post {
  id: number;
  title: string;
  body: string;
}

export function PostCard({ post }: { post: Post }) {
  return (
    <article>
      <h2>{post.title}</h2>
      <p>{post.body}</p>
    </article>
  );
}
