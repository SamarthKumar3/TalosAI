import { cpSync, existsSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const directory = resolve(".next/standalone");
const server = resolve(directory, "server.js");
if (!existsSync(server)) {
  console.error("Build the Web UI first with npm run build.");
  process.exit(1);
}
cpSync(".next/static", resolve(directory, ".next/static"), { recursive: true });
if (existsSync("public")) cpSync("public", resolve(directory, "public"), { recursive: true });
process.env.HOSTNAME = process.env.TALOS_WEB_HOST ?? "127.0.0.1";
process.env.PORT ??= "3000";
await import(pathToFileURL(server).href);
