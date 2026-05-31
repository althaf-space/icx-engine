const fastify = require("fastify")({ logger: true });
const usersPlugin = require("./routes/users");
const postsPlugin = require("./routes/posts");

fastify.register(usersPlugin, { prefix: "/users" });
fastify.register(postsPlugin, { prefix: "/posts" });

fastify.listen({ port: 3000 });
