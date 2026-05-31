import { Hono } from "hono";
import { usersApp } from "./routes/users";
import { postsApp } from "./routes/posts";
import { logger } from "./middleware/logger";
import { authMiddleware } from "./middleware/auth";

const app = new Hono();
app.use("*", logger);
app.use("/api/*", authMiddleware);
app.route("/api/users", usersApp);
app.route("/api/posts", postsApp);

export default app;
