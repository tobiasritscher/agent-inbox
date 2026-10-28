"""Nutzungsstatistik über Claude Code, OpenCode und Codex.

Index pro Quelldatei (mtime/size), inkrementell. Einträge gelöschter Transcripts bleiben im Index:
Claude Code räumt alte Transcripts auf, die Statistik soll trotzdem nicht schrumpfen.
Gespeichert werden nur Summen, keine Inhalte.
"""
import calendar
import json
import os
import sqlite3
import threading
import time
from collections import defaultdict
from pathlib import Path

HOME = Path.home()
CLAUDE_PROJECTS = HOME / '.claude/projects'
CODEX_SESSIONS = HOME / '.codex/sessions'
OPENCODE_DB = HOME / '.local/share/opencode/opencode.db'
INDEX = HOME / '.local/state/agent-inbox/stats-index.json'
INDEX_VERSION = 2
BUCKET = 300          # 5-min-Raster für aktive Zeit und Parallelität
WAIT_CAP = 8 * 3600   # längere Pausen sind Feierabend, keine Wartezeit

# $/MTok (input, output, cache_read). Cache-Writes: 1.25× (5 min) bzw. 2× (1 h) Input.
# Reihenfolge zählt: spezifischere Präfixe zuerst.
PRICES = [
    ('claude-fable-5-1', (10, 50, 0.25)), ('claude-mythos-5-1', (10, 50, 0.25)),
    ('claude-fable-5', (10, 50, 1.0)), ('claude-mythos-5', (10, 50, 1.0)),
    ('claude-opus-5-5', (4, 20, 0.20)), ('claude-opus-5', (5, 25, 0.5)),
    ('claude-opus-4-8', (5, 25, 0.5)), ('claude-opus-4-7', (5, 25, 0.5)),
    ('claude-opus-4-6', (5, 25, 0.5)), ('claude-opus-4-5', (5, 25, 0.5)),
    ('claude-opus-4', (15, 75, 1.5)),
    ('claude-sonnet-5', (2, 10, 0.2)), ('claude-sonnet-4', (3, 15, 0.3)),
    ('claude-haiku-5-5', (0.1, 0.5, 0.01)), ('claude-haiku-4-5', (1, 5, 0.1)),
]

CONFIG = HOME / '.config/agent-inbox/config.json'


def load_groups():
    """Ordnername -> Gruppe, z.B. {"Kunden": "Kunde", "Privat": "Privat"}. Ohne Config: keine Gruppen."""
    try:
        return json.loads(CONFIG.read_text()).get('groups') or {}
    except (OSError, ValueError):
        return {}


def price(model):
    return next((p for prefix, p in PRICES if model.startswith(prefix)), None)


def claude_cost(model, u):
    p = price(model)
    if not p:
        return 0.0
    inp, out, read = p
    cc = u.get('cache_creation') or {}
    w1h = cc.get('ephemeral_1h_input_tokens', 0) or 0
    w5m = (u.get('cache_creation_input_tokens') or 0) - w1h
    cost = ((u.get('input_tokens') or 0) * inp + (u.get('output_tokens') or 0) * out
            + (u.get('cache_read_input_tokens') or 0) * read + w5m * inp * 1.25 + w1h * inp * 2) / 1e6
    return cost * (2 if u.get('speed') == 'fast' else 1)


def cache_saving(model, u):
    """Was die Cache-Reads ohne Cache als normaler Input gekostet hätten, minus Read-Preis."""
    p = price(model)
    return (u.get('cache_read_input_tokens') or 0) * (p[0] - p[2]) / 1e6 if p else 0.0


def classify(cwd, groups):
    """cwd -> (Gruppe, Projekt). Projekt = Ordner direkt unter dem Gruppen-Ordner, sonst cwd-Name."""
    parts = Path(cwd or '').parts
    for i, part in enumerate(parts):
        if part in groups:
            return groups[part], (parts[i + 1] if i + 1 < len(parts) else part)
    return ('Andere' if groups else ''), Path(cwd).name if cwd else '?'


def utc_epoch(ts):
    try:
        return calendar.timegm(time.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S'))
    except (TypeError, ValueError):
        return None


class Agg:
    """Summen einer Quelldatei. rows: 'datum|stunde|cwd|modell|quelle' -> Zahlenvektor."""
    FIELDS = ('input', 'output', 'cache_read', 'cache_write', 'reasoning', 'cost', 'saved', 'prompts', 'calls')

    def __init__(self):
        self.rows = defaultdict(lambda: [0] * len(self.FIELDS))
        self.tools = defaultdict(lambda: defaultdict(int))
        self.buckets = set()
        self.waits = []
        self.days = set()

    def add(self, epoch, cwd, model, source, **vals):
        lt = time.localtime(epoch)
        key = f"{time.strftime('%Y-%m-%d', lt)}|{lt.tm_hour}|{cwd}|{model}|{source}"
        row = self.rows[key]
        for k, v in vals.items():
            row[self.FIELDS.index(k)] += v
        self.mark(epoch)

    def mark(self, epoch):
        self.buckets.add(int(epoch // BUCKET))
        self.days.add(time.strftime('%Y-%m-%d', time.localtime(epoch)))

    def to_json(self, session):
        return dict(rows=dict(self.rows), tools={d: dict(t) for d, t in self.tools.items()},
                    buckets=sorted(self.buckets), waits=self.waits, days=sorted(self.days), session=session)


def is_prompt(e):
    if e.get('isMeta') or e.get('isSidechain'):
        return False
    c = (e.get('message') or {}).get('content')
    if isinstance(c, str):
        return not c.startswith(('<local-command', '<command-name>', 'Caveat:'))
    return isinstance(c, list) and any(b.get('type') == 'text' for b in c) and \
        not any(b.get('type') == 'tool_result' for b in c)


def index_claude(path):
    agg, seen, last_turn_end = Agg(), set(), None
    cwd = ''
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            cwd = e.get('cwd') or cwd
            ep = utc_epoch(e.get('timestamp'))
            if ep is None:
                continue
            t = e.get('type')
            if t == 'assistant':
                m = e.get('message') or {}
                model = m.get('model') or '?'
                for b in m.get('content') or []:
                    if b.get('type') == 'tool_use':
                        name = b.get('name', '?')
                        name = 'MCP: ' + name.split('__')[1] if name.startswith('mcp__') and '__' in name[5:] else name
                        agg.tools[time.strftime('%Y-%m-%d', time.localtime(ep))][name] += 1
                mid = m.get('id')
                if mid in seen or model == '<synthetic>':
                    agg.mark(ep)
                    continue
                seen.add(mid)
                u = m.get('usage') or {}
                cw = u.get('cache_creation_input_tokens') or 0
                agg.add(ep, cwd, model, 'Claude Code',
                        input=u.get('input_tokens') or 0, output=u.get('output_tokens') or 0,
                        cache_read=u.get('cache_read_input_tokens') or 0, cache_write=cw,
                        reasoning=(u.get('output_tokens_details') or {}).get('thinking_tokens') or 0,
                        cost=claude_cost(model, u), saved=cache_saving(model, u), calls=1)
                last_turn_end = ep
            elif t == 'user':
                if is_prompt(e):
                    agg.add(ep, cwd, '-', 'Claude Code', prompts=1)
                    if last_turn_end and 0 < ep - last_turn_end < WAIT_CAP:
                        agg.waits.append([time.strftime('%Y-%m-%d', time.localtime(ep)), round(ep - last_turn_end)])
                    last_turn_end = None
                else:
                    agg.mark(ep)
    return agg


def index_codex(path):
    agg, prev, model, cwd = Agg(), None, '?', ''
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ep = utc_epoch(e.get('timestamp'))
            if ep is None:
                continue
            p = e.get('payload') or {}
            pt = p.get('type')
            if e.get('type') == 'session_meta' or (e.get('type') == 'turn_context'):
                cwd = p.get('cwd') or cwd
                model = p.get('model') or model
            elif pt == 'token_count' and p.get('info'):
                tot = p['info'].get('total_token_usage') or {}
                cur = [tot.get(k, 0) for k in ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens')]
                d = [c - q for c, q in zip(cur, prev or [0] * 4)]
                prev = cur
                if any(x > 0 for x in d):
                    agg.add(ep, cwd, model, 'Codex', input=d[0] - d[1], cache_read=d[1], output=d[2],
                            reasoning=d[3], calls=1)
            elif e.get('type') == 'response_item' and pt == 'message' and p.get('role') == 'user':
                text = next((c.get('text', '') for c in p.get('content') or [] if c.get('type') == 'input_text'), '')
                if text and not text.lstrip().startswith(('#', '<')):  # AGENTS.md und App-Kontext sind keine Prompts
                    agg.add(ep, cwd, '-', 'Codex', prompts=1)
                else:
                    agg.mark(ep)
            elif pt in ('function_call', 'custom_tool_call'):
                agg.tools[time.strftime('%Y-%m-%d', time.localtime(ep))][p.get('name', '?')] += 1
                agg.mark(ep)
            else:
                agg.mark(ep)
    return agg


def index_opencode():
    """OpenCode rechnet Kosten selbst (Provider-Preise via LiteLLM): übernehmen statt schätzen.

    Seit 25.10.2026 schreibt OpenCode in session_message (Schema v2, alte Nachrichten migriert);
    die alte Tabelle message liefert nur noch, was dort fehlt.
    """
    per_session = defaultdict(Agg)

    def add(sid, ep, cwd, role, model, m):
        agg = per_session[sid]
        if role == 'user':
            agg.add(ep, cwd, '-', 'OpenCode', prompts=1)
        elif role == 'assistant':
            tk = m.get('tokens') or {}
            cache = tk.get('cache') or {}
            agg.add(ep, cwd, model or '?', 'OpenCode',
                    input=tk.get('input', 0), output=tk.get('output', 0), reasoning=tk.get('reasoning', 0),
                    cache_read=cache.get('read', 0), cache_write=cache.get('write', 0),
                    cost=m.get('cost') or 0, calls=1)

    def tool(sid, ep, name):
        per_session[sid].tools[time.strftime('%Y-%m-%d', time.localtime(ep))][name or '?'] += 1

    try:
        db = sqlite3.connect(f'file:{OPENCODE_DB}?mode=ro', uri=True, timeout=2)
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        dirs = {}
        for t in ('session', 'session_v2'):
            if t in tables:
                dirs.update(db.execute(f'SELECT id, directory FROM {t}'))
        seen, old_only = set(), set()
        if 'session_message' in tables:
            rows = db.execute("SELECT id, session_id, type, data FROM session_message "
                              "WHERE type IN ('user', 'assistant') ORDER BY time_created")
            for mid, sid, typ, data in rows:
                try:
                    m = json.loads(data)
                except ValueError:
                    continue
                seen.add(mid)
                ep = ((m.get('time') or {}).get('created') or 0) / 1000
                if not ep:
                    continue
                add(sid, ep, dirs.get(sid, ''), typ, (m.get('model') or {}).get('id'), m)
                for c in m.get('content') or []:
                    if c.get('type') == 'tool':
                        tool(sid, ep, c.get('name'))
        for mid, sid, data in db.execute('SELECT id, session_id, data FROM message ORDER BY time_created'):
            if mid in seen:
                continue
            try:
                m = json.loads(data)
            except ValueError:
                continue
            ep = ((m.get('time') or {}).get('created') or 0) / 1000
            if ep:
                add(sid, ep, (m.get('path') or {}).get('cwd') or dirs.get(sid, ''), m.get('role'), m.get('modelID'), m)
                old_only.add(mid)
        # Tool-Parts der alten Tabelle nur für Nachrichten, die nicht migriert sind (sonst doppelt).
        for mid, sid, data in db.execute("SELECT message_id, session_id, data FROM part WHERE data LIKE '%\"type\":\"tool\"%'"):
            if mid not in old_only:
                continue
            try:
                p = json.loads(data)
            except ValueError:
                continue
            ts = ((p.get('state') or {}).get('time') or {}).get('start')
            if ts:
                tool(sid, ts / 1000, p.get('tool'))
        db.close()
    except sqlite3.Error:
        pass
    return per_session


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.index = self._load()
        self.building = False

    def _load(self):
        try:
            d = json.loads(INDEX.read_text())
            if d.get('version') != INDEX_VERSION:
                # Neuer Parser: vorhandene Dateien neu lesen, Summen gelöschter Transcripts behalten.
                for f in d.get('files', {}).values():
                    f['mtime'] = -1
                d['version'] = INDEX_VERSION
            return d
        except (OSError, ValueError):
            pass
        return dict(version=INDEX_VERSION, files={}, created=time.time())

    def refresh(self):
        """Nur geänderte Dateien neu lesen. Läuft im Hintergrund-Thread."""
        if self.building:
            return
        self.building = True
        try:
            files = dict(self.index['files'])
            sources = [(p, index_claude, 'subagents' not in p.parts) for p in CLAUDE_PROJECTS.glob('**/*.jsonl')]
            sources += [(p, index_codex, True) for p in CODEX_SESSIONS.glob('**/*.jsonl')]
            for path, fn, is_session in sources:
                try:
                    st = path.stat()
                except OSError:
                    continue
                key = str(path)
                old = files.get(key)
                if old and old['mtime'] == st.st_mtime and old['size'] == st.st_size:
                    continue
                try:
                    agg = fn(path)
                except OSError:
                    continue
                files[key] = dict(mtime=st.st_mtime, size=st.st_size, **agg.to_json(is_session))
            for sid, agg in index_opencode().items():
                files[f'opencode:{sid}'] = dict(mtime=0, size=0, **agg.to_json(True))
            with self.lock:
                self.index = dict(version=INDEX_VERSION, files=files, updated=time.time(),
                                  created=self.index.get('created', time.time()))
            INDEX.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = INDEX.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.index))
            os.replace(tmp, INDEX)
        finally:
            self.building = False

    def summary(self, days):
        """Aggregat für die letzten `days` Tage (0 = alles) plus gleich lange Vorperiode."""
        now = time.time()
        with self.lock:
            files = list(self.index['files'].values())
            updated = self.index.get('updated')
            created = self.index.get('created', now)
        today = time.strftime('%Y-%m-%d')
        start = time.strftime('%Y-%m-%d', time.localtime(now - (days - 1) * 86400)) if days else '0000'
        prev_start = time.strftime('%Y-%m-%d', time.localtime(now - (2 * days - 1) * 86400)) if days else None
        bstart = int((now - days * 86400) // BUCKET) if days else 0
        bprev = int((now - 2 * days * 86400) // BUCKET) if days else None

        F = Agg.FIELDS
        tot, prev = dict.fromkeys(F, 0), dict.fromkeys(F, 0)
        daily = defaultdict(lambda: defaultdict(lambda: dict.fromkeys(('cost', 'output', 'prompts'), 0)))
        groups = defaultdict(lambda: dict(cost=0, output=0, prompts=0, group=''))
        projects = defaultdict(lambda: dict(cost=0, output=0, prompts=0, group=''))
        models, sources = defaultdict(lambda: dict(cost=0, output=0)), defaultdict(lambda: dict(cost=0, output=0, prompts=0))
        heat = [[0] * 24 for _ in range(7)]
        tools = defaultdict(int)
        sessions = prev_sessions = 0
        bucket_count, prev_buckets = defaultdict(int), defaultdict(int)
        waits, prev_waits = [], []
        unpriced = defaultdict(int)
        group_map = load_groups()

        for f in files:
            in_cur = in_prev = False
            for key, vec in f['rows'].items():
                date, hour, cwd, model, source = key.split('|', 4)
                v = dict(zip(F, vec))
                if date >= start:
                    in_cur = True
                    for k in F:
                        tot[k] += v[k]
                    fam = family(model)
                    if model != '-':
                        daily[date][fam]['cost'] += v['cost']
                        daily[date][fam]['output'] += v['output']
                        models[fam]['cost'] += v['cost']
                        models[fam]['output'] += v['output']
                        if source == 'Claude Code' and not price(model) and v['output']:
                            unpriced[model] += v['output']
                        if source == 'Codex':
                            unpriced[model] += v['output']
                    daily[date]['_']['prompts'] += v['prompts']
                    g, p = classify(cwd, group_map)
                    for bucket, name in ((groups, g), (projects, p)) if g else ((projects, p),):
                        bucket[name]['cost'] += v['cost']
                        bucket[name]['output'] += v['output']
                        bucket[name]['prompts'] += v['prompts']
                        bucket[name]['group'] = g
                    s = sources[source]
                    s['cost'] += v['cost']
                    s['output'] += v['output']
                    s['prompts'] += v['prompts']
                    if v['prompts']:
                        wd = time.strptime(date, '%Y-%m-%d').tm_wday
                        heat[wd][int(hour)] += v['prompts']
                elif prev_start and date >= prev_start:
                    in_prev = True
                    for k in F:
                        prev[k] += v[k]
            if f.get('session'):
                sessions += in_cur
                prev_sessions += in_prev
            for d, ts in f['tools'].items():
                if d >= start:
                    for name, n in ts.items():
                        tools[name[:1].upper() + name[1:]] += n  # OpenCode: bash, Claude: Bash
            for b in f['buckets']:
                if b >= bstart:
                    bucket_count[b] += 1
                elif bprev is not None and b >= bprev:
                    prev_buckets[b] += 1
            for d, sec in f['waits']:
                if d >= start:
                    waits.append(sec)
                elif prev_start and d >= prev_start:
                    prev_waits.append(sec)

        # Claude Code löscht Transcripts nach cleanupPeriodDays. Vollständig ist der Index erst ab
        # (erster Index-Build - Aufbewahrung); davor fehlt Geschichte. Vorperiode nur vergleichen, wenn komplett.
        earliest = time.strftime('%Y-%m-%d', time.localtime(created - retention_days() * 86400))
        if not prev_start or earliest > prev_start:
            prev = dict.fromkeys(F, None)
            prev_sessions, prev_buckets, prev_waits = None, None, []

        par_daily = defaultdict(list)
        for b, n in bucket_count.items():
            par_daily[time.strftime('%Y-%m-%d', time.localtime(b * BUCKET))].append(n)

        def med(xs):
            xs = sorted(xs)
            return xs[len(xs) // 2] if xs else None

        dates = sorted(daily)
        if days:
            dates = [time.strftime('%Y-%m-%d', time.localtime(now - i * 86400)) for i in range(days - 1, -1, -1)]
        fams = sorted({k for d in daily.values() for k in d if k != '_'}, key=lambda k: -models[k]['output'])
        top = lambda d, n: sorted(({'name': k, **v} for k, v in d.items()), key=lambda x: -x['cost'] - x['output'] / 1e7)[:n]
        return dict(
            updated=updated, building=self.building, days=days, today=today, earliest=earliest,
            kpi=dict(
                sessions=[sessions, prev_sessions], prompts=[tot['prompts'], prev['prompts']],
                output=[tot['output'], prev['output']], cost=[round(tot['cost'], 2), prev['cost'] and round(prev['cost'], 2)],
                saved=round(tot['saved'], 2),
                cache_hit=round(tot['cache_read'] / max(1, tot['cache_read'] + tot['input'] + tot['cache_write']), 3),
                active_h=[round(len(bucket_count) * BUCKET / 3600, 1),
                          None if prev_buckets is None else round(len(prev_buckets) * BUCKET / 3600, 1)],
                parallel=round(sum(bucket_count.values()) / max(1, len(bucket_count)), 1),
                wait_median=[med(waits), med(prev_waits)], wait_n=len(waits)),
            families=fams,
            daily=[dict(date=d, prompts=daily[d]['_']['prompts'] if d in daily else 0,
                        parallel=round(sum(par_daily[d]) / len(par_daily[d]), 1) if par_daily.get(d) else 0,
                        peak=max(par_daily[d]) if par_daily.get(d) else 0,
                        **{f'{k}_{fam}': round(daily[d][fam][k], 4) if d in daily else 0
                           for fam in fams for k in ('cost', 'output')}) for d in dates],
            groups=top(groups, 10), projects=top(projects, 12),
            sources=[{'name': k, **v} for k, v in sorted(sources.items(), key=lambda x: -x[1]['output'])],
            heat=heat, tools=sorted(({'name': k, 'n': v} for k, v in tools.items()), key=lambda x: -x['n'])[:12],
            unpriced=dict(unpriced))


def retention_days():
    try:
        return int(json.loads((HOME / '.claude/settings.json').read_text()).get('cleanupPeriodDays', 30))
    except (OSError, ValueError, TypeError):
        return 30


def family(model):
    m = model.lower()
    for key, name in (('fable', 'Fable'), ('mythos', 'Fable'), ('opus', 'Opus'), ('sonnet', 'Sonnet'),
                      ('haiku', 'Haiku'), ('gpt', 'GPT'), ('qwen', 'Qwen')):
        if key in m:
            return name
    return 'Andere'
