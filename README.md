# 📬 mbox-search

Local Gmail-like search for your `.mbox` email archives (Google Takeout, Thunderbird, Apple Mail exports…). A single Python file, zero dependencies, everything stays on your machine.

## Getting started

1. Drop your `.mbox` file(s) into this folder.
2. Run:

```bash
python3 app.py
```

That's it. On first launch, messages are indexed (about a minute for 12,000 emails / 3.4 GB), then the search interface opens in your browser at `http://127.0.0.1:8422`.

Subsequent launches are instant: the index (`mail_index.db`) is reused, and only new messages or new `.mbox` files are indexed.

Options:

```bash
python3 app.py /path/to/folder    # mbox files located elsewhere
python3 app.py archive.mbox       # a specific file
python3 app.py --port 9000        # change the port
```

## Search

| Syntax | Effect |
|---|---|
| `invoice edf` | all words (diacritics ignored) |
| `"exact phrase"` | exact phrase |
| `from:amazon` / `to:martin` | sender / recipient |
| `subject:contract` | in the subject line |
| `filename:pdf` | attachment name |
| `has:attachment` | only messages with attachments |
| `after:2023 before:2024-06` | date range |
| `label:important` | Gmail labels (Takeout exports) |

Operators can be combined: `from:edf filename:pdf after:2022`.

## Attachments

They are not extracted in bulk (no duplicated gigabytes): they are listed in each message and read on demand straight from the `.mbox` when you click to download them.

## Notes

- Requires Python 3.9+ (already installed on macOS/Linux; Windows: [python.org](https://www.python.org/downloads/)).
- The server only listens on `127.0.0.1`: nothing is reachable from the network.
- To reindex from scratch: delete `mail_index.db` and relaunch.
- The index only stores text (~40 MB for 12,000 emails); the original `.mbox` remains the source of truth — don't delete it.

## License

MIT — do whatever you want with it.
