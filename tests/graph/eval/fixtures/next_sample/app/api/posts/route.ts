import { NextResponse } from "next/server";
import { listPosts } from "../../../lib/posts";

export async function GET() {
  const posts = await listPosts();
  return NextResponse.json(posts);
}
