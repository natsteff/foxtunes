#!/usr/bin/env python3
"""Local/LAN FoxTunes browser feasibility prototype."""
import argparse
import base64
import hashlib
from collections import defaultdict, deque
from http.cookies import SimpleCookie
import http.server
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit, urlencode

import foxtunes as f


def track(item):
    return {'uri': item.get('uri'), 'name': item.get('name', 'Unknown track'),
            'artists': ', '.join(a['name'] for a in item.get('artists', [])),
            'art': next((i['url'] for i in (item.get('album') or {}).get('images', [])[-1:]), ''),
            'url': (item.get('external_urls') or {}).get('spotify', ''),
            'duration_ms': item.get('duration_ms', 0)}


class App:
    def __init__(self, data=f.DATA, api=None):
        self.data, self.api = data, api or f.Spotify(data)
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(data / 'web.sqlite3'), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        os.chmod(data / 'web.sqlite3', 0o600)
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS songs(id INTEGER PRIMARY KEY, guest TEXT, uri TEXT,
            metadata TEXT, state TEXT DEFAULT 'waiting', created REAL, detail TEXT DEFAULT '');
          CREATE TABLE IF NOT EXISTS votes(song INTEGER,guest TEXT,PRIMARY KEY(song,guest));
        ''')
        self.db.execute("UPDATE songs SET state='uncertain', detail='Server restarted during handoff; host reconciliation required' WHERE state='sending'")
        self.db.commit()
        self.lock = threading.RLock()
        self.sessions, self.catalog = {}, {}
        self.limits = defaultdict(deque)
        self.now, self.error, self.updated = None, '', 0
        settings_file = data / 'settings.json'
        saved = f.load(settings_file) if settings_file.exists() else {}
        self.enabled, self.device, self.accepting = False, saved.get('device', ''), saved.get('accepting', True)
        self.reason = 'Session stopped after startup. Verify device and enable handoff.'
        self.public_url = os.environ.get('FOXTUNES_PUBLIC_URL', '').rstrip('/')
        if self.public_url:
            parsed = urlsplit(self.public_url)
            if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
                raise ValueError('FOXTUNES_PUBLIC_URL must be an HTTP(S) origin without path.')
        client = os.environ.get('SPOTIFY_CLIENT_ID', '').strip()
        if client:
            if not f.re.fullmatch(r'[a-fA-F0-9]{32}', client):
                raise ValueError('Invalid SPOTIFY_CLIENT_ID.')
            existing = f.load(data / 'config.json') if (data / 'config.json').exists() else {}
            if existing.get('client_id') not in (None, client) and (data / 'tokens.json').exists():
                raise ValueError('Disconnect Spotify before changing its client ID.')
            f.private_write(data / 'config.json', {'client_id': client})
        self.last_poll, self.next_poll = 0, 0
        hostfile = data / 'web-host.json'
        if not hostfile.exists():
            f.private_write(hostfile, {'password': secrets.token_urlsafe(24)})
        self.password = f.load(hostfile)['password']

    def session(self, sid):
        now = time.time()
        for key in list(self.sessions):
            if self.sessions[key]['expires'] < now:
                del self.sessions[key]
        if sid not in self.sessions:
            if len(self.sessions) >= 500:
                raise f.Failure('Too many sessions; try later.')
            sid = secrets.token_urlsafe(24)
            self.sessions[sid] = {'csrf': secrets.token_urlsafe(24), 'host': False, 'expires': now + 8 * 3600}
        return sid, self.sessions[sid]

    def limit(self, key, count, seconds=60):
        now = time.monotonic()
        q = self.limits[key]
        while q and q[0] < now - seconds:
            q.popleft()
        if len(q) >= count:
            raise f.Failure('Please wait before trying again.')
        q.append(now)

    def queue(self, sid):
        rows = self.db.execute('''SELECT s.*,count(v.guest) AS votes,
          max(CASE WHEN v.guest=? THEN 1 ELSE 0 END) AS voted FROM songs s
          LEFT JOIN votes v ON v.song=s.id WHERE s.state NOT IN ('removed','past','playing')
          GROUP BY s.id ORDER BY CASE WHEN s.state='waiting' THEN 1 ELSE 0 END,
          votes DESC,s.created,s.id''', (sid,)).fetchall()
        return [dict(json.loads(r['metadata']), id=r['id'], state=r['state'], votes=r['votes'], voted=bool(r['voted'])) for r in rows]

    def submit(self, sid, uri):
        if not self.accepting:
            raise f.Failure('The host has paused new requests.')
        if uri not in self.catalog:
            raise f.Failure('Search for this track first.')
        if self.db.execute("SELECT id FROM songs WHERE uri=? AND state NOT IN ('past','removed')", (uri,)).fetchone():
            raise f.Failure('Already requested. Give it a vote instead.')
        if self.db.execute("SELECT count(*) FROM songs WHERE guest=? AND state='waiting'", (sid,)).fetchone()[0] >= 3:
            raise f.Failure('You can have up to three waiting requests.')
        self.db.execute('INSERT INTO songs(guest,uri,metadata,created) VALUES(?,?,?,?)',
                        (sid, uri, json.dumps(self.catalog[uri]), time.time()))
        self.db.commit()

    def vote(self, sid, song):
        row = self.db.execute('SELECT state FROM songs WHERE id=?', (song,)).fetchone()
        if not row or row['state'] != 'waiting':
            raise f.Failure('This track is locked for playback.')
        self.db.execute('INSERT OR IGNORE INTO votes(song,guest) VALUES(?,?)', (song, sid))
        self.db.commit()

    def poll(self, force=False):
        if not force and time.monotonic() < self.next_poll:
            return
        self.next_poll = time.monotonic() + (5 if self.enabled else 20)
        try:
            state = self.api.call('/me/player')
            self.updated = time.time()
            self.error = ''
            item = state.get('item') or {}
            actual = (state.get('device') or {}).get('id')
            self.now = dict(track(item), playing=bool(state.get('is_playing')), progress_ms=state.get('progress_ms', 0)) if item else None
            pending = self.db.execute("SELECT * FROM songs WHERE state IN ('sending','accepted','queued','uncertain') ORDER BY id LIMIT 1").fetchone()
            if pending and item.get('uri') == pending['uri'] and actual == self.device and state.get('is_playing') and pending['state'] != 'uncertain':
                self.db.execute("UPDATE songs SET state='playing' WHERE id=?", (pending['id'],))
            self.db.execute("UPDATE songs SET state='past', detail='No longer current; completion unverified' WHERE state='playing' AND uri!=?", (item.get('uri', ''),))
            self.db.commit()
            if not self.enabled:
                return
            if actual != self.device:
                raise f.Failure('Playback device changed. Handoff stopped; host must re-enable it.')
            if self.db.execute("SELECT id FROM songs WHERE state IN ('sending','accepted','queued','uncertain')").fetchone():
                return
            if not state.get('is_playing') or not item.get('duration_ms') or item['duration_ms'] - (state.get('progress_ms') or 0) > 15000:
                return
            rows = self.queue('')
            row = next((r for r in rows if r['state'] == 'waiting'), None)
            if not row:
                return
            self.db.execute("UPDATE songs SET state='sending' WHERE id=?", (row['id'],))
            self.db.commit()
            try:
                self.api.call('/me/player/queue', 'POST', params={'uri': row['uri'], 'device_id': self.device})
            except f.Failure as error:
                self.db.execute("UPDATE songs SET state='uncertain',detail=? WHERE id=?", (str(error), row['id']))
                self.db.commit()
                raise
            self.db.execute("UPDATE songs SET state='accepted' WHERE id=?", (row['id'],))
            self.db.commit()
        except f.Failure as error:
            self.error = str(error)
            self.reason = str(error)
            self.enabled = False
            self.next_poll = time.monotonic() + 60

    def health(self):
        status = 'Running' if self.enabled else ('Needs attention' if self.reason and self.reason not in (
            'Session stopped after startup. Verify device and enable handoff.', 'Handoff stopped by host.',
            'Handoff stopped after host reconciliation.', 'Handoff stopped after host playback control.') else 'Stopped')
        return {'status': status, 'reason': self.reason}

    def oauth_start(self, session):
        if not session['host']:
            raise PermissionError('Host access required.')
        parsed = urlsplit(self.public_url)
        if parsed.scheme != 'https' and parsed.hostname not in ('127.0.0.1', '::1'):
            raise f.Failure('Spotify hostname login requires HTTPS. Use the documented loopback CLI sign-in instead.')
        config = f.load(self.data / 'config.json')
        verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(32)
        session['oauth'] = {'verifier': verifier, 'state': state, 'expires': time.time() + 180}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        return 'https://accounts.spotify.com/authorize?' + urlencode({
            'client_id': config['client_id'], 'response_type': 'code', 'redirect_uri': self.public_url + '/callback',
            'scope': f.SCOPES, 'state': state, 'code_challenge_method': 'S256', 'code_challenge': challenge})

    def oauth_finish(self, session, query):
        flow = session.pop('oauth', None)
        if not session['host'] or not flow or flow['expires'] < time.time() or not secrets.compare_digest(flow['state'], query.get('state', [''])[0]):
            raise PermissionError('Expired or invalid Spotify login. Start again from the host desk.')
        if not query.get('code'):
            raise f.Failure('Spotify authorization was declined.')
        config = f.load(self.data / 'config.json')
        tokens = f.request_json('https://accounts.spotify.com/api/token', 'POST', urlencode({
            'client_id': config['client_id'], 'grant_type': 'authorization_code', 'code': query['code'][0],
            'redirect_uri': self.public_url + '/callback', 'code_verifier': flow['verifier']}).encode(),
            {'Content-Type': 'application/x-www-form-urlencoded'})
        if not tokens.get('access_token') or not tokens.get('refresh_token'):
            raise f.Failure('Spotify did not return the required credentials.')
        tokens['expires_at'] = time.time() + tokens.get('expires_in', 3600)
        f.private_write(self.data / 'tokens.json', tokens)
        self.next_poll = 0

    def start_session(self, device, accepting):
        devices = self.api.call('/me/player/devices').get('devices', [])
        if not any(d.get('id') == device and not d.get('is_restricted') for d in devices):
            raise f.Failure('Player unavailable. Refresh devices and check that Soloist is running.')
        if self.db.execute("SELECT id FROM songs WHERE state IN ('sending','uncertain')").fetchone():
            raise f.Failure('Reconcile the uncertain handoff before starting a session.')
        # A remote mutation may change playback even if its response is lost.
        # Stop dispatch until the selected target and playing state are observed.
        self.enabled = False
        self.reason = 'Starting selected player.'
        try:
            self.api.call('/me/player', 'PUT', {'device_ids': [device], 'play': True})
            observed = {}
            for attempt in range(4):
                observed = self.api.call('/me/player')
                if (observed.get('device') or {}).get('id') == device:
                    break
                time.sleep(0.5)
            if (observed.get('device') or {}).get('id') != device:
                raise f.Failure('Spotify accepted the transfer but the player has not become active. Handoff remains stopped; try Start again after checking player status.')
            seed = None
            if not observed.get('is_playing'):
                if observed.get('item'):
                    self.api.call('/me/player/play', 'PUT', {}, {'device_id': device})
                else:
                    seed = next((r for r in self.queue('') if r['state'] == 'waiting'), None)
                    if not seed:
                        raise f.Failure('Player activated. Request a song in FoxTunes, then press Start session to begin playback.')
                    if self.db.execute("SELECT id FROM songs WHERE state IN ('accepted','queued')").fetchone():
                        raise f.Failure('An earlier handoff remains unresolved. Reconcile it before starting a new track.')
                    self.db.execute("UPDATE songs SET state='sending' WHERE id=?", (seed['id'],))
                    self.db.commit()
                    try:
                        self.api.call('/me/player/play', 'PUT', {'uris': [seed['uri']]}, {'device_id': device})
                    except f.Failure:
                        self.db.execute("UPDATE songs SET state='uncertain',detail='Start-track result uncertain; reconcile before retry' WHERE id=?", (seed['id'],))
                        self.db.commit()
                        raise
                    self.db.execute("UPDATE songs SET state='accepted' WHERE id=?", (seed['id'],))
                    self.db.commit()
            self.api.call('/me/player/shuffle', 'PUT', params={'state': 'false', 'device_id': device})
            self.api.call('/me/player/repeat', 'PUT', params={'state': 'off', 'device_id': device})
            for attempt in range(4):
                observed = self.api.call('/me/player')
                valid = ((observed.get('device') or {}).get('id') == device and observed.get('is_playing')
                         and not observed.get('shuffle_state') and observed.get('repeat_state') in (None, 'off'))
                if valid:
                    break
                time.sleep(0.5)
            if not valid:
                raise f.Failure('Player playback/settings could not be confirmed. Handoff remains stopped; check Soloist connectivity and Spotify permissions.')
            f.private_write(self.data / 'settings.json', {'device': device, 'accepting': accepting})
            self.device, self.accepting = device, accepting
            self.enabled, self.reason, self.error = True, '', ''
            self.next_poll = 0
            self.now = dict(track(observed.get('item') or {}), playing=True, progress_ms=observed.get('progress_ms', 0))
            self.updated = time.time()
            if seed and (observed.get('item') or {}).get('uri') == seed['uri']:
                self.db.execute("UPDATE songs SET state='playing' WHERE id=?", (seed['id'],))
                self.db.commit()
        except (f.Failure, OSError) as error:
            self.reason = 'Session start needs attention: ' + (str(error) if isinstance(error, f.Failure) else 'Could not save configuration.')
            self.error = self.reason
            self.enabled = False
            self.next_poll = time.monotonic() + 30
            raise f.Failure(self.reason)

    def skip_request(self):
        if not self.device:
            raise f.Failure('Start a session on the selected player first.')
        if self.db.execute("SELECT id FROM songs WHERE state IN ('sending','uncertain')").fetchone():
            raise f.Failure('Reconcile the uncertain handoff before skipping.')
        resume = self.enabled
        chosen = None
        mutated = False
        self.enabled = False
        try:
            before = self.api.call('/me/player')
            before_uri = (before.get('item') or {}).get('uri')
            if (before.get('device') or {}).get('id') != self.device:
                raise f.Failure('Selected player is no longer active. Start the session again before skipping.')
            # A handed-off request is locked ahead of still-votable requests.
            pending = self.db.execute("SELECT * FROM songs WHERE state IN ('accepted','queued') ORDER BY id LIMIT 1").fetchone()
            if pending:
                chosen = dict(pending)
                # Already playing but not yet reconciled: do not replay it.
                if (before.get('item') or {}).get('uri') == chosen['uri'] and before.get('is_playing'):
                    self.db.execute("UPDATE songs SET state='playing' WHERE id=?", (chosen['id'],))
                    self.db.commit()
                    pending = chosen = None
            if not pending:
                chosen = next((r for r in self.queue('') if r['state'] == 'waiting'), None)
            if chosen and not pending:
                self.db.execute("UPDATE songs SET state='sending' WHERE id=?", (chosen['id'],))
                self.db.commit()
                mutated = True
                self.api.call('/me/player/play', 'PUT', {'uris': [chosen['uri']]}, {'device_id': self.device})
            else:
                # Do not queue or directly play an already-handed-off URI twice.
                mutated = True
                self.api.call('/me/player/next', 'POST', params={'device_id': self.device})
            for attempt in range(4):
                observed = self.api.call('/me/player')
                item = observed.get('item') or {}
                target = chosen['uri'] if chosen else None
                changed = item.get('uri') == target if target else bool(item.get('uri') and item.get('uri') != before_uri)
                if (observed.get('device') or {}).get('id') == self.device and observed.get('is_playing') and changed:
                    break
                time.sleep(0.5)
            else:
                raise f.Failure('Next track could not be confirmed. Handoff stopped; reconcile before trying again.')
            self.db.execute("UPDATE songs SET state='past',detail='Host skipped; full playback not claimed' WHERE state='playing'")
            if chosen:
                self.db.execute("UPDATE songs SET state='playing',detail='Host skip observed selected request playing' WHERE id=?", (chosen['id'],))
            self.db.commit()
            self.now = dict(track(observed.get('item') or {}), playing=True, progress_ms=observed.get('progress_ms', 0))
            self.updated = time.time()
            self.enabled = resume
            self.reason = '' if resume else 'Handoff stopped by host.'
            self.error = ''
            self.next_poll = time.monotonic() + 5
        except f.Failure as error:
            if chosen and mutated:
                self.db.execute("UPDATE songs SET state='uncertain',detail=? WHERE id=?", (str(error), chosen['id']))
                self.db.commit()
            self.reason = 'Skip needs attention: ' + str(error)
            self.error = self.reason
            self.next_poll = time.monotonic() + 30
            raise f.Failure(self.reason)

    def action(self, sid, route, body):
        session = self.sessions[sid]
        if route == '/api/host/login':
            self.limit('login', 10, 300)
            if not secrets.compare_digest(str(body.get('password', '')), self.password):
                raise PermissionError('Host password is incorrect.')
            session['host'] = True
            return {'ok': True}
        if route.startswith('/api/host/') and not session['host']:
            raise PermissionError('Host access required.')
        if route == '/api/request':
            self.limit(('submit', sid), 6)
            self.submit(sid, body.get('uri'))
        elif route == '/api/vote':
            self.limit(('vote', sid), 30)
            self.vote(sid, int(body['id']))
        elif route == '/api/host/start':
            self.start_session(str(body.get('device', '')), bool(body.get('accepting', True)))
        elif route == '/api/host/stop':
            self.enabled = False
            self.reason = 'Handoff stopped by host.'
            self.error = ''
        elif route == '/api/host/settings':
            accepting = bool(body.get('accepting', True))
            enabled = bool(body.get('enabled'))
            device = str(body.get('device', ''))
            devices = self.api.call('/me/player/devices').get('devices', [])
            if not any(d.get('id') == device and not d.get('is_restricted') for d in devices):
                raise f.Failure('Choose an available unrestricted playback device.')
            if enabled:
                state = self.api.call('/me/player')
                if (state.get('device') or {}).get('id') != device:
                    raise f.Failure('Activate that device in Spotify first, then enable handoff.')
                if state.get('shuffle_state') or (state.get('repeat_state') not in (None, 'off')):
                    raise f.Failure('Turn off Spotify shuffle/repeat before enabling handoff.')
            # Commit only after the whole proposal validates, including persistence.
            f.private_write(self.data / 'settings.json', {'device': device, 'accepting': accepting})
            self.device, self.accepting, self.enabled = device, accepting, enabled
            self.reason = '' if enabled else 'Handoff stopped by host.'
            self.error = ''
            self.next_poll = 0
        elif route == '/api/host/remove':
            row = self.db.execute('SELECT state FROM songs WHERE id=?', (int(body['id']),)).fetchone()
            if not row or row['state'] != 'waiting':
                raise f.Failure('Only waiting requests can be removed. Reconcile handed-off tracks in Spotify.')
            self.db.execute("UPDATE songs SET state='removed' WHERE id=?", (int(body['id']),))
            self.db.commit()
        elif route == '/api/host/reconcile':
            note = str(body.get('note', '')).strip()
            if len(note) < 10:
                raise f.Failure('Describe what you checked in Spotify before releasing this request.')
            row = self.db.execute('SELECT state FROM songs WHERE id=?', (int(body['id']),)).fetchone()
            if not row or row['state'] not in ('uncertain', 'accepted', 'queued'):
                raise f.Failure('Only an unresolved handoff can be reconciled.')
            self.enabled = False
            self.reason = 'Handoff stopped after host reconciliation.'
            self.db.execute("UPDATE songs SET state='past', detail=? WHERE id=?",
                            ('Host reconciled; no completion claim: ' + note[:300], int(body['id'])))
            self.db.commit()
        elif route == '/api/host/skip':
            self.skip_request()
        elif route == '/api/host/pause':
            if not self.device:
                raise f.Failure('Select the playback device first.')
            self.enabled = False
            self.reason = 'Handoff stopped after host playback control.'
            if route.endswith('pause'):
                self.api.call('/me/player/pause', 'PUT', params={'device_id': self.device})
            else:
                self.api.call('/me/player/next', 'POST', params={'device_id': self.device})
        elif route == '/api/host/logout':
            session['host'] = False
        else:
            raise LookupError('Unknown action.')
        return {'ok': True}


def handler(app):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, code, payload, content='application/json', cookie=None):
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header('Content-Type', content)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' https://i.scdn.co; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            if cookie:
                self.send_header('Set-Cookie', 'ft_session=' + cookie + '; HttpOnly; SameSite=Lax; Path=/; Max-Age=28800' + ('; Secure' if app.public_url.startswith('https://') else ''))
            self.end_headers()
            self.wfile.write(raw)

        def handle_route(self, post=False):
            try:
                with app.lock:
                    host = self.headers.get('Host', '')
                    # Explicit allowed authorities prevent DNS-rebinding requests to arbitrary hosts.
                    if host not in self.server.allowed_hosts:
                        raise PermissionError('Unknown host.')
                    cookies = SimpleCookie(self.headers.get('Cookie', ''))
                    old = cookies.get('ft_session')
                    sid, session = app.session(old.value if old else '')
                    if post:
                        if self.headers.get('Origin') != (app.public_url or 'http://' + host) or self.headers.get('X-FoxTunes-CSRF') != session['csrf']:
                            raise PermissionError('Invalid request origin or session.')
                        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                            raise ValueError('JSON required.')
                        length = int(self.headers.get('Content-Length', 0))
                        if not 0 < length <= 4096:
                            raise ValueError('Invalid request size.')
                        app.limit(('ip', self.client_address[0]), 100)
                        body = json.loads(self.rfile.read(length))
                        payload = app.action(sid, urlsplit(self.path).path, body)
                    else:
                        parsed = urlsplit(self.path)
                        query = parse_qs(parsed.query)
                        if parsed.path == '/healthz':
                            self.respond(200, {'ok': True})
                            return
                        if parsed.path == '/api/host/spotify-login':
                            url = app.oauth_start(session)
                            self.send_response(303)
                            self.send_header('Location', url)
                            self.send_header('Cache-Control', 'no-store')
                            self.send_header('Content-Length', '0')
                            self.end_headers()
                            return
                        if parsed.path == '/callback':
                            app.oauth_finish(session, query)
                            self.respond(200, b'<!doctype html><title>FoxTunes connected</title><p>Spotify connected. <a href="/host">Return to host desk</a></p>', 'text/html')
                            return
                        if parsed.path in ('/', '/host', '/app.js', '/style.css', '/foxtunes-logo.png'):
                            name = {'/': 'index.html', '/host': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css', '/foxtunes-logo.png': 'foxtunes-logo.png'}[parsed.path]
                            content = {'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript', 'style.css': 'text/css', 'foxtunes-logo.png': 'image/png'}[name]
                            self.respond(200, (Path(__file__).parent / 'web' / name).read_bytes(), content, sid)
                            return
                        if parsed.path == '/api/state':
                            payload = {'csrf': session['csrf'], 'host': session['host'], 'now': app.now,
                                       'queue': app.queue(sid), 'accepting': app.accepting, 'enabled': app.enabled,
                                       'error': app.error, 'updated': app.updated, 'handoff': app.health()}
                            if session['host']:
                                payload['device'] = app.device
                                payload['spotify_connected'] = (app.data / 'tokens.json').exists()
                        elif parsed.path == '/api/search':
                            app.limit(('search', sid), 15)
                            app.limit(('search-ip', self.client_address[0]), 60)
                            text = query.get('q', [''])[0].strip()
                            offset = int(query.get('offset', ['0'])[0])
                            if not 1 <= len(text) <= 150 or not 0 <= offset <= 990:
                                raise ValueError('Enter a search of 1–150 characters.')
                            result = app.api.call('/search', params={'q': text, 'type': 'track', 'limit': 10, 'offset': offset}).get('tracks', {})
                            items = [track(t) for t in result.get('items', [])]
                            # Bounded in-memory catalog; metadata always comes from Spotify, never guest HTML.
                            if len(app.catalog) > 2000:
                                app.catalog.clear()
                            for item in items:
                                app.catalog[item['uri']] = item
                            payload = {'items': items, 'more': bool(result.get('next')), 'offset': offset}
                        elif parsed.path == '/api/host/devices':
                            if not session['host']:
                                raise PermissionError('Host access required.')
                            payload = {'devices': app.api.call('/me/player/devices').get('devices', [])}
                        else:
                            raise LookupError('Not found.')
                    self.respond(200, payload, cookie=sid)
            except PermissionError as error:
                self.respond(403, {'error': str(error)})
            except LookupError as error:
                self.respond(404, {'error': str(error)})
            except (ValueError, TypeError, json.JSONDecodeError):
                self.respond(400, {'error': 'Invalid request.'})
            except f.Failure as error:
                self.respond(409, {'error': str(error)})
            except Exception:
                self.respond(500, {'error': 'Local operation failed. Check host setup.'})

        def do_GET(self):
            self.handle_route()

        def do_POST(self):
            self.handle_route(True)
    return Handler


def make_server(app, bind='127.0.0.1', port=8863, allowed=None):
    server = http.server.ThreadingHTTPServer((bind, port), handler(app))
    server.daemon_threads = True
    actual = server.server_address[1]
    server.allowed_hosts = set(allowed or ['127.0.0.1:' + str(actual), 'localhost:' + str(actual)])
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8863)
    parser.add_argument('--lan-host', help='LAN IP used in browser URLs, required for LAN binding')
    args = parser.parse_args()
    if args.bind != '127.0.0.1' and not args.lan_host and not os.environ.get('FOXTUNES_PUBLIC_URL'):
        parser.error('LAN binding requires --lan-host with this computer\'s LAN IP.')
    os.umask(0o077)
    app = App()
    allowed = ['127.0.0.1:%s' % args.port, 'localhost:%s' % args.port]
    if args.lan_host:
        allowed.append('%s:%s' % (args.lan_host, args.port))
    if app.public_url:
        allowed.append(urlsplit(app.public_url).netloc)
    server = make_server(app, args.bind, args.port, allowed)
    stop = threading.Event()

    def worker():
        while not stop.wait(1):
            with app.lock:
                app.poll()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    print('FoxTunes: http://127.0.0.1:%s' % args.port, flush=True)
    print('Host page: /host. Password is in private .local-work/runtime/web-host.json', flush=True)
    print('Automatic handoff starts OFF; enable it only for a deliberate host test.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        thread.join(timeout=20)
        app.db.close()


if __name__ == '__main__':
    main()
