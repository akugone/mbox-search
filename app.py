#!/usr/bin/env python3
"""
mbox-search — local Gmail-like search for .mbox files
=====================================================

Usage:
    python3 app.py                    # looks for .mbox files in the script's folder
    python3 app.py /path/to/folder    # or in a given folder
    python3 app.py archive.mbox       # or a specific file
    python3 app.py --port 8422        # server port (default 8422)

Zero dependencies: Python 3.9+ is enough (sqlite3 + FTS5 included).
On first launch, messages are indexed (incremental: a new mbox file or
newly appended messages are picked up on the next launch).
Attachments are NOT extracted in bulk: they are read on demand straight
from the mbox when you download them.

Search operators:
    invoice edf                      free text (implicit AND, diacritics ignored)
    "exact phrase"
    from:amazon   to:martin   subject:contract   label:important
    filename:pdf                     attachment name
    has:attachment                   only messages with attachments
    after:2023  before:2024-06       date range
"""
import sys, os, re, json, sqlite3, email, html, mimetypes, threading, webbrowser, time
from email import policy
from email.utils import parsedate_to_datetime, getaddresses
from email.header import decode_header, make_header
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------- config
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("MBOX_SEARCH_DB", os.path.join(SCRIPT_DIR, "mail_index.db"))
PORT = 8422
BODY_LIMIT = 500_000          # body characters kept per message
FROM_RE = re.compile(rb"^From \S+ .*\d{4}")

# ---------------------------------------------------------------- db
def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def db_init(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS mboxes(
      id INTEGER PRIMARY KEY, path TEXT UNIQUE, indexed_offset INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS messages(
      id INTEGER PRIMARY KEY,
      mbox_id INTEGER, offset INTEGER, length INTEGER,
      message_id TEXT, date TEXT, epoch REAL,
      from_addr TEXT, to_addr TEXT, cc_addr TEXT,
      subject TEXT, labels TEXT, attachments TEXT, att_count INTEGER,
      body TEXT,
      UNIQUE(mbox_id, offset));
    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
      subject, from_addr, to_addr, body, labels, attachments,
      content='messages', content_rowid='id',
      tokenize='unicode61 remove_diacritics 2');
    CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
      INSERT INTO messages_fts(rowid,subject,from_addr,to_addr,body,labels,attachments)
      VALUES (new.id,new.subject,new.from_addr,new.to_addr,new.body,new.labels,new.attachments);
    END;
    """)
    con.commit()

# ---------------------------------------------------------------- parsing
def dec(s):
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return str(s)

def addrs(msg, field):
    try:
        return ", ".join(f"{dec(n)} <{a}>" if n else a
                         for n, a in getaddresses(msg.get_all(field, [])))
    except Exception:
        return dec(msg.get(field, ""))

def html_to_text(h):
    h = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", h, flags=re.S | re.I)
    h = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", h, flags=re.I)
    h = re.sub(r"<[^>]+>", " ", h)
    h = html.unescape(h)
    h = re.sub(r"[ \t]+", " ", h)
    return re.sub(r"\n\s*\n+", "\n\n", h).strip()

def extract(msg):
    """-> (body_text, [attachment names])"""
    plain, htm, atts = [], [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fname = part.get_filename()
        disp = str(part.get("Content-Disposition") or "")
        if fname or disp.lower().startswith("attachment"):
            atts.append(dec(fname) if fname else "unnamed")
            continue
        ctype = part.get_content_type()
        try:
            if ctype == "text/plain":
                plain.append(part.get_content())
            elif ctype == "text/html":
                htm.append(part.get_content())
        except Exception:
            try:
                payload = part.get_payload(decode=True)
                if payload:
                    (plain if ctype == "text/plain" else htm).append(
                        payload.decode("utf-8", "replace"))
            except Exception:
                pass
    body = "\n".join(plain).strip() or html_to_text("\n".join(htm))
    return body[:BODY_LIMIT], atts

def iter_mbox(path, start=0):
    """Yields (offset, length, bytes) for each message in the mbox."""
    with open(path, "rb") as f:
        f.seek(start)
        offset = start
        cur, cur_off, prev_blank = [], None, True
        for line in f:
            if prev_blank and FROM_RE.match(line):
                if cur:
                    raw = b"".join(cur)
                    yield cur_off, offset - cur_off, raw
                cur, cur_off = [], offset
            elif cur_off is not None:
                cur.append(line)
            prev_blank = line in (b"\r\n", b"\n")
            offset += len(line)
        if cur:
            yield cur_off, offset - cur_off, b"".join(cur)

def index_mbox(con, path):
    size = os.path.getsize(path)
    row = con.execute("SELECT id, indexed_offset FROM mboxes WHERE path=?", (path,)).fetchone()
    if row is None:
        con.execute("INSERT INTO mboxes(path, indexed_offset) VALUES (?,0)", (path,))
        con.commit()
        row = con.execute("SELECT id, indexed_offset FROM mboxes WHERE path=?", (path,)).fetchone()
    mbox_id, start = row["id"], row["indexed_offset"]
    if start >= size:
        return 0
    print(f"Indexing {os.path.basename(path)} "
          f"({(size - start) / 1e9:.2f} GB remaining)…", flush=True)
    n, last_off = 0, start
    for off, length, raw in iter_mbox(path, start):
        try:
            msg = email.message_from_bytes(raw, policy=policy.default)
            try:
                dt = parsedate_to_datetime(msg.get("Date", ""))
                date, epoch = dt.isoformat(), dt.timestamp()
            except Exception:
                date, epoch = msg.get("Date", ""), 0
            body, atts = extract(msg)
            con.execute(
                """INSERT OR IGNORE INTO messages
                   (mbox_id,offset,length,message_id,date,epoch,from_addr,to_addr,
                    cc_addr,subject,labels,attachments,att_count,body)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mbox_id, off, length, msg.get("Message-ID", ""), date, epoch,
                 addrs(msg, "From"), addrs(msg, "To"), addrs(msg, "Cc"),
                 dec(msg.get("Subject", "")), dec(msg.get("X-Gmail-Labels", "")),
                 "; ".join(atts), len(atts), body))
            n += 1
        except Exception as e:
            print(f"  skipped message (offset {off}): {e}", file=sys.stderr)
        last_off = off + length
        if n % 500 == 0:
            con.execute("UPDATE mboxes SET indexed_offset=? WHERE id=?", (last_off, mbox_id))
            con.commit()
            print(f"  {n} messages…", flush=True)
    con.execute("UPDATE mboxes SET indexed_offset=? WHERE id=?", (size, mbox_id))
    con.commit()
    print(f"  {n} new messages indexed.", flush=True)
    return n

# ---------------------------------------------------------------- search
def parse_date(s, end=False):
    for fmt, span in (("%Y-%m-%d", "day"), ("%Y-%m", "month"), ("%Y", "year")):
        try:
            dt = time.strptime(s, fmt)
            t = time.mktime(dt)
            if end:
                t += {"day": 86400, "month": 32 * 86400, "year": 366 * 86400}[span]
            return t
        except ValueError:
            continue
    return None

def fts_quote(term):
    return '"' + term.replace('"', '""') + '"'

def build_query(q):
    """-> (fts_expr | None, [SQL conditions], [params])"""
    fts, conds, params = [], [], []
    col = {"from": "from_addr", "to": "to_addr", "subject": "subject",
           "label": "labels", "filename": "attachments", "attachment": "attachments"}
    for tok in re.findall(r'\w+:"[^"]*"|\w+:\S+|"[^"]*"|\S+', q or ""):
        if ":" in tok and not tok.startswith('"'):
            op, _, val = tok.partition(":")
            op, val = op.lower(), val.strip('"')
            if op == "has" and val.lower() == "attachment":
                conds.append("m.att_count > 0")
            elif op == "after":
                t = parse_date(val)
                if t:
                    conds.append("m.epoch >= ?"); params.append(t)
            elif op == "before":
                t = parse_date(val, end=True)
                if t:
                    conds.append("m.epoch < ?"); params.append(t)
            elif op in col and val:
                fts.append(f"{col[op]}:{fts_quote(val)}")
            elif val:
                fts.append(fts_quote(tok))
        elif tok.strip('"').strip():
            fts.append(fts_quote(tok.strip('"')))
    return (" AND ".join(fts) or None), conds, params

def search(con, q, page=0, sort="date"):
    fts_expr, conds, params = build_query(q)
    where, p = list(conds), list(params)
    if fts_expr:
        base = ("FROM messages_fts f JOIN messages m ON m.id = f.rowid "
                "WHERE messages_fts MATCH ?")
        p = [fts_expr] + p
        snippet = "snippet(messages_fts, 3, '<b>', '</b>', ' … ', 14)"
        order = "ORDER BY " + ("rank" if sort == "rank" else "m.epoch DESC")
    else:
        base = "FROM messages m WHERE 1=1"
        snippet = "substr(m.body, 1, 160)"
        order = "ORDER BY m.epoch DESC"
    if where:
        base += " AND " + " AND ".join(where)
    total = con.execute(f"SELECT count(*) {base}", p).fetchone()[0]
    rows = con.execute(
        f"""SELECT m.id, m.date, m.from_addr, m.subject, m.attachments, m.att_count,
                   {snippet} AS snip {base} {order} LIMIT 50 OFFSET ?""",
        p + [page * 50]).fetchall()
    return total, [dict(r) for r in rows]

# ---------------------------------------------------------------- attachments on demand
def raw_message(con, msg_id):
    r = con.execute(
        """SELECT m.offset, m.length, b.path FROM messages m
           JOIN mboxes b ON b.id = m.mbox_id WHERE m.id=?""", (msg_id,)).fetchone()
    if not r:
        return None
    with open(r["path"], "rb") as f:
        f.seek(r["offset"])
        return email.message_from_bytes(f.read(r["length"]), policy=policy.default)

def attachment_parts(msg):
    out = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fname = part.get_filename()
        disp = str(part.get("Content-Disposition") or "")
        if fname or disp.lower().startswith("attachment"):
            out.append((dec(fname) if fname else "unnamed", part))
    return out

# ---------------------------------------------------------------- HTTP
PAGE = """<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>mbox search</title>
<style>
:root{--b:#f6f8fc;--c:#fff;--t:#1f1f1f;--m:#5f6368;--a:#0b57d0;--h:#e8f0fe}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--b);color:var(--t)}
header{display:flex;align-items:center;gap:16px;padding:10px 20px;background:var(--c);box-shadow:0 1px 3px rgba(0,0,0,.12);position:sticky;top:0;z-index:2}
header h1{font-size:19px;font-weight:500;margin:0;white-space:nowrap}
#q{flex:1;max-width:720px;padding:11px 18px;border:0;border-radius:24px;background:var(--b);font-size:15px;outline:none}
#q:focus{background:var(--c);box-shadow:0 1px 6px rgba(32,33,36,.28)}
#stats{color:var(--m);font-size:12px;white-space:nowrap}
main{max-width:960px;margin:16px auto;padding:0 12px}
.hint{color:var(--m);font-size:12px;margin:8px 4px}
.hint code{background:var(--h);padding:1px 5px;border-radius:4px}
.card{background:var(--c);border-radius:12px;overflow:hidden;box-shadow:0 1px 2px rgba(0,0,0,.08)}
.row{display:block;padding:12px 16px;border-bottom:1px solid #eee;cursor:pointer;text-decoration:none;color:inherit}
.row:hover{background:var(--h)}
.l1{display:flex;gap:10px;align-items:baseline}
.from{font-weight:600;min-width:180px;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.subj{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.date{color:var(--m);font-size:12px;white-space:nowrap}
.snip{color:var(--m);font-size:13px;margin-top:2px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.snip b{color:var(--t);background:#fef7c3}
.chips{margin-top:4px}.chip{display:inline-block;background:var(--h);color:var(--a);border-radius:12px;padding:1px 10px;font-size:12px;margin-right:6px}
nav{display:flex;gap:8px;justify-content:center;margin:16px}
button{border:1px solid #dadce0;background:var(--c);border-radius:18px;padding:7px 18px;cursor:pointer;font-size:13px}
button:disabled{opacity:.4;cursor:default}
#view{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;z-index:5}
#view.on{display:flex}
#pane{background:var(--c);margin:auto;width:min(860px,94vw);max-height:90vh;border-radius:14px;display:flex;flex-direction:column}
#pane header{position:static;box-shadow:none;border-bottom:1px solid #eee;border-radius:14px 14px 0 0}
#pane h2{font-size:17px;margin:0;flex:1}
#meta{padding:10px 20px;color:var(--m);font-size:13px;border-bottom:1px solid #eee}
#meta b{color:var(--t)}
#body{padding:16px 20px;overflow:auto;white-space:pre-wrap;word-break:break-word;font-size:14px}
#atts{padding:10px 20px;border-top:1px solid #eee}
#atts a{color:var(--a);text-decoration:none;margin-right:14px}
.x{cursor:pointer;font-size:22px;color:var(--m);border:0;background:none}
.empty{padding:48px;text-align:center;color:var(--m)}
</style>
<header><h1>📬 mbox search</h1>
<input id=q placeholder="Search mail…  (from:  subject:  filename:  has:attachment  after:2023)" autofocus>
<span id=stats></span></header>
<main>
<div class=hint>Tips: <code>"exact phrase"</code> <code>from:edf</code> <code>filename:pdf</code> <code>has:attachment</code> <code>after:2022 before:2023-07</code></div>
<div class=card id=results><div class=empty>Type a search, or press Enter to browse everything.</div></div>
<nav><button id=prev disabled>← Previous</button><button id=next disabled>Next →</button></nav>
</main>
<div id=view><div id=pane>
<header><h2 id=vsubj></h2><button class=x onclick="V.classList.remove('on')">✕</button></header>
<div id=meta></div><div id=body></div><div id=atts></div>
</div></div>
<script>
const $=s=>document.querySelector(s),V=$('#view');let page=0,total=0,curQ='';
const esc=s=>(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const fdate=d=>{if(!d)return'';const t=new Date(d);return isNaN(t)?d:t.toLocaleDateString(undefined,{day:'numeric',month:'short',year:'numeric'})};
async function run(){
  const r=await fetch('/api/search?q='+encodeURIComponent(curQ)+'&page='+page),j=await r.json();
  total=j.total;$('#stats').textContent=j.total+' result'+(j.total>1?'s':'');
  $('#results').innerHTML=j.rows.length?j.rows.map(m=>`
    <a class=row onclick="open_(${m.id})">
      <div class=l1><span class=from>${esc(m.from_addr.split('<')[0]||m.from_addr)}</span>
      <span class=subj>${esc(m.subject)||'(no subject)'}</span>
      <span class=date>${fdate(m.date)}</span></div>
      <div class=snip>${m.snip||''}</div>
      ${m.att_count?`<div class=chips>${m.attachments.split('; ').slice(0,4).map(a=>`<span class=chip>📎 ${esc(a)}</span>`).join('')}</div>`:''}
    </a>`).join(''):'<div class=empty>No results.</div>';
  $('#prev').disabled=page==0;$('#next').disabled=(page+1)*50>=total;
  window.scrollTo(0,0);
}
async function open_(id){
  const j=await(await fetch('/api/message?id='+id)).json();
  $('#vsubj').textContent=j.subject||'(no subject)';
  $('#meta').innerHTML=`<b>From:</b> ${esc(j.from_addr)}<br><b>To:</b> ${esc(j.to_addr)}${j.cc_addr?'<br><b>Cc:</b> '+esc(j.cc_addr):''}<br><b>Date:</b> ${fdate(j.date)}`;
  $('#body').textContent=j.body||'(empty body)';
  $('#atts').innerHTML=j.attachments.map((a,i)=>`<a href="/attachment?id=${id}&i=${i}" download>📎 ${esc(a)}</a>`).join('')||'';
  V.classList.add('on');
}
$('#q').addEventListener('keydown',e=>{if(e.key=='Enter'){curQ=e.target.value;page=0;run()}});
$('#prev').onclick=()=>{page--;run()};$('#next').onclick=()=>{page++;run()};
V.onclick=e=>{if(e.target==V)V.classList.remove('on')};
document.addEventListener('keydown',e=>{if(e.key=='Escape')V.classList.remove('on')});
fetch('/api/stats').then(r=>r.json()).then(j=>{$('#stats').textContent=j.count+' messages indexed'});
</script>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        con = db_connect()
        try:
            if u.path == "/":
                data = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif u.path == "/api/stats":
                c = con.execute("SELECT count(*) FROM messages").fetchone()[0]
                self._json({"count": c})
            elif u.path == "/api/search":
                total, rows = search(con, qs.get("q", [""])[0],
                                     int(qs.get("page", ["0"])[0]),
                                     qs.get("sort", ["date"])[0])
                self._json({"total": total, "rows": rows})
            elif u.path == "/api/message":
                r = con.execute("SELECT * FROM messages WHERE id=?",
                                (int(qs["id"][0]),)).fetchone()
                if not r:
                    self.send_error(404); return
                d = dict(r)
                d["attachments"] = [a for a in (r["attachments"] or "").split("; ") if a]
                self._json(d)
            elif u.path == "/attachment":
                msg = raw_message(con, int(qs["id"][0]))
                parts = attachment_parts(msg) if msg else []
                i = int(qs.get("i", ["0"])[0])
                if not msg or i >= len(parts):
                    self.send_error(404); return
                name, part = parts[i]
                payload = part.get_payload(decode=True) or b""
                ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{name.replace(chr(34), "")}"')
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            con.close()

# ---------------------------------------------------------------- main
def find_mboxes(target):
    if os.path.isfile(target):
        return [os.path.abspath(target)]
    return sorted(os.path.abspath(os.path.join(target, f))
                  for f in os.listdir(target) if f.lower().endswith(".mbox"))

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = PORT
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    target = args[0] if args else SCRIPT_DIR
    mboxes = find_mboxes(target)
    if not mboxes:
        print(f"No .mbox file found in {target}"); sys.exit(1)

    con = db_connect()
    db_init(con)
    for p in mboxes:
        index_mbox(con, p)
    count = con.execute("SELECT count(*) FROM messages").fetchone()[0]
    con.close()

    url = f"http://127.0.0.1:{port}"
    print(f"\n✅ {count} messages indexed — open {url}  (Ctrl+C to quit)")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()

if __name__ == "__main__":
    main()
