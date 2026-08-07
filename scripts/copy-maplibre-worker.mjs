/**
 * Copy MapLibre's worker bundle into public/ so the browser can load it.
 *
 * 🔴 WHY THIS SCRIPT EXISTS, after two failed attempts at doing it "properly".
 *
 * MapLibre v6 ships its worker as MULTIPLE files: maplibre-gl-worker.mjs imports
 * maplibre-gl-shared.mjs by a relative path at runtime. Neither is reachable
 * through the package's main entry, so a bundler that inlines the entry emits
 * neither.
 *
 * Attempt 1 was excluding maplibre-gl from Vite's optimizeDeps. That fixes the dev
 * server and does nothing for the production bundle, so a blank map shipped.
 * Attempt 2 was importing the worker with Vite's `?url` suffix. That emits the
 * worker, and then the worker 404s on its own import of the shared chunk, because
 * Vite has no reason to follow a dependency inside a file it is treating as an
 * opaque asset.
 *
 * Copying both files, unmodified, into one directory is what actually works: the
 * worker's relative import resolves because the file it wants is sitting next to
 * it. It also behaves identically in dev and in production, which the first two
 * attempts did not, and that difference is exactly what let a broken build reach
 * the live site.
 *
 * Runs from predev and prebuild. public/maplibre/ is generated, so it is gitignored.
 */
import { copyFileSync, mkdirSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const from = join(root, "node_modules", "maplibre-gl", "dist");
const to = join(root, "public", "maplibre");

mkdirSync(to, { recursive: true });

// The worker and everything it pulls in at runtime. Dev variants and source maps
// are skipped: they are large and only the production pair is ever requested.
const wanted = readdirSync(from).filter(
  (f) =>
    (f === "maplibre-gl-worker.mjs" || f === "maplibre-gl-shared.mjs") && !f.includes("-dev"),
);

if (wanted.length !== 2) {
  console.error(
    `expected maplibre-gl-worker.mjs and maplibre-gl-shared.mjs in ${from}, found: ${wanted.join(", ") || "neither"}`,
  );
  process.exit(1);
}

for (const f of wanted) copyFileSync(join(from, f), join(to, f));
console.log(`copied ${wanted.join(", ")} -> public/maplibre/`);
