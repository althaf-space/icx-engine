import { Hono } from "hono";
import { getUser, listUsers } from "../services/userService";

export const usersApp = new Hono();

usersApp.get("/", (c) => {
  return c.json(listUsers());
});

usersApp.get("/:id", (c) => {
  const user = getUser(Number(c.req.param("id")));
  return user ? c.json(user) : c.notFound();
});
