import copy
import json
from pathlib import Path
import tempfile
import unittest

import foxtunes as f
import web_server as w
from test_web import FakeSpotify, A

P = 'p'*22
Q = 'q'*22
LOCAL = 'spotify:local:Test:Album:Unique+Local+Recording:10'


class PlaylistSpotify(FakeSpotify):
    def __init__(self):
        super().__init__()
        self.denied = False
        self.legacy = False
        self.summary = {'id':P,'name':'Private test playlist','public':False,'owner':{'display_name':'Host'},'tracks':{'total':4},'external_urls':{'spotify':'https://open.spotify.com/playlist/'+P},'images':[]}

    def call(self,path,method='GET',payload=None,params=None):
        self.calls.append((path,method,params))
        if self.denied:
            raise f.Failure('Spotify HTTP 403. Check account, scopes and device; no automatic retry.')
        if path=='/me/playlists':
            return {'items':[self.summary,None], 'next':'next'}
        if path=='/search':
            return {'playlists':{'items':[None,self.summary],'next':None}}
        if path=='/playlists/'+P:
            return self.summary
        if path=='/playlists/'+P+'/items':
            field='track' if self.legacy else 'item'
            return {'items':[
                {field:{'uri':A,'name':'Playable song','type':'track','artists':[{'name':'Artist'}],'duration_ms':120000,'explicit':False}},
                {field:{'uri':LOCAL,'name':'Unique Local Recording','type':'track','is_local':True,'artists':None,'album':{'name':'Local album','release_date':None,'images':None}},'is_local':True},
                {field:None},
                {field:{'uri':'spotify:episode:'+'e'*22,'name':'Podcast','type':'episode'}}], 'next':'next'}
        return {}


class PlaylistTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.api=PlaylistSpotify()
        self.app=w.App(Path(self.temp.name),self.api)
        self.host, session=self.app.session('');session['host']=True
        self.guest,_=self.app.session('')

    def tearDown(self):
        self.app.db.close();self.temp.cleanup()

    def test_library_and_search_pagination(self):
        data=self.app.list_playlists(self.host,{'offset':['50']})
        self.assertEqual(len(data['items']),1)
        self.assertTrue(data['more'])
        self.assertEqual(data['next_offset'],100)
        self.assertEqual(self.api.calls[-1][2],{'limit':50,'offset':50})
        self.app.list_playlists(self.host,{'q':['metal'],'offset':['10']})
        self.assertEqual(self.api.calls[-1][2],{'q':'metal','type':'playlist','limit':10,'offset':10})
        with self.assertRaises(PermissionError):self.app.list_playlists(self.guest,{})

    def test_private_preview_cache_does_not_authorize_guest(self):
        self.app.playlist_tracks(self.host,P)
        with self.assertRaises(PermissionError):self.app.playlist_tracks(self.guest,P)
        self.assertEqual(self.app.db.execute('SELECT count(*) FROM shared_playlists').fetchone()[0],0)

    def test_enable_browse_request_and_revoke(self):
        self.app.action(self.host,'/api/host/playlist',{'id':P})
        data=self.app.playlist_tracks(self.guest,P)
        self.assertTrue(data['items'][0]['requestable'])
        self.assertTrue(data['items'][1]['is_local'])
        self.assertFalse(data['items'][1]['requestable'])
        self.assertFalse(data['items'][2]['requestable'])
        self.assertFalse(data['items'][3]['requestable'])
        self.app.submit(self.guest,A)
        self.assertEqual(self.app.queue(self.guest)[0]['name'],'Playable song')
        self.assertEqual(self.app.queue(self.guest)[0]['votes'],1)
        self.app.action(self.host,'/api/host/playlist',{'id':P,'remove':True})
        with self.assertRaises(PermissionError):self.app.playlist_tracks(self.guest,P)

    def test_cache_repopulates_catalog_and_keeps_favorites_current(self):
        self.app.action(self.host,'/api/host/playlist',{'id':P})
        calls=len(self.api.calls)
        self.app.action(self.host,'/api/host/favorite',{'uri':A})
        self.app.catalog.clear()
        data=self.app.playlist_tracks(self.guest,P)
        self.assertEqual(len(self.api.calls),calls)
        self.assertTrue(data['items'][0]['house_favorite'])
        self.assertIn(A,self.app.catalog)
        self.assertNotIn(LOCAL,self.app.catalog)

    def test_legacy_track_shape_and_local_request_guard(self):
        self.api.legacy=True
        data=self.app.playlist_tracks(self.host,P)
        self.assertEqual(data['items'][0]['uri'],A)
        self.assertTrue(data['items'][1]['is_local'])
        self.app.catalog[LOCAL]=copy.deepcopy(data['items'][1])
        with self.assertRaises(f.Failure):self.app.submit(self.guest,LOCAL)

    def test_enable_requires_access_and_guests_cannot_enable(self):
        with self.assertRaises(PermissionError):self.app.action(self.guest,'/api/host/playlist',{'id':P})
        self.api.denied=True
        with self.assertRaisesRegex(f.Failure,'Reconnect Spotify'):
            self.app.action(self.host,'/api/host/playlist',{'id':P})
        self.assertEqual(self.app.db.execute('SELECT count(*) FROM shared_playlists').fetchone()[0],0)

    def test_shared_selection_persists_after_restart(self):
        self.app.action(self.host,'/api/host/playlist',{'id':P})
        other=w.App(Path(self.temp.name),self.api)
        try:
            guest,_=other.session('')
            self.assertEqual(other.playlist_tracks(guest,P)['playlist']['name'],'Private test playlist')
            self.assertFalse(other.enabled)
        finally:other.db.close()

    def test_invalid_ids_offsets_and_null_items(self):
        for ident in ('../me','https://example.com',P+'/items'):
            with self.assertRaises(f.Failure):self.app.playlist_tracks(self.host,ident)
        with self.assertRaises(f.Failure):self.app.playlist_tracks(self.host,P,-1)
        with self.assertRaises(f.Failure):self.app.list_playlists(self.host,{'offset':['-1']})

    def test_http_playlist_sharing_and_private_access_boundary(self):
        import http.cookiejar
        import threading
        import urllib.error
        import urllib.request
        server=w.make_server(self.app,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_address[1])
        clients=[urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())) for _ in range(2)]
        def call(client,path,body=None,csrf=''):
            headers={'Content-Type':'application/json','Origin':base,'X-FoxTunes-CSRF':csrf}
            req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
            with client.open(req) as response:return json.load(response)
        try:
            host_state=call(clients[0],'/api/state');guest_state=call(clients[1],'/api/state')
            call(clients[0],'/api/host/login',{'password':self.app.password},host_state['csrf'])
            with self.assertRaises(urllib.error.HTTPError):call(clients[1],'/api/host/playlists')
            self.assertEqual(call(clients[1],'/api/playlists'),{'items':[]})
            call(clients[0],'/api/host/playlists')
            call(clients[0],'/api/playlist-items?id='+P)
            with self.assertRaises(urllib.error.HTTPError):call(clients[1],'/api/playlist-items?id='+P)
            call(clients[0],'/api/host/playlist',{'id':P},host_state['csrf'])
            self.assertEqual(call(clients[1],'/api/playlists')['items'][0]['id'],P)
            self.assertEqual(len(call(clients[1],'/api/playlist-items?id='+P)['items']),4)
            with self.assertRaises(urllib.error.HTTPError):call(clients[1],'/api/host/playlist',{'id':P,'remove':True},guest_state['csrf'])
            call(clients[0],'/api/host/playlist',{'id':P,'remove':True},host_state['csrf'])
            with self.assertRaises(urllib.error.HTTPError):call(clients[1],'/api/playlist-items?id='+P)
        finally:
            server.shutdown();server.server_close();thread.join()
