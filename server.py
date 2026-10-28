#!/usr/bin/env python3
"""Agent Inbox: alle wartenden Claude-Code-Sessions an einem Ort, nach Hebel gerankt.

Stdlib only. Liest nur lokale Fakten (Session-Records, Transcripts, busybar-Events, gh)
und speichert nichts davon doppelt. Einziger eigener State: geparkte Sessions.
"""
import json
import os
import secrets
import sys
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import stats

HOME = Path.home()
SESSIONS = HOME / '.claude/sessions'
PROJECTS = HOME / '.claude/projects'
EVENTS = HOME / '.local/state/busybar-agents/events'
STATE = HOME / '.local/state/agent-inbox'
PORT = int(os.environ.get('AGENT_INBOX_PORT', 8787))
TOKEN = secrets.token_urlsafe(24)  # CSRF-Schutz: nur die eigene Seite darf POSTen
TAIL_BYTES = 1_500_000

# Gewichte bewusst im Code, jede Karte zeigt ihre Rechnung.
W_BLOCKED = 3       # Permission-Prompt / AskUserQuestion: Agent steht hart
W_QUESTION = 2      # letzte Nachricht endet mit einer Frage
W_PR = 2            # offener PR auf dem Branch
W_CI_RED = 1        # ... und CI ist rot
W_AGE_PER_H = 0.1   # Alter, gedeckelt: soll ranken, nicht dominieren
AGE_CAP_H = 24
PARKED_FACTOR = 0.5


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


_transcript_paths = {}


def transcript_path(session_id):
    p = _transcript_paths.get(session_id)
    if p and p.exists():
        return p
    p = next(PROJECTS.glob(f'*/{session_id}.jsonl'), None)
    if p:
        _transcript_paths[session_id] = p
    return p


def tail_lines(path):
    with open(path, 'rb') as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        data = f.read().decode('utf-8', 'replace')
    lines = data.splitlines()
    return lines[1:] if size > TAIL_BYTES else lines


def parse_transcript(path):
    """Letzte Agent-Nachricht, offene AskUserQuestion, away_summary, Branch."""
    out = dict(text='', ask=None, summary='', branch='', last_ts='', user_after=False)
    pending_tool_ids = {}
    last_assistant_ts = ''
    for line in tail_lines(path):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get('isSidechain'):
            continue
        out['branch'] = e.get('gitBranch') or out['branch']
        t, ts = e.get('type'), e.get('timestamp', '')
        if t == 'assistant':
            content = (e.get('message') or {}).get('content') or []
            texts = [c.get('text', '') for c in content if c.get('type') == 'text']
            if texts:
                out['text'] = '\n'.join(texts).strip()
                out['user_after'] = False
            for c in content:
                if c.get('type') == 'tool_use':
                    pending_tool_ids[c.get('id')] = c
            last_assistant_ts, out['last_ts'] = ts, ts
        elif t == 'user':
            content = (e.get('message') or {}).get('content')
            if isinstance(content, list):
                for c in content:
                    if c.get('type') == 'tool_result':
                        pending_tool_ids.pop(c.get('tool_use_id'), None)
            if not e.get('isMeta') and not (isinstance(content, list) and
                                             all(c.get('type') == 'tool_result' for c in content)):
                out['user_after'] = True
            out['last_ts'] = ts or out['last_ts']
        elif t == 'system' and e.get('subtype') == 'away_summary':
            summary = e.get('content', '').replace('(disable recaps in /config)', '').strip()
            out['summary'] = summary if ts >= last_assistant_ts else ''
    asks = [c for c in pending_tool_ids.values() if c.get('name') == 'AskUserQuestion']
    if asks:
        out['ask'] = [dict(question=q.get('question', ''), header=q.get('header', ''),
                           multi=q.get('multiSelect', False),
                           options=[o.get('label', '') for o in q.get('options', [])])
                      for q in (asks[-1].get('input') or {}).get('questions', [])]
    out['pending_tool'] = next((c.get('name') for c in pending_tool_ids.values()), None)
    return out


def busybar_states():
    """pid -> letzter Hook-Zustand (waiting/running/finished) aus dem busybar-Tracker."""
    result = {}
    for p in EVENTS.glob('*.json'):
        d = load_json(p, None)
        if d and d.get('agent') == 'claude':
            cur = result.get(d.get('pid'))
            if not cur or d['updated'] > cur['updated']:
                result[d.get('pid')] = d
    return result


class PrCache:
    """gh-Abfragen sind langsam: im Hintergrund, 5 min gecacht, unbekannt = kein Bonus."""

    def __init__(self):
        self.data, self.lock = {}, threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=4)

    def get(self, cwd, branch):
        key = (cwd, branch)
        with self.lock:
            hit = self.data.get(key)
            if hit and (hit[1] == 'pending' or time.time() - hit[0] < 300):
                return None if hit[1] == 'pending' else hit[1]
            self.data[key] = (time.time(), 'pending')
            self.pool.submit(self._fetch, key)
        return hit[1] if hit else None

    def _fetch(self, key):
        cwd, branch = key
        pr = None
        if branch and branch not in ('main', 'master', 'HEAD'):
            try:
                out = subprocess.run(
                    ['gh', 'pr', 'list', '--head', branch, '--state', 'open', '--limit', '1',
                     '--json', 'number,url,isDraft,statusCheckRollup'],
                    cwd=cwd, capture_output=True, text=True, timeout=15)
                items = json.loads(out.stdout or '[]') if out.returncode == 0 else []
                if items:
                    it = items[0]
                    checks = it.get('statusCheckRollup') or []
                    concl = {(c.get('conclusion') or c.get('state') or '').upper() for c in checks}
                    ci = ('red' if concl & {'FAILURE', 'ERROR', 'TIMED_OUT', 'CANCELLED'}
                          else 'pending' if concl & {'', 'PENDING', 'IN_PROGRESS', 'QUEUED'}
                          else 'green' if checks else 'none')
                    pr = dict(number=it['number'], url=it['url'], draft=it['isDraft'], ci=ci)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        with self.lock:
            self.data[key] = (time.time(), pr)


PRS = PrCache()


def iso_to_epoch(ts):
    try:
        return time.mktime(time.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S')) - time.timezone
    except (ValueError, TypeError):
        return None


def score(item, now):
    parts = []
    if item['blocked']:
        parts.append(('blockiert', W_BLOCKED))
    if item['is_question']:
        parts.append(('Frage', W_QUESTION))
    pr = item.get('pr')
    if pr:
        parts.append((f"PR #{pr['number']}", W_PR))
        if pr['ci'] == 'red':
            parts.append(('CI rot', W_CI_RED))
    if item['waiting_since']:
        hours = min(AGE_CAP_H, max(0, (now - item['waiting_since']) / 3600))
        parts.append((f'{hours:.1f} h', round(hours * W_AGE_PER_H, 2)))
    total = sum(v for _, v in parts)
    if item['parked']:
        parts.append(('geparkt ×0.5', round(-total * (1 - PARKED_FACTOR), 2)))
        total *= PARKED_FACTOR
    return round(total, 2), parts


def snapshot():
    now = time.time()
    parked = load_json(STATE / 'parked.json', {})
    hooks = busybar_states()
    items = []
    for f in SESSIONS.glob('*.json'):
        s = load_json(f, None)
        if not s or s.get('kind') != 'interactive' or not alive(s.get('pid', -1)):
            continue
        tp = transcript_path(s.get('sessionId', ''))
        t = parse_transcript(tp) if tp else {}
        hook = hooks.get(s['pid'], {})
        busy = s.get('status') == 'busy'
        # busybar meldet auch SessionStart als 'waiting': nur echte Freigabe-Events zählen.
        perm = (hook.get('state') == 'waiting' and not busy
                and hook.get('event') in ('PermissionRequest', 'Notification', 'Elicitation'))
        blocked = bool(t.get('ask')) or perm
        text = t.get('text', '')
        if blocked:
            state = 'blocked'
        elif busy:
            state = 'running'
        elif t.get('user_after') or not text:
            state = 'idle'
        else:
            state = 'waiting'
        is_question = state == 'waiting' and ('?' in text[-400:] or t.get('summary', '').endswith('?'))
        waiting_since = iso_to_epoch(t.get('last_ts')) if state in ('blocked', 'waiting') else None
        # Parken endet von selbst, sobald in der Session etwas passiert (Ereignis statt Timer).
        p = parked.get(s['sessionId'])
        is_parked = bool(p) and (waiting_since or 0) <= p
        item = dict(
            name=s.get('name', ''), session_id=s['sessionId'], pid=s['pid'], cwd=s.get('cwd', ''),
            project=Path(s.get('cwd', '')).name, branch=t.get('branch', ''), state=state,
            blocked=blocked, permission=perm and not t.get('ask'), ask=t.get('ask'),
            text=text[-4000:], summary=t.get('summary', ''), is_question=is_question,
            waiting_since=waiting_since, parked=is_parked)
        if state in ('blocked', 'waiting'):
            item['pr'] = PRS.get(item['cwd'], item['branch'])
        item['score'], item['why'] = score(item, now) if state in ('blocked', 'waiting') else (0, [])
        items.append(item)
    order = {'blocked': 0, 'waiting': 0, 'running': 1, 'idle': 2}
    items.sort(key=lambda i: (order[i['state']], -i['score'], i['name']))
    return dict(now=now, items=items)


def send_answer(name, text):
    """Offizieller Rückweg: Haiku ruft SendMessage auf, Nachricht kommt via stdin, nicht via argv."""
    prompt = (f'Call SendMessage (load it via ToolSearch first) with to="{name}" and as message '
              'the exact text between <answer> tags below, verbatim, no additions. '
              'Then reply only OK, or the error.\n<answer>\n' + text + '\n</answer>')
    r = subprocess.run(
        ['claude', '-p', '--model', 'haiku', '--tools', 'SendMessage,ToolSearch',
         '--permission-mode', 'bypassPermissions', '--no-session-persistence'],
        input=prompt, capture_output=True, text=True, timeout=90, cwd=STATE)
    ok = r.returncode == 0 and r.stdout.strip().upper().startswith('OK')
    return ok, (r.stdout or r.stderr).strip()[-500:]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/':
            html = (Path(__file__).parent / 'index.html').read_text().replace('__TOKEN__', TOKEN)
            self._send(200, html.encode(), 'text/html; charset=utf-8')
        elif self.path == '/stats.js':
            self._send(200, (Path(__file__).parent / 'stats.js').read_bytes(), 'text/javascript; charset=utf-8')
        elif self.path == '/api/inbox':
            self._send(200, snapshot())
        elif self.path.startswith('/api/stats'):
            q = parse_qs(urlparse(self.path).query)
            days = int((q.get('days') or ['30'])[0])
            self._send(200, STATS.summary(max(0, min(days, 3650))))
        else:
            self._send(404, {'error': 'not found'})

    def do_POST(self):
        if self.headers.get('X-Inbox-Token') != TOKEN:
            return self._send(403, {'error': 'token'})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or '{}')
        except ValueError:
            return self._send(400, {'error': 'json'})
        sid = body.get('session_id', '')
        live = {i['session_id']: i for i in snapshot()['items']}
        if sid not in live:
            return self._send(404, {'error': 'Session nicht (mehr) aktiv'})
        if self.path == '/api/answer':
            text = (body.get('text') or '').strip()
            if not text:
                return self._send(400, {'error': 'leer'})
            ok, detail = send_answer(live[sid]['name'], text)
            return self._send(200 if ok else 502, {'ok': ok, 'detail': detail})
        if self.path == '/api/park':
            parked = load_json(STATE / 'parked.json', {})
            if body.get('park'):
                parked[sid] = time.time()
            else:
                parked.pop(sid, None)
            save_json(STATE / 'parked.json', parked)
            return self._send(200, {'ok': True})
        self._send(404, {'error': 'not found'})


STATS = stats.Stats()


def stats_loop():
    while True:
        try:
            STATS.refresh()
        except Exception as exc:  # Statistik darf die Inbox nie mitreissen
            print(f'stats refresh failed: {exc!r}', flush=True)
        time.sleep(600)


if __name__ == '__main__':
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    threading.Thread(target=stats_loop, daemon=True).start()
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print(f'Agent Inbox: http://127.0.0.1:{PORT}')
    if '--open' in sys.argv:
        subprocess.Popen(['open', f'http://127.0.0.1:{PORT}'])
    srv.serve_forever()
