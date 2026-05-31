import type { MiddlewareHandler } from "hono";

export const authMiddleware: MiddlewareHandler = async (c, next) => {
  const token = c.req.header("Authorization");
  if (!token) {
    return c.json({ error: "unauthorized" }, 401);
  }
  await next();
};
