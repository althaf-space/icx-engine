const { listPosts } = require("../services");

async function getPosts(request, reply) {
  return listPosts();
}

async function postsPlugin(fastify, opts) {
  fastify.get("/", getPosts);
}

module.exports = postsPlugin;
