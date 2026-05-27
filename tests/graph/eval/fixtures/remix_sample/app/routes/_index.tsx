import type { LoaderFunctionArgs } from "@remix-run/node";
import { useLoaderData } from "@remix-run/react";
import { listPosts } from "../models/post.server";
import PostCard from "../components/PostCard";

export async function loader({ request }: LoaderFunctionArgs) {
  return { posts: listPosts() };
}

export default function Index() {
  const { posts } = useLoaderData<typeof loader>();
  return (
    <div>
      {posts.map((p) => (
        <PostCard key={p.id} title={p.title} author="user" />
      ))}
    </div>
  );
}
