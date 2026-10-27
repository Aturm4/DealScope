import {copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync} from "node:fs";
import {resolve, dirname} from "node:path";
import {fileURLToPath} from "node:url";

const root = dirname(fileURLToPath(import.meta.url));
const settings = JSON.parse(readFileSync(resolve(root, "site.json"), "utf8"));
if (!/^https:\/\/[a-z][a-z0-9-]*\.[a-z][a-z0-9-]*\.chatgpt\.site$/.test(settings.url)
    || !Number.isInteger(settings.port) || settings.port < 1024 || settings.port > 65535) throw new Error("Invalid Site connection settings");
const files = ["index.html", "app.js", "app.css", "icon.svg"];
mkdirSync(resolve(root, "assets"), {recursive: true});
const shared = resolve(root, "../dealradar/web");
if (existsSync(shared)) for (const name of files) copyFileSync(resolve(shared, name), resolve(root, "assets", name));
mkdirSync(resolve(root, "dist"), {recursive: true});
for (const name of files) copyFileSync(resolve(root, "assets", name), resolve(root, "dist", name));
const csp = `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self' http://127.0.0.1:${settings.port}; img-src 'self'; base-uri 'none'; form-action 'none'; object-src 'none'`;
const html = readFileSync(resolve(root, "assets/index.html"), "utf8")
  .replace('<meta charset="utf-8">', `<meta charset="utf-8">\n  <meta http-equiv="Content-Security-Policy" content="${csp}">\n  <meta name="referrer" content="no-referrer">`)
  .replace('<script src="/app.js" defer>', '<link rel="stylesheet" href="/bridge.css">\n  <script src="/bridge.js" defer></script>\n  <script src="/app.js" defer>');
if (!html.includes('src="/bridge.js"')) throw new Error("Shared HTML contract changed");
writeFileSync(resolve(root, "dist/index.html"), html);
writeFileSync(resolve(root, "dist/bridge.js"), readFileSync(resolve(root, "bridge.js"), "utf8").replace("__RADAR_PORT__", String(settings.port)));
copyFileSync(resolve(root, "bridge.css"), resolve(root, "dist/bridge.css"));
console.log("Sites build ready: 6 static assets; no runtime data or Telegram credentials.");
