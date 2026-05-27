const express = require("express");
const { listPosts } = require("../services");

const router = express.Router();

function getPosts(req, res) {
  res.json(listPosts());
}

router.get("/", getPosts);

module.exports = { router };
