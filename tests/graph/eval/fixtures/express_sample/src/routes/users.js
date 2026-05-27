const express = require("express");
const { listUsers } = require("../services");

const router = express.Router();

function getUsers(req, res) {
  res.json(listUsers());
}

router.get("/", getUsers);

module.exports = { router };
