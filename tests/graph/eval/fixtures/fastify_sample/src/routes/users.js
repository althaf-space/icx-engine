const { listUsers } = require("../services");

async function getUsers(request, reply) {
  return listUsers();
}

async function usersPlugin(fastify, opts) {
  fastify.get("/", getUsers);
}

module.exports = usersPlugin;
