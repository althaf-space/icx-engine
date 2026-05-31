async function listUsers() {
  return [{ id: 1, name: "alice" }];
}

async function listPosts() {
  return [{ id: 1, title: "hello" }];
}

module.exports = { listUsers, listPosts };
