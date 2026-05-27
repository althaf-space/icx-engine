import type { LoaderFunctionArgs } from "@remix-run/node";
import { useLoaderData } from "@remix-run/react";
import { getPostById } from "../models/post.server";
import { getUserById } from "../models/user.server";

export async function loader({ params }: LoaderFunctionArgs) {
  const post = getPostById(Number(params.id));
  if (!post) throw new Response("Not Found", { status: 404 });
  const author = getUserById(post.authorId);
  return { post, author };
}

export default function PostDetail() {
  const { post, author } = useLoaderData<typeof loader>();
  return (
    <article>
      <h1>{post.title}</h1>
      <p>By {author?.name}</p>
    </article>
  );
}
