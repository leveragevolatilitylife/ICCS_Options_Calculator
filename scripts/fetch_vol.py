#!/usr/bin/env python3
"""Fetch Cboe delayed quotes (VIX, VIX history, option chains) and write data/vol.json.
Runs on GitHub Actions so the browser never needs cross-site access. Standard library only."""
import json, os, re, sys, time, zipfile, urllib.request, datetime as dt

BASE = 'https://cdn.cboe.com/api/global/delayed_quotes/'
TICKERS = ['SPY', 'QQQ', 'SOXX', 'IWM', 'TLT', 'GLD']   # edit this list to track more underlyings
INDEX = {'SPX': '_SPX', 'RUT': '_RUT', 'NDX': '_NDX'}
OCC = re.compile(r'^([A-Z0-9]+?)(\d{6})([CP])(\d{8})$')


XLSX = 'VTS_Iron_Condor_strategy.xlsx'
HDR = [('type', r'trade type'), ('open', r'^open date'), ('ticker', r'^ticker'), ('near', r'near-month'), ('far', r'distant-month'),
       ('lp', r'long put strike'), ('sp', r'short put strike'), ('sc', r'short call strike'), ('lc', r'long call strike'),
       ('cdate', r'^close date'), ('cprem', r'close premium')]

def serial(n):
    return (dt.date(1899, 12, 30) + dt.timedelta(days=int(n))).isoformat()

def open_positions(path=XLSX):
    """Read open trades (no close date/premium) from the course workbook; header-based, so inserted columns are fine."""
    z = zipfile.ZipFile(path)
    ss = [re.sub(r'<[^>]+>', '', x) for x in re.findall(r'<si>(.*?)</si>', z.read('xl/sharedStrings.xml').decode('utf-8'), re.S)]
    out = []
    for name in z.namelist():
        if not re.match(r'xl/worksheets/sheet\d+\.xml$', name):
            continue
        rows = {}
        for m in re.finditer(r'<c r="([A-Z]+)(\d+)"([^>]*?)(?:/>|>(.*?)</c>)', z.read(name).decode('utf-8'), re.S):
            body = m.group(4) or ''
            v = re.search(r'<v>(.*?)</v>', body)
            if 't="s"' in m.group(3) and v:
                val = ss[int(v.group(1))]
            elif v:
                try: val = float(v.group(1))
                except ValueError: val = v.group(1)
            else:
                t = re.search(r'<t[^>]*>(.*?)</t>', body)
                val = t.group(1) if t else None
            if val is not None and val != '':
                rows.setdefault(int(m.group(2)), {})[m.group(1)] = val
        col = {}
        for rn in sorted(rows):
            cand = {}
            for c, val in rows[rn].items():
                if isinstance(val, str):
                    t = re.sub(r'\s+', ' ', val).strip().lower()
                    for k, pat in HDR:
                        if k not in cand and re.search(pat, t): cand[k] = c
            if 'type' in cand and 'open' in cand and 'near' in cand:
                col = cand; hdr = rn; break
        if not col:
            continue
        for rn in sorted(rows):
            if rn <= hdr: continue
            r = rows[rn]; g = lambda k: r.get(col.get(k))
            ty = str(g('type') or '').strip().lower()
            if not (ty.startswith('iron condor') or ty.startswith('calendar')): continue
            if isinstance(r.get('A'), str) and re.search(r'example|範例', r['A'], re.I): continue
            if isinstance(g('cdate'), float) and isinstance(g('cprem'), float): continue
            if not isinstance(g('near'), float): continue
            p = {'type': 'IC' if ty.startswith('iron') else 'CAL', 'ticker': str(g('ticker')).strip().upper(), 'near': serial(g('near')),
                 'far': serial(g('far')) if isinstance(g('far'), float) else None}
            for k in ('lp', 'sp', 'sc', 'lc'):
                p[k] = g(k) if isinstance(g(k), float) else None
            out.append(p)
    return out

def wanted_legs(pos):
    """Return [(ticker, 'YYYY-MM-DD', 'P'|'C', strike)] for every leg we need to price."""
    legs = set()
    for p in pos:
        if p['type'] == 'IC':
            for t, k in (('P', 'lp'), ('P', 'sp'), ('C', 'sc'), ('C', 'lc')):
                if p[k]: legs.add((p['ticker'], p['near'], t, p[k]))
        else:
            K = p['sp'] or p['lp']
            if K:
                legs.add((p['ticker'], p['near'], 'P', K))
                if p['far']: legs.add((p['ticker'], p['far'], 'P', K))
    return sorted(legs)

def f(x):
    try:
        v = float(x); return v if v == v else None
    except (TypeError, ValueError):
        return None

def leg_quotes(chains_raw, legs):
    out = []
    for (tk, date, ty, K) in legs:
        d = chains_raw.get(tk)
        if not d: continue
        for o in d.get('options', []):
            m = OCC.match(o.get('option', ''))
            if not m or m.group(3) != ty: continue
            if '20%s-%s-%s' % (m.group(2)[:2], m.group(2)[2:4], m.group(2)[4:6]) != date: continue
            if abs(int(m.group(4)) / 1000 - K) > 1e-6: continue
            bid, ask, iv = f(o.get('bid')), f(o.get('ask')), f(o.get('iv'))
            mid = round((bid + ask) / 2, 4) if bid is not None and ask is not None and ask > 0 else None
            out.append({'sym': tk, 'date': date, 'type': ty, 'K': K, 'bid': bid, 'ask': ask, 'mid': mid, 'iv': round(iv * 100, 2) if iv else None})
    return out

def get(path, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(BASE + path, headers={'User-Agent': 'Mozilla/5.0 (iccs-options-calculator)'})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except Exception as e:
            last = e; time.sleep(2 + 3 * i)
    raise last

def pct_rank(closes, v):
    a = [x for x in closes if x and x > 0]
    return None if len(a) < 120 else round(sum(1 for x in a if x < v) / len(a) * 100, 1)

def vix_block():
    q = get('quotes/_VIX.json')['data']
    px = float(q['current_price'])
    hist = []
    try:
        h = get('charts/historical/_VIX.json')['data']
        arr = h if isinstance(h, list) else h.get('data', [])
        hist = [float(r.get('close', r.get('Close', 0)) or 0) for r in arr][-252:]
    except Exception as e:
        print('VIX history failed:', e, file=sys.stderr)
    return {'v': px, 't': str(q.get('last_trade_time', ''))[:16], 'pct': pct_rank(hist, px), 'n': len([x for x in hist if x > 0])}

def chain_block(sym, today, raw=None):
    d = get('options/%s.json' % INDEX.get(sym, sym))['data']
    if raw is not None: raw[sym] = d
    spot = float(d['current_price'])
    puts = []
    for o in d.get('options', []):
        m = OCC.match(o.get('option', ''))
        if not m or m.group(3) != 'P':
            continue
        iv = float(o.get('iv') or 0)
        if iv <= 0:
            continue
        date = '20%s-%s-%s' % (m.group(2)[:2], m.group(2)[2:4], m.group(2)[4:6])
        K = int(m.group(4)) / 1000
        if not (0.8 * spot <= K <= 1.2 * spot):
            continue
        if (dt.date.fromisoformat(date) - today).days > 200:
            continue
        puts.append([date, K, round(iv * 100, 2)])
    return {'spot': spot, 'puts': puts}

def main(out='data/vol.json'):
    today = dt.datetime.now(dt.timezone.utc).date()
    res = {'updated': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'source': 'Cboe delayed quotes', 'chains': {}}
    try:
        res['vix'] = vix_block()
    except Exception as e:
        res['vixErr'] = str(e); print('VIX failed:', e, file=sys.stderr)
    try:
        pos = open_positions()
    except Exception as e:
        pos = []; print('xlsx read failed:', e, file=sys.stderr)
    legs = wanted_legs(pos); raw = {}
    tickers = list(dict.fromkeys(TICKERS + [l[0] for l in legs]))
    for s in tickers:
        try:
            res['chains'][s] = chain_block(s, today, raw)
        except Exception as e:
            res.setdefault('chainErr', {})[s] = str(e); print(s, 'failed:', e, file=sys.stderr)
    res['legs'] = leg_quotes(raw, legs)
    if 'vix' not in res and not res['chains']:
        sys.exit('nothing fetched')
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(res, f, separators=(',', ':'), ensure_ascii=False)
    print('wrote', out)

if __name__ == '__main__':
    main(*sys.argv[1:])
