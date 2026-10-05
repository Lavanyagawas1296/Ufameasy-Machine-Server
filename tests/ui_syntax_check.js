const fs = require("fs");

const html = fs.readFileSync("ui/index.html", "utf8");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((match) => match[1]);
if (scripts.length !== 1) throw new Error("Expected exactly one inline dashboard script");
new Function(scripts[0]);
