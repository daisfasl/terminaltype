// Bun-specific "sqlite" import attribute: embeds the database into a
// compiled binary and hands back an already-open Database instance (unlike
// `type: "file"`, which only yields a path - one that lives in bunfs, a
// virtual filesystem bun:sqlite's native binding can't open, see
// https://github.com/oven-sh/bun/issues/15766). Works the same way under
// plain `bun run` in dev. See ROADMAP Phase G, Part B - this is what lets
// content survive being bundled into a single binary, unlike the
// readFileSync/readdirSync-off-`seed/` approach this file used to use.
//
// Word/quote content lives as one JSON blob per language/quote-file, one row
// per file, mirroring monkeytypegame/monkeytype's own per-language layout
// (see ROADMAP Phase D/attribution in README) - not consolidated into a
// single JSON blob, since there's no longer a single "the" word list.
// Deliberately separate from the writable stats DB in db/index.ts - this one
// is a regenerated build artifact (see scripts/build-content-db.ts), that
// one holds durable user data.
import contentDb from "./content.db" with { type: "sqlite", embed: "true" };

function getContentDb() {
  return contentDb;
}

export const DEFAULT_LANGUAGE = "english";

// Comfortably covers the full 300s multiplayer time cap at well beyond
// realistic WPM (see ROADMAP Phase C) — reused by the host when starting a
// multiplayer "time" mode race.
export const TIME_MODE_WORD_BUFFER = 1200;

type Mode = "words" | "time" | "quote";

// MonkeyType's own 4-tier quote-length convention. Each language's quote
// file carries its own `groups` array of [min, max] character-length
// ranges in this exact tier order - read from the file rather than
// hardcoded here, in case a language's ranges ever differ.
export type QuoteLength = "short" | "medium" | "long" | "extreme";

const QUOTE_LENGTH_GROUP_INDEX: Record<QuoteLength, number> = {
  short: 0,
  medium: 1,
  long: 2,
  extreme: 3,
};

interface LanguageFile {
  name: string;
  words: string[];
}

interface QuoteEntry {
  text: string;
  source: string;
  length: number;
  id: number;
}

interface QuoteFile {
  language: string;
  groups: number[][];
  quotes: QuoteEntry[];
}

const wordListCache = new Map<string, string[]>();
// null entry = confirmed no quote file for this language (cached so repeated
// lookups don't keep hitting the filesystem).
const quoteFileCache = new Map<string, QuoteFile | null>();

function loadWordList(language: string): string[] {
  const cached = wordListCache.get(language);
  if (cached) return cached;

  const row = getContentDb()
    .query("SELECT data FROM languages WHERE name = ?")
    .get(language) as { data: string } | null;
  if (!row) {
    throw new Error(`Unknown language: ${language}`);
  }
  const data = JSON.parse(row.data) as LanguageFile;
  wordListCache.set(language, data.words);
  return data.words;
}

function loadQuoteFile(language: string): QuoteFile | null {
  const cached = quoteFileCache.get(language);
  if (cached !== undefined) return cached;

  const row = getContentDb()
    .query("SELECT data FROM quotes WHERE name = ?")
    .get(language) as { data: string } | null;
  if (!row) {
    quoteFileCache.set(language, null);
    return null;
  }
  const data = JSON.parse(row.data) as QuoteFile;
  quoteFileCache.set(language, data);
  return data;
}

// Quotes in a language file whose `length` falls in the requested tier's
// [min, max] range (from that file's own `groups`, not hardcoded).
function quotesInLengthBucket(file: QuoteFile, quoteLength: QuoteLength): QuoteEntry[] {
  const range = file.groups[QUOTE_LENGTH_GROUP_INDEX[quoteLength]];
  if (!range) return file.quotes;
  const [min, max] = range;
  return file.quotes.filter((quote) => quote.length >= min && quote.length <= max);
}

// Not every language MonkeyType ships words for also has a quote file -
// fall back to English quotes rather than throwing. Same degrade-gracefully
// spirit applies if a requested length bucket comes up empty for a (usually
// small) language file - fall back to that language's full quote list
// rather than returning nothing.
export function getRandomQuoteWithSource(
  language: string,
  quoteLength?: QuoteLength,
): { text: string; source: string } {
  let file = loadQuoteFile(language);
  if (!file || file.quotes.length === 0) {
    file = loadQuoteFile(DEFAULT_LANGUAGE);
  }
  if (!file || file.quotes.length === 0) return { text: "", source: "" };

  let pool = file.quotes;
  if (quoteLength) {
    const bucketed = quotesInLengthBucket(file, quoteLength);
    if (bucketed.length > 0) pool = bucketed;
  }

  const chosen = pool[Math.floor(Math.random() * pool.length)]!;
  return { text: chosen.text, source: chosen.source };
}

function randomWords(count: number, language: string): string[] {
  const words = loadWordList(language);
  const result: string[] = [];
  for (let i = 0; i < count; i++) {
    result.push(words[Math.floor(Math.random() * words.length)]!);
  }
  return result;
}

// Available language codes, derived from the bundled seed files - drives the
// Settings "Word list" cycler and (for multiplayer) the Lobby's language row.
export function getAvailableLanguages(): string[] {
  return getContentDb()
    .query("SELECT name FROM languages ORDER BY name")
    .all()
    .map((row) => (row as { name: string }).name);
}

export function generateText(
  mode: Mode,
  count: number,
  language: string = DEFAULT_LANGUAGE,
): string {
  // "words" and "time" both resolve to N random words joined - the caller
  // decides what N means (a word-count setting, an initial local chunk for
  // solo endless mode, or TIME_MODE_WORD_BUFFER for multiplayer). Quote-mode
  // callers that also need the source should use getRandomQuoteWithSource
  // instead - this stays string-only so "words"/"time" callers keep a
  // stable return type.
  if (mode === "quote") return getRandomQuoteWithSource(language).text;
  return randomWords(count, language).join(" ");
}

export function generateMoreWords(
  n: number,
  language: string = DEFAULT_LANGUAGE,
): string {
  return randomWords(n, language).join(" ");
}
