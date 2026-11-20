import { writeFileSync } from "node:fs";

const raw = process.argv[2];
if (!raw) throw new Error("Pass the HTTPS origin that forwards to the SC Express server.");
const origin = new URL(raw);
if (origin.protocol !== "https:" || origin.username || origin.password ||
    origin.pathname !== "/" || origin.search || origin.hash) {
  throw new Error("Use only the HTTPS origin, without a path, query or credentials.");
}

const config = {
  $schema: "https://openapi.vercel.sh/vercel.json",
  framework: "vite",
  buildCommand: "npm run build",
  outputDirectory: "dist",
  rewrites: [{ source: "/api/:path*", destination: origin.origin + "/api/:path*" }],
};
writeFileSync(new URL("../vercel.json", import.meta.url), JSON.stringify(config, null, 2) + "\n");
console.log("Vercel API destination:", origin.origin);
