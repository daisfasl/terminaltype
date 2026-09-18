// Ambient type for Bun's `with { type: "sqlite", embed: "true" }` import
// attribute (see textGeneration.ts) - TypeScript has no built-in module
// declaration for this Bun-specific import form, so declare it ourselves.
// Resolves to an already-open bun:sqlite Database instance, both under
// plain `bun run` and once embedded in a compiled binary.
declare module "*.db" {
  import type { Database } from "bun:sqlite";
  const db: Database;
  export default db;
}
