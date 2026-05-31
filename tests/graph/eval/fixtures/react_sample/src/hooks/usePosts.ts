import { useState, useEffect } from "react";
import { Post } from "../components/PostCard";

export function usePosts(): Post[] {
  const [posts, setPosts] = useState<Post[]>([]);
  useEffect(() => {
    setPosts([{ id: 1, title: "hello", body: "world" }]);
  }, []);
  return posts;
}
