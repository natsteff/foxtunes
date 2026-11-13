import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import foxtunes as f

A, B, C = ['spotify:track:' + c * 22 for c in 'abc']


class API:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def call(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return {}


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.ledger = f.Ledger(self.data)

    def tearDown(self):
        self.ledger.db.close()
        self.temp.cleanup()

    def test_three_guests_persist_without_spotify(self):
        for i, uri in enumerate([A, B, C]):
            self.ledger.submit('guest-' + str(i), uri)
        reopened = f.Ledger(self.data)
        self.assertEqual([r['uri'] for r in reopened.rows()], [A, B, C])
        self.assertEqual({r['state'] for r in reopened.rows()}, {'requested'})
        reopened.db.close()
        self.assertFalse((self.data / 'tokens.json').exists())

    def test_duplicate_and_bad_input_rejected(self):
        self.ledger.submit('guest', A)
        with self.assertRaises(f.Failure):
            self.ledger.submit('other', A)
        with self.assertRaises(f.Failure):
            self.ledger.submit('guest', 'https://example.com')

    def test_acceptance_is_not_playback(self):
        self.ledger.submit('guest', A)
        self.assertEqual(self.ledger.dispatch(API(), 'speaker')['state'], 'command-accepted')
        self.assertEqual(self.ledger.rows()[0]['state'], 'command-accepted')
        with self.assertRaises(f.Failure):
            self.ledger.dispatch(API(), 'speaker')

    def test_observation_requires_device_and_playing(self):
        self.ledger.submit('guest', A)
        self.ledger.dispatch(API(), 'speaker')
        for device, playing in [('other', True), ('speaker', False)]:
            self.ledger.observe({'item': {'uri': A}, 'device': {'id': device}, 'is_playing': playing})
            self.assertEqual(self.ledger.rows()[0]['state'], 'command-accepted')
        self.ledger.observe({'item': {'uri': A}, 'device': {'id': 'speaker'}, 'is_playing': True, 'progress_ms': 100})
        self.assertEqual(self.ledger.rows()[0]['state'], 'observed-playing')
        self.assertNotIn('completed', self.ledger.rows()[0]['state'])

    def test_timeout_does_not_retry(self):
        self.ledger.submit('guest', A)
        api = API(f.Failure('timeout', uncertain=True))
        self.assertEqual(self.ledger.dispatch(api, 'speaker')['state'], 'uncertain')
        with self.assertRaises(f.Failure):
            self.ledger.dispatch(api, 'speaker')
        self.assertEqual(len(api.calls), 1)

    def test_crash_or_second_process_preserves_dispatch_lock(self):
        self.ledger.submit('guest', A)
        self.ledger.db.execute("UPDATE requests SET state='dispatching'")
        self.ledger.db.commit()
        other = f.Ledger(self.data)
        with self.assertRaises(f.Failure):
            other.dispatch(API(), 'speaker')
        self.assertEqual(other.rows()[0]['state'], 'dispatching')
        other.db.close()

    def test_forbidden_records_failure(self):
        self.ledger.submit('guest', A)
        self.assertEqual(self.ledger.dispatch(API(f.Failure('Spotify HTTP 403')), 'speaker')['state'], 'failed')

    def test_private_token_storage_and_refresh(self):
        f.private_write(self.data / 'config.json', {'client_id': 'id'})
        f.private_write(self.data / 'tokens.json', {'access_token': 'old', 'refresh_token': 'private', 'expires_at': 0})
        with patch.object(f, 'request_json', return_value={'access_token': 'fresh', 'expires_in': 3600}):
            self.assertEqual(f.Spotify(self.data).token(), 'fresh')
        saved = json.loads((self.data / 'tokens.json').read_text())
        self.assertEqual(saved['refresh_token'], 'private')
        self.assertEqual((self.data / 'tokens.json').stat().st_mode & 0o777, 0o600)

    def test_error_redacts_response_body(self):
        from io import BytesIO
        error = urllib.error.HTTPError('https://api.spotify.com', 403, 'Forbidden', {}, BytesIO(b'SECRET'))
        with patch.object(f.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaises(f.Failure) as caught:
                f.request_json('https://api.spotify.com')
        self.assertNotIn('SECRET', str(caught.exception))

    def test_cli_three_guests_atomic_and_no_token_access(self):
        import contextlib
        import io
        with patch.object(f, 'DATA', self.data), patch('sys.argv', ['foxtunes.py', 'three-guests', A, B, C]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(f.main(), 0)
                self.assertEqual(f.main(), 1)  # duplicates fail without partial insert
        self.assertEqual(len(self.ledger.rows()), 3)
        self.assertFalse((self.data / 'tokens.json').exists())

    def test_oauth_state_validation_and_pkce_exchange(self):
        import contextlib
        import io
        import threading
        import urllib.parse
        import urllib.request
        f.private_write(self.data / 'config.json', {'client_id': 'a' * 32})
        errors = []
        threads = []

        def browser(url):
            def callback():
                try:
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                    self.assertEqual(query['code_challenge_method'], ['S256'])
                    with self.assertRaises(urllib.error.HTTPError):
                        urllib.request.urlopen(f.REDIRECT + '?state=wrong&code=bad')
                    with urllib.request.urlopen(f.REDIRECT + '?' + urllib.parse.urlencode({
                        'state': query['state'][0], 'code': 'temporary-code'})) as response:
                        self.assertEqual(response.status, 200)
                except Exception as error:
                    errors.append(error)
            thread = threading.Thread(target=callback)
            threads.append(thread)
            thread.start()

        with patch.object(f.webbrowser, 'open', side_effect=browser), patch.object(f, 'request_json', return_value={
                'access_token': 'PRIVATE', 'refresh_token': 'REFRESH', 'expires_in': 3600}) as token_exchange:
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                f.login(self.data)
            self.assertNotIn('PRIVATE', stdout.getvalue())
            payload = urllib.parse.parse_qs(token_exchange.call_args[0][2].decode())
            self.assertEqual(payload['code'], ['temporary-code'])
            self.assertIn('code_verifier', payload)
        for thread in threads:
            thread.join(2)
        self.assertFalse(errors, errors)
        self.assertEqual(f.load(self.data / 'tokens.json')['access_token'], 'PRIVATE')

    def test_player_mutation_does_not_parse_optional_response(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = ValueError('Not a JSON document')
        with patch.object(f.urllib.request, 'urlopen', return_value=response):
            self.assertEqual(f.request_json('https://api.spotify.com/v1/me/player/queue', 'POST'), {})
        response.read.assert_not_called()

    def test_rate_limit_no_retry(self):
        error = urllib.error.HTTPError('https://api.spotify.com', 429, 'Limited', {'Retry-After': '30'}, None)
        with patch.object(f.urllib.request, 'urlopen', side_effect=error) as transport:
            with self.assertRaisesRegex(f.Failure, '30 seconds'):
                f.request_json('https://api.spotify.com')
            self.assertEqual(transport.call_count, 1)


if __name__ == '__main__':
    unittest.main()
