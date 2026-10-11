"use strict";
// Narrow adapter for @next/eslint-plugin-next getRootDirs. No third-party pattern parser.
// CommonJS is required by the upstream lint plugin.
// eslint-disable-next-line @typescript-eslint/no-require-imports
const fs = require("node:fs");
function globSync(pattern, options = {}) {
  if (typeof pattern !== "string" || options.onlyDirectories !== true || Object.keys(options).some(key => key !== "onlyDirectories")) {
    throw new TypeError("The Next root-directory adapter supports only globSync(pattern, { onlyDirectories: true })");
  }
  return [...fs.globSync(pattern)].filter(path => fs.statSync(path).isDirectory());
}
module.exports = { globSync };
