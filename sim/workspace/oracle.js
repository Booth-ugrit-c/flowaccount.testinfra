const fs = require("node:fs");
const path = require("node:path");

exports.name = (project) => {
  const file = path.join(__dirname, project, "index.js");
  const match = /name = \(\) => "([^"]+)"/.exec(fs.readFileSync(file, "utf8"));
  if (!match) {
    throw new Error(`no name() literal in ${file}`);
  }
  return match[1];
};
