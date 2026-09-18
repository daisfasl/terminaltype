# terminaltype

A terminal typing game, typeracer-style: solo practice plus multiplayer
"party" races against friends, played entirely in the terminal.

## Features

- **Solo practice** — `words`, `time`, and `quote` modes, a choice of word
  list/language, and quote length filtering, fully offline against bundled
  content.
- **Multiplayer party races** — create or join a room by code, race friends
  against a shared text in real time, live opponent progress.
- **Local stats** — personal bests per mode/setting and race history,
  tracked on-device.

## Repo layout

- `tui/` — the active client. Ink (React for terminals) + TypeScript, run on
  Bun. Ships as a single compiled binary and owns the only database in the
  system: a bundled SQLite file for local player stats, plus JSON word/quote
  content loaded straight from disk.
- `backend/` — FastAPI server. A stateless in-memory relay/coordinator for
  multiplayer room state — no database, no persistence. Race text is
  generated client-side by the host and relayed as-is. Deployed using AWS EC2; 
  the TUI points there by default.


## Getting started

```
brew install daisfasl/terminaltype/terminaltype
terminaltype
```

Or run from source (requires [Bun](https://bun.sh)):

```
cd tui
bun install
bun run start
```

Solo practice works completely offline. Multiplayer connects to
the hosted backend automatically; override the target backend by editing
`~/.config/terminaltype/config.json` (e.g. to point at a local backend for
development).

Alternatively, run the backend + a local Caddy reverse proxy together via
`docker compose up` from the repo root (see `Caddyfile`/`docker-compose.yml`;
defaults to serving `localhost` with no real domain needed).

## License

GPL-3.0. See `LICENSE`.

terminaltype is licensed GPL-3.0 specifically so it can incorporate word list
and quote content from [MonkeyType](https://github.com/monkeytypegame/monkeytype),
which is itself GPL-3.0.

## Attribution

Word lists and quotes (`tui/src/db/seed/languages/`, `tui/src/db/seed/quotes/`)
are sourced from [monkeytypegame/monkeytype](https://github.com/monkeytypegame/monkeytype),
licensed GPL-3.0. Quotes longer than ~300 characters have been filtered out
of the bundled copy.
