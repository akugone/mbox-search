# mbox-search

Local Gmail-like search engine for `.mbox` email archives. Single-file Python app, **zero dependencies** (stdlib only — keep it that way, it's the core selling point for community sharing).

## Architecture (all in `app.py`)

- **Indexer**: streams the mbox (custom `From ` line splitter, handles CRLF), stores metadata + text body in SQLite, full-text via **FTS5** (`unicode61 remove_diacritics 2`). Incremental: per-mbox byte offset checkpoint in `mboxes.indexed_offset`; re-runs only index new bytes. Dedup via `UNIQUE(mbox_id, offset)`.
- **Attachments**: never extracted in bulk. Only names are indexed; files are parsed on demand from the mbox using stored `offset`/`length` (`raw_message()` → `attachment_parts()`), served by `/attachment?id=&i=`.
- **Query parser** (`build_query`): Gmail-like operators — `from:` `to:` `subject:` `label:` `filename:` `has:attachment` `after:` `before:` + free text (implicit AND) and `"exact phrase"`. Operators map to FTS column filters; dates map to SQL conditions on `epoch`.
- **Server**: stdlib `ThreadingHTTPServer` on `127.0.0.1:8422`. Routes: `/` (inline HTML UI in the `PAGE` constant), `/api/search`, `/api/message`, `/api/stats`, `/attachment`.

## Commands

```bash
python3 app.py                 # index *.mbox in script dir, then serve + open browser
python3 app.py --port 9000
MBOX_SEARCH_DB=/tmp/t.db python3 app.py   # relocate the index (used for sandboxed tests)
```

No test suite yet. Quick smoke test:

```bash
python3 -c "import app; con=app.db_connect(); print(app.search(con,'has:attachment filename:pdf')[0])"
```

## Constraints & gotchas

- Python 3.9+ only, no pip installs.
- `mail_index.db` stores the **absolute path** of each mbox (needed for on-demand attachments). If an mbox is moved/renamed, delete the db and reindex.
- Body stored capped at 500k chars (`BODY_LIMIT`); HTML-only mails are converted to text with a regex stripper.
- UI is a single inline HTML string (`PAGE`) — no build step, no external assets.
- `.gitignore` excludes `*.mbox` and `mail_index.db*`: never commit user mail data.
