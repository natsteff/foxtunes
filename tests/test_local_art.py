import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import foxtunes as f
import mpd_agent
import web_server as w

URI='local:'+'a'*64
IMAGE=b'\xff\xd8\xfftest-jpeg'


class Duplex:
    def __init__(self,response):self.response=io.BytesIO(response);self.sent=io.BytesIO()
    def write(self,data):return self.sent.write(data)
    def flush(self):pass
    def readline(self,n=-1):return self.response.readline(n)
    def read(self,n):return self.response.read(n)


class ArtworkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.app=w.App(Path(self.temp.name),Mock())
        self.app.local_config['enabled']=True;self.app.local=Mock()
        self.app.db.execute('INSERT INTO local_tracks VALUES(?,?)',(URI,json.dumps({'uri':URI})));self.app.db.commit()
    def tearDown(self):self.app.db.close();self.temp.cleanup()

    def test_binary_response_parser_keeps_raw_bytes(self):
        raw=b'\xff\x00\nOK\n\xfe'
        mpd=mpd_agent.MPD();mpd.file=Duplex(b'size: 8\nbinary: '+str(len(raw)).encode()+b'\n'+raw+b'\nOK\n')
        result=dict(mpd.command('readpicture','Song "quote".mp3',0))
        self.assertEqual(result['binary'],raw)
        self.assertIn(b'\\"quote\\"',mpd.file.sent.getvalue())

    def test_binary_parser_rejects_truncated_and_large_chunks(self):
        for response in [b'binary: 4\nabc',b'binary: 999999\n']:
            mpd=mpd_agent.MPD();mpd.file=Duplex(response)
            with self.assertRaises(ValueError):mpd.command('readpicture','song.mp3',0)

    def test_embedded_artwork_reads_multiple_chunks(self):
        mpd=Mock();mpd.command.side_effect=[ [('size',str(len(IMAGE))),('binary',IMAGE[:5])], [('size',str(len(IMAGE))),('binary',IMAGE[5:])] ]
        result=mpd_agent.picture(mpd,{'_file':'song.mp3'})
        self.assertEqual(base64.b64decode(result['data']),IMAGE);self.assertEqual(result['type'],'image/jpeg')
        self.assertEqual(mpd.command.call_args_list[1].args[-1],5)

    def test_missing_and_oversized_artwork(self):
        mpd=Mock();mpd.command.return_value=[]
        self.assertEqual(mpd_agent.picture(mpd,{'_file':'song.mp3'})['data'],'')
        mpd.command.return_value=[('size',str(mpd_agent.MAX_ART+1)),('binary',b'a')]
        with self.assertRaises(ValueError):mpd_agent.picture(mpd,{'_file':'song.mp3'})

    def test_artwork_is_cached_and_magic_validated(self):
        self.app.local.call.return_value={'data':base64.b64encode(IMAGE).decode(),'type':'text/html'}
        for _ in range(2):self.assertEqual(self.app.local_artwork('a'*64),('image/jpeg',IMAGE))
        self.assertEqual(self.app.local.call.call_count,1)
        with self.assertRaises(LookupError):self.app.local_artwork('../../etc/passwd')
        with self.assertRaises(LookupError):self.app.local_artwork('b'*64)

    def test_missing_artwork_negative_cache_and_invalid_data(self):
        self.app.local.call.return_value={'data':'','type':''}
        for _ in range(2):self.assertEqual(self.app.local_artwork('a'*64),('',b''))
        self.assertEqual(self.app.local.call.call_count,1)
        self.app.db.execute('DELETE FROM local_art');self.app.db.commit()
        self.app.local.call.return_value={'data':base64.b64encode(b'<svg onload=bad>').decode()}
        with self.assertRaises(f.Failure):self.app.local_artwork('a'*64)

    def test_volume_persists_without_starting_playback(self):
        result=self.app.apply_local_volume(75)
        self.assertEqual(result,{'volume':75,'live':False});self.app.local.call.assert_not_called()
        self.assertEqual(json.loads((Path(self.temp.name)/'local-player.json').read_text())['volume'],75)
        for invalid in [-1,101,True,75.5,'75']:
            with self.assertRaises(f.Failure):self.app.apply_local_volume(invalid)

    def test_live_volume_checks_ownership_and_confirms_result(self):
        self.app.local_active={'id':1,'uri':URI,'song_id':'7'}
        self.app.local.call.side_effect=[{'uri':URI,'song_id':'7','state':'play'},{'volume':75}]
        self.assertTrue(self.app.apply_local_volume(75)['live'])
        self.app.local.call.side_effect=[{'uri':URI,'song_id':'8','state':'play'}]
        with self.assertRaises(f.Failure):self.app.apply_local_volume(80)
        self.assertEqual(self.app.local_config['volume'],75)

    def test_agent_volume_is_bounded_and_confirmed(self):
        mpd=Mock();mpd.command.return_value=[('volume','75')]
        self.assertEqual(mpd_agent.execute(mpd,{'action':'volume','volume':75}),{'volume':75})
        self.assertEqual(mpd.command.call_args_list[0].args,('setvol',75))
        with self.assertRaises(ValueError):mpd_agent.execute(mpd,{'action':'volume','volume':True})

    def test_live_volume_is_host_only(self):
        sid,_=self.app.session('')
        with self.assertRaises(PermissionError):self.app.action(sid,'/api/host/local-volume',{'volume':75})

    def test_http_artwork_serves_binary_with_caching(self):
        import threading
        import urllib.request
        self.app.local.call.return_value={'data':base64.b64encode(IMAGE).decode()}
        server=w.make_server(self.app,port=0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_address[1])
        try:
            for _ in range(2):
                with urllib.request.urlopen(base+'/api/local-art/'+'a'*64) as response:
                    self.assertEqual(response.headers['Content-Type'],'image/jpeg')
                    self.assertIn('max-age',response.headers['Cache-Control']);self.assertEqual(response.read(),IMAGE)
            self.assertEqual(self.app.local.call.call_count,1)
        finally:server.shutdown();server.server_close();thread.join()

    def test_artwork_ssh_does_not_block_playback_lock(self):
        import threading
        entered=threading.Event();finish=threading.Event()
        def blocked(*args,**kwargs):
            entered.set();finish.wait(2)
            return {'data':''}
        self.app.local.call.side_effect=blocked
        thread=threading.Thread(target=lambda:self.app.local_artwork('a'*64));thread.start()
        try:
            self.assertTrue(entered.wait(1))
            acquired=self.app.lock.acquire(timeout=.2)
            self.assertTrue(acquired)
            if acquired:self.app.lock.release()
        finally:finish.set();thread.join()
