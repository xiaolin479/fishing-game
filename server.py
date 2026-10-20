#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fisherman's Spring backend - leaderboard + save sync
pure stdlib, zero deps. run: python server.py
  1. serves the game page (index.html)
  2. GET/POST /api/state -> save.json (multi-player)
  3. GET /api/rank?player=xxx&cat=score|aq, daily lazy settle, npc seed board
  4. old single-player save auto-migrates to the first new player
then open http://localhost:8787
"""
import json
import os
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
SAVE_PATH = os.path.join(BASE, 'save.json')
HTML_PATH = os.path.join(BASE, 'index.html')
PORT = int(os.environ.get('PORT', '8787'))
SAVE_LOCK = threading.Lock()
DAY_MS = 86400000

# rank tiers + daily rewards, keep in sync with SEGS in the html
SEGS = [
    ('bronze', 'Bronze', 0, 10),
    ('silver', 'Silver', 100, 20),
    ('gold', 'Gold', 300, 40),
    ('platinum', 'Platinum', 600, 70),
    ('diamond', 'Diamond', 1000, 110),
    ('master', 'Master', 1600, 180),
]

# npc seed board (name, score, collected) so TOP100 isn't empty before real players
NPC = [
    ('Invincible Captain 37', 2280, 38), ('Fish & Shrimp', 2140, 37), ('Hardcore Rod King 74', 1980, 36),
    ('Big Catch', 1850, 35), ('Easy Sea Angler 47', 1720, 34), ('Fervent Sea King 36', 1600, 33),
    ('Mysterious Captain 69', 1490, 32), ('Never Skunked', 1380, 31), ('Legendary Angler 88', 1270, 30),
    ('Old A-Fu', 1160, 29), ('First Cast', 1060, 28), ('Willow Angler', 960, 27),
    ('Bridge Bane', 870, 26), ('Night Angler', 780, 25), ('Pond Regular', 690, 24),
    ('Creek Flyer', 610, 23), ('Sea Breeze', 530, 22), ('Ice Lake Watcher', 460, 21),
    ('Worm Mover', 390, 20), ('Newbie Legend', 320, 19),
]


def seg_of(score):
    seg = SEGS[0]
    for s in SEGS:
        if score >= s[2]:
            seg = s
    return seg


def seg_stars(score, seg):
    stars = min(3, max(0, int((score - seg[2]) / 250)))
    return '★' * stars + '☆' * (3 - stars)


def seg_label(score):
    seg = seg_of(score)
    return seg[1] + ' ' + seg_stars(score, seg)


def empty_store():
    return {
        'players': {},           # pid -> S object (score/name/rankReward ...)
        'nextSettle': int(time.time() * 1000) + DAY_MS,
        'legacy': None,          # old single-player save, for migration
    }


def load_store():
    try:
        with open(SAVE_PATH, 'r', encoding='utf-8') as f:
            d = json.load(f)
    except Exception:
        return empty_store()
    if not isinstance(d, dict) or 'players' not in d:
        # old format: whole file is one player save
        if isinstance(d, dict) and 'coins' in d:
            return {'players': {}, 'nextSettle': int(time.time() * 1000) + DAY_MS, 'legacy': d}
        return empty_store()
    return d


def _write_store(st):
    tmp = SAVE_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, SAVE_PATH)

def save_store(st):
    with SAVE_LOCK:
        _write_store(st)


def settle_if_due(st, now):
    # lazy daily settle: hand out rank rewards at cutoff
    if now < st.get('nextSettle', 0):
        return st
    seg = seg_of
    for pid, s in st.get('players', {}).items():
        sc = s.get('score', 0)
        rw = seg(sc)[3]
        s['rankReward'] = s.get('rankReward', 0) + rw
        s['_rewardCoins'] = s.get('_rewardCoins', 0) + rw * 10
    st['nextSettle'] = now + DAY_MS
    return st


def build_rank(st, pid, cat):
    # board = npc + real players sorted by cat, plus my row
    rows = []
    seen = set()
    for name, score, aq in NPC:
        key = 'npc:' + name
        if cat == 'aq':
            rows.append({'name': name, 'score': aq, 'seg': seg_of(aq)[0],
                         'segLabel': seg_label(aq), 'me': False, 'key': key, 'npc': True})
        else:
            rows.append({'name': name, 'score': score, 'seg': seg_of(score)[0],
                         'segLabel': seg_label(score), 'me': False, 'key': key, 'npc': True})
        seen.add(key)
    me = None
    if pid:
        s = st.get('players', {}).get(pid)
        if s:
            score = s.get('aqCount', 0) if cat == 'aq' else s.get('score', 0)
            _tg = s.get('tg') or {}
            me = {
                'score': score,
                'seg': seg_of(score)[0],
                'segLabel': seg_label(score),
                'reward': {'gems': s.get('rankReward', 0), 'coins': s.get('_rewardCoins', 0)},
                'rank': 0,
                'key': 'me:' + pid,
                'avatar': _tg.get('avatar') or '',
                'account': _tg.get('account') or '',
            }
            tg = s.get('tg') or {}
            rows.append({'name': s.get('name', 'Angler'), 'score': score,
                         'seg': me['seg'], 'segLabel': me['segLabel'], 'me': True,
                         'key': me['key'], 'npc': False,
                         'avatar': tg.get('avatar') or '', 'account': tg.get('account') or ''})
    rows.sort(key=lambda r: -r['score'])
    top = []
    rank = 1
    prev = None
    for r in rows:
        if prev is not None and r['score'] < prev:
            rank += 1
        r['rank'] = rank
        prev = r['score']
        top.append(r)
        if r.get('me') and me:
            me['rank'] = rank
    return top, me


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path)
        if p.path in ('/', '/index.html'):
            try:
                with open(HTML_PATH, 'rb') as f:
                    return self._send(200, f.read(), 'text/html; charset=utf-8')
            except FileNotFoundError:
                return self._send(404, "Not Found Fisherman\'s Spring.html; place it in the server dir")
        if p.path == '/api/state':
            q = parse_qs(p.query)
            pid = (q.get('player') or [''])[0]
            with SAVE_LOCK:
                st = load_store()
                st = settle_if_due(st, int(time.time() * 1000))
                d = st.get('players', {}).get(pid) if pid else None
                if d is None and st.get('legacy') is not None:
                    # old save migrates to the first new player
                    d = st['legacy']
                    st['legacy'] = None
                    if pid:
                        st['players'][pid] = d
                        _write_store(st)
                _write_store(st)
            return self._send(200, json.dumps(d or {}, ensure_ascii=False))
        if p.path == '/api/rank':
            q = parse_qs(p.query)
            pid = (q.get('player') or [''])[0]
            cat = (q.get('cat') or ['score'])[0]
            if cat not in ('score', 'aq'):
                cat = 'score'
            now = int(time.time() * 1000)
            with SAVE_LOCK:
                st = load_store()
                st = settle_if_due(st, now)
                if pid:
                    s = st['players'].setdefault(pid, {})
                    if 'aqCount' not in s:
                        s['aqCount'] = 0
                top, me = build_rank(st, pid, cat)
                _write_store(st)
            best = top[0] if top else None
            out = {
                'top': top[:100],
                'bestSeg': best['segLabel'] if best else '--',
                'bestName': best['name'] if best else '',
                'me': me,
                'nextSettle': st.get('nextSettle', now + DAY_MS),
                'ts': now,
            }
            return self._send(200, json.dumps(out, ensure_ascii=False))
        if p.path == '/favicon.ico':
            return self._send(204, b'')
        return self._send(404, '{"error":"not found"}')

    def do_POST(self):
        p = urlparse(self.path)
        if p.path == '/api/state':
            try:
                q = parse_qs(p.query)
                pid = (q.get('player') or [''])[0]
                n = int(self.headers.get('Content-Length', 0) or 0)
                raw = self.rfile.read(n) if n > 0 else b'{}'
                d = json.loads(raw.decode('utf-8'))
                d['last'] = int(time.time() * 1000)
                with SAVE_LOCK:
                    st = load_store()
                    st = settle_if_due(st, d['last'])
                    players = st.setdefault('players', {})
                    key = pid or d.get('playerId') or ('p' + str(d.get('last', '0')))
                    old = players.get(key) or {}
                    # client already reset rankReward, server clears too
                    if d.get('rankReward', 0) == 0 and old.get('rankReward', 0) > 0:
                        d['rankReward'] = 0
                        d['_rewardCoins'] = 0
                    # collected count feeds the spirit leaderboard
                    d['aqCount'] = len(d.get('aquarium') or {})
                    players[key] = d
                    _write_store(st)
                return self._send(200, json.dumps({'ok': True, 'pid': key}, ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc(file=sys.stderr)
                return self._send(400, json.dumps({'error': str(e)}, ensure_ascii=False))
        if p.path == '/api/adreward':
            try:
                n = int(self.headers.get('Content-Length', 0) or 0)
                d = json.loads(self.rfile.read(n).decode('utf-8') if n > 0 else '{}')
                typ = d.get('type')
                pid = d.get('playerId') or ''
                if typ not in ('gems', 'spirit') or not pid:
                    return self._send(400, '{"error":"bad request"}')
                now = int(time.time() * 1000)
                with SAVE_LOCK:
                    st = load_store()
                    players = st.setdefault('players', {})
                    s = players.get(pid)
                    if not s:
                        # first ad auto-creates the account, so brand-new players don't fail
                        s = {'playerId': pid, 'name': 'Angler', 'gems': 0, 'spirit': 0,
                             'score': 0, 'aquarium': {}, 'last': now,
                             'adsT': 0, 'adsGemsLeft': 3, 'adsSpiritLeft': 2}
                        players[pid] = s
                    # ad counts reset every 24h
                    if now - s.get('adsT', 0) > DAY_MS:
                        s['adsT'] = now
                        s['adsGemsLeft'] = 3
                        s['adsSpiritLeft'] = 2
                    if typ == 'gems':
                        left = s.get('adsGemsLeft', 3)
                        if left <= 0:
                            return self._send(200, '{"ok":false,"msg":"Daily gem ads used up, come back tomorrow"}')
                        s['adsGemsLeft'] = left - 1
                        s['gems'] = s.get('gems', 0) + 3
                    else:
                        left = s.get('adsSpiritLeft', 2)
                        if left <= 0:
                            return self._send(200, '{"ok":false,"msg":"Daily spirit ads used up, come back tomorrow"}')
                        s['adsSpiritLeft'] = left - 1
                        s['spirit'] = s.get('spirit', 0) + 2
                    s['last'] = now
                    _write_store(st)
                    return self._send(200, json.dumps({'ok': True, 'gems': s.get('gems', 0), 'spirit': s.get('spirit', 0),
                                                       'adsGemsLeft': s.get('adsGemsLeft', 3), 'adsSpiritLeft': s.get('adsSpiritLeft', 2)}, ensure_ascii=False))
            except Exception as e:
                import traceback
                traceback.print_exc(file=sys.stderr)
                return self._send(400, '{"error":"bad request"}')
        return self._send(404, '{"error":"not found"}')

    def log_message(self, fmt, *args):
        sys.stderr.write('%s - %s\n' % (self.address_string(), fmt % args))


def main():
    print('fishing server up (leaderboard edition)')
    print(' local  http://localhost:%d/' % PORT)
    print(' lan    http://<lan-ip>:%d/' % PORT)
    print(' save   %s' % SAVE_PATH)
    print(' ctrl+c to stop')
    try:
        ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print('\nserver stopped')


if __name__ == '__main__':
    main()
