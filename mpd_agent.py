#!/usr/bin/env python3
"""Restricted SSH command: one bounded JSON request to the loopback MPD instance."""
import base64
import hashlib
import json
import socket
import sys
from pathlib import PurePosixPath


class MPD:
    def __enter__(self):
        self.sock=socket.create_connection(('127.0.0.1',6601),timeout=5)
        self.file=self.sock.makefile('rwb')
        if not self.file.readline().startswith(b'OK MPD '):raise ValueError('Invalid MPD greeting')
        return self

    def __exit__(self,*args):
        self.file.close();self.sock.close()

    def command(self,name,*args):
        if not name.isidentifier():raise ValueError('Invalid command')
        encoded=[]
        for arg in args:
            text=str(arg)
            if any(c in text for c in ('\n','\r','\0')):raise ValueError('Invalid argument')
            encoded.append('"'+text.replace('\\','\\\\').replace('"','\\"')+'"')
        self.file.write((' '.join([name]+encoded)+'\n').encode());self.file.flush()
        rows=[]
        for _ in range(30000):
            line=self.file.readline(16384).decode('utf-8').rstrip('\n')
            if line=='OK':return rows
            if line.startswith('ACK '):raise ValueError('MPD rejected '+name+': '+line.split('}',1)[-1].strip()[:200])
            if not line:raise ValueError('MPD connection closed')
            if line.startswith('binary: '):
                length=int(line.split(': ',1)[1])
                if not 0 <= length <= 65536:raise ValueError('Artwork chunk too large')
                chunk=self.file.read(length)
                if len(chunk)!=length or self.file.read(1)!=b'\n':raise ValueError('Truncated artwork chunk')
                rows.append(('binary',chunk))
            elif ': ' in line:rows.append(tuple(line.split(': ',1)))
        raise ValueError('Library response too large')


def library(mpd):
    records=[];current=None
    for key,value in mpd.command('listallinfo'):
        if key=='file':
            current={'file':value};records.append(current)
        elif current is not None:current.setdefault(key,value)
    songs=[]
    for row in records[:1000]:
        path=PurePosixPath(row['file'])
        if path.is_absolute() or '..' in path.parts or '://' in row['file']:continue
        uri='local:'+hashlib.sha256(row['file'].encode()).hexdigest()
        duration=float(row.get('duration',row.get('Time',0)))
        songs.append({'uri':uri,'source':'local','name':row.get('Title') or path.stem,
                      'artists':row.get('Artist','Unknown artist'),'artist_names':[row.get('Artist','Unknown artist')],
                      'album':row.get('Album',''),'year':row.get('Date','')[:4],
                      'duration_ms':round(duration*1000),'art':'','url':'','explicit':None,'_file':row['file']})
    return songs


MAX_ART = 2 * 1024 * 1024


def picture(mpd, song):
    data = bytearray()
    total = None
    while True:
        response = dict(mpd.command('readpicture', song['_file'], len(data)))
        if not response: return {'data':'','type':''}
        size = int(response.get('size', 0))
        if not 0 < size <= MAX_ART: raise ValueError('Embedded artwork exceeds the 2 MiB limit')
        if total is not None and size != total: raise ValueError('Artwork changed while reading')
        total = size
        chunk = response.get('binary', b'')
        if not isinstance(chunk, bytes) or not chunk or len(data) + len(chunk) > total:
            raise ValueError('Invalid artwork chunk')
        data.extend(chunk)
        if len(data) == total: break
    raw = bytes(data)
    mime = image_type(raw)
    if not mime: raise ValueError('Unsupported artwork format; use JPEG, PNG, GIF or WebP')
    return {'data':base64.b64encode(raw).decode('ascii'), 'type':mime}


def image_type(raw):
    if raw.startswith(b'\xff\xd8\xff'): return 'image/jpeg'
    if raw.startswith(b'\x89PNG\r\n\x1a\n'): return 'image/png'
    if raw.startswith((b'GIF87a',b'GIF89a')): return 'image/gif'
    if raw.startswith(b'RIFF') and raw[8:12] == b'WEBP': return 'image/webp'
    return ''


def execute(mpd,request):
    action=request.get('action')
    if action not in ('library','status','play','pause','stop','scan','picture','volume'):raise ValueError('Unknown action')
    if action=='scan':return {'job':dict(mpd.command('update')).get('updating_db')}
    if action=='library':return {'items':[{k:v for k,v in s.items() if k!='_file'} for s in library(mpd)]}
    if action=='status':
        status=dict(mpd.command('status'));current=dict(mpd.command('currentsong'))
        uri='local:'+hashlib.sha256(current['file'].encode()).hexdigest() if current.get('file') else None
        return {'agent_version':2,'state':status.get('state','stop'),'uri':uri,'song_id':status.get('songid'),
                'progress_ms':round(float(status.get('elapsed',0))*1000),
                'duration_ms':round(float(status.get('duration',0))*1000),'volume':status.get('volume'),
                'error':status.get('error',''),'updating':bool(status.get('updating_db'))}
    if action=='picture':
        song=next((s for s in library(mpd) if s['uri']==request.get('uri')),None)
        if not song:raise ValueError('Local file is not in the scanned library')
        return picture(mpd,song)
    if action=='volume':
        volume=request.get('volume')
        if type(volume) is not int or not 0<=volume<=100:raise ValueError('Invalid local volume')
        mpd.command('setvol',volume)
        return {'volume':int(dict(mpd.command('status')).get('volume',-1))}
    if action=='play':
        uri=request.get('uri');song=next((s for s in library(mpd) if s['uri']==uri),None)
        if not song:raise ValueError('Local file is not in the scanned library')
        volume=request.get('volume',50)
        if not isinstance(volume,int) or not 0<=volume<=100:raise ValueError('Invalid local volume')
        # Dedicated MPD queue: one file, never automatic MPD advancement into another source.
        mpd.command('stop');mpd.command('clear');mpd.command('repeat',0);mpd.command('random',0)
        mpd.command('single',1);mpd.command('replay_gain_mode','track');mpd.command('setvol',volume)
        song_id=dict(mpd.command('addid',song['_file']))['Id']
        mpd.command('playid',song_id)
        return {'song_id':song_id,'uri':uri}
    if action=='pause':mpd.command('pause',1)
    if action=='stop':mpd.command('stop')
    return {'ok':True}


def main():
    try:
        raw=sys.stdin.buffer.readline(8193)
        if len(raw)>8192:raise ValueError('Request too large')
        request=json.loads(raw)
        if not isinstance(request,dict):raise ValueError('JSON object required')
        with MPD() as mpd:result=execute(mpd,request)
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({'error':str(error)[:300]}));return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
