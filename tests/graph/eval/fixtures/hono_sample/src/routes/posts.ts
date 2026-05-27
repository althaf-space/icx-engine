import { Hono } from "hono";
import { listPosts, createPost } from "../services/postService";

export const postsApp = new Hono();

postsApp.get("/", (c) => {
  return c.json(listPosts());
});

postsApp.post("/", async (c) => {
  const body = await c.req.json();
  return c.json(createPost(body.title, body.authorId), 201);
});
