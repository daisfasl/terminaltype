import { Database } from "bun:sqlite";
import { existsSync, readdirSync, readFileSync, unlinkSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// Regenerates tui/src/db/content.db from the tracked seed JSON (see
// tui/src/db/seed/languages/*.json and tui/src/db/seed/quotes/*.json) into a
// single SQLite file that textGeneration.ts embeds via `bun build --compile`
// (see ROADMAP Phase G, Part B) - this is a build artifact, not a file to
// hand-edit or commit.
const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const DB_DIR = join(SCRIPT_DIR, "..", "src", "db");
const SEED_DIR = join(DB_DIR, "seed");
const LANGUAGES_DIR = join(SEED_DIR, "languages");
const QUOTES_DIR = join(SEED_DIR, "quotes");
const OUTPUT_PATH = join(DB_DIR, "content.db");

function loadJsonFiles(dir: string): { name: string; data: string }[] {
  return readdirSync(dir)
    .filter((file) => file.endsWith(".json"))
    .map((file) => ({
      name: file.slice(0, -".json".length),
      data: readFileSync(join(dir, file), "utf-8"),
    }));
}

function main() {
  if (existsSync(OUTPUT_PATH)) unlinkSync(OUTPUT_PATH);

  const db = new Database(OUTPUT_PATH, { create: true });
  db.exec(`
    CREATE TABLE languages (
      name TEXT PRIMARY KEY,
      data TEXT NOT NULL
    )
  `);
  db.exec(`
    CREATE TABLE quotes (
      name TEXT PRIMARY KEY,
      data TEXT NOT NULL
    )
  `);

  const insertLanguage = db.prepare(
    "INSERT INTO languages (name, data) VALUES (?, ?)",
  );
  const languages = loadJsonFiles(LANGUAGES_DIR);
  db.transaction(() => {
    for (const { name, data } of languages) insertLanguage.run(name, data);
  })();

  const insertQuote = db.prepare(
    "INSERT INTO quotes (name, data) VALUES (?, ?)",
  );
  const quotes = loadJsonFiles(QUOTES_DIR);
  db.transaction(() => {
    for (const { name, data } of quotes) insertQuote.run(name, data);
  })();

  db.close();

  console.log(
    `Wrote ${OUTPUT_PATH} (${languages.length} languages, ${quotes.length} quote files)`,
  );
}

main();
