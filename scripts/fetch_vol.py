#!/usr/bin/env python3
"""Fetch Cboe delayed quotes (VIX, VIX history, option chains) and write data/vol.json.
Runs on GitHub Actions so the browser never needs cross-site access. Standard library only."""
import json, os, re, sys, time, urllib.request, datetime as dt

BASE = 'https://cdn.cboe.com/api/global/delayed_quotes/'
TICKERS = ['SPY', 'QQQ', 'IWM', 'TLT', 'GLD']   # edit this list to track more underlyings
INDEX = {'SPX': '_SPX', 'RUT': '_RUT', 'NDX': '_NDX'}
OCC = re.compile(r'^([A-Z0-9]+?)(\d{6})([CP])(\d{8})$')

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

def chain_block(sym, today):
    d = get('options/%s.json' % INDEX.get(sym, sym))['data']
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
    for s in TICKERS:
        try:
            res['chains'][s] = chain_block(s, today)
        except Exception as e:
            res.setdefault('chainErr', {})[s] = str(e); print(s, 'failed:', e, file=sys.stderr)
    if 'vix' not in res and not res['chains']:
        sys.exit('nothing fetched')
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(res, f, separators=(',', ':'), ensure_ascii=False)
    print('wrote', out)

if __name__ == '__main__':
    main(*sys.argv[1:])
