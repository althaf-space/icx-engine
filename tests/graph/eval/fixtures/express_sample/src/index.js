const express = require("express");
const { router: usersRouter } = require("./routes/users");
const { router: postsRouter } = require("./routes/posts");
const { requestLogger } = require("./middleware");

const app = express();
app.use(express.json());
app.use(requestLogger);
app.use("/users", usersRouter);
app.use("/posts", postsRouter);

app.listen(3000);
