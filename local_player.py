"""Optional local playback provider. No hardcoded player address or shared shell access."""
import json
import re
import subprocess
import time

import foxtunes as f

LOCAL=re.compile(r'^local:[a-f0-9]{64}$')


class RemoteMPD:
    def __init__(self,data,config):
        self.data,self.config=data,config

    def call(self,action,**args):
        host=self.config.get('host','');user=self.config.get('user','')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]{0,252}',host) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',user):
            raise f.Failure('Configure the local-player hostname and SSH user in the host desk.')
        port=int(self.config.get('port',22))
        if not 1<=port<=65535:raise f.Failure('Invalid local-player SSH port.')
        key=self.data/'local-player.key';known=self.data/'local-player.known_hosts'
        if not key.exists() or not known.exists():raise f.Failure('Install the dedicated player key and verified host key using the local-player setup guide.')
        command=['ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=5','-o','StrictHostKeyChecking=yes',
                 '-o','IdentitiesOnly=yes','-o','GlobalKnownHostsFile=/dev/null','-o','UserKnownHostsFile='+str(known),
                 '-o','ControlMaster=auto','-o','ControlPersist=30','-o','ControlPath=/tmp/unicorntunes-mpd-%C',
                 '-i',str(key),'-p',str(port),user+'@'+host,'unicorntunes-mpd']
        try:
            result=subprocess.run(command,input=json.dumps(dict(action=action,**args))+'\n',text=True,capture_output=True,timeout=12)
            if len(result.stdout)>4_000_000:raise f.Failure('Local library response is too large.')
            if not result.stdout.strip():raise f.Failure('Local-player SSH connection failed. Check its address, dedicated key and pinned host key.')
            payload=json.loads(result.stdout)
            if result.returncode or payload.get('error'):raise f.Failure('Local player: '+str(payload.get('error','command failed'))[:300],uncertain=action in ('play','pause','stop'))
            return payload
        except (OSError,subprocess.TimeoutExpired,ValueError) as error:
            raise f.Failure('Local-player connection/response failed; check the player before retrying playback.',uncertain=action in ('play','pause','stop')) from error


class LocalPlayback:
    def local_init(self):
        path=self.data/'local-player.json'
        self.local_config=f.load(path) if path.exists() else {'enabled':False,'host':'','user':'','port':22,'device':'','volume':50}
        self.local=RemoteMPD(self.data,self.local_config)
        self.db.execute('CREATE TABLE IF NOT EXISTS local_tracks(uri TEXT PRIMARY KEY,metadata TEXT NOT NULL)')
        self.db.commit()
        self.local_error=''
        self.player_missing=False

    def local_bound(self):
        return bool(self.local_config.get('enabled') and self.device and self.local_config.get('device')==self.device)

    def scan_local(self):
        if not self.local_config.get('enabled'):raise f.Failure('Enable and configure the local player first.')
        self.local.call('scan')
        for _ in range(20):
            if not self.local.call('status').get('updating'):break
            time.sleep(.25)
        else:raise f.Failure('Local library scan is still running; try again shortly.')
        result=self.local.call('library')
        songs=result.get('items',[])
        if not isinstance(songs,list) or len(songs)>1000:raise f.Failure('Local library must contain at most 1000 indexed tracks.')
        cleaned=[]
        for song in songs:
            if not isinstance(song,dict) or not LOCAL.fullmatch(str(song.get('uri',''))):raise f.Failure('Invalid local track returned by player.')
            cleaned.append(dict(song,source='local',url='',art=''))
        with self.db:
            self.db.execute('DELETE FROM local_tracks')
            self.db.executemany('INSERT INTO local_tracks VALUES(?,?)',[(s['uri'],json.dumps(s)) for s in cleaned])
        self.local_error=''
        return {'count':len(cleaned)}

    def local_search(self,query,offset):
        terms=query.get('q',[''])[0].strip().casefold().split()
        title=query.get('track',[''])[0].strip().casefold();artist=query.get('artist',[''])[0].strip().casefold()
        album=query.get('album',[''])[0].strip().casefold();year=query.get('year',[''])[0].strip()
        exact=query.get('exact',['0'])[0]=='1'
        if exact and not title:raise f.Failure('Enter a title for exact matching.')
        matches=[]
        for row in self.db.execute('SELECT metadata FROM local_tracks ORDER BY uri'):
            song=json.loads(row[0]);name=song.get('name','').casefold()
            hay=' '.join(str(song.get(k,'')) for k in ('name','artists','album','year')).casefold()
            if any(t not in hay for t in terms):continue
            if title and (name!=title if exact else title not in name):continue
            if artist and artist not in song.get('artists','').casefold():continue
            if album and album not in song.get('album','').casefold():continue
            if year:
                bounds=year.split('-')
                if len(bounds)>2 or any(not re.fullmatch(r'\d{4}',v) for v in bounds) or bounds[0]>bounds[-1]:raise f.Failure('Use a four-digit year or year range.')
                if not bounds[0]<=song.get('year','')<=bounds[-1]:continue
            song['house_favorite']=bool(self.db.execute('SELECT 1 FROM favorites WHERE uri=?',(song['uri'],)).fetchone())
            matches.append(song)
        matches.sort(key=lambda s:(s['name'].casefold(),s['uri']))
        return matches[offset:offset+10],len(matches)>offset+10

    def local_action(self,sid,route,body):
        if route=='/api/host/local-config':
            config={'enabled':bool(body.get('enabled')),'host':str(body.get('host','')).strip(),
                    'user':str(body.get('user','')).strip(),'port':int(body.get('port',22)),
                    'device':str(body.get('device','')),'volume':int(body.get('volume',50))}
            if not 1<=config['port']<=65535 or not 0<=config['volume']<=100:raise f.Failure('Invalid port or local volume.')
            if config['enabled']:
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]{0,252}',config['host']) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',config['user']):raise f.Failure('Enter a valid player hostname/IP and SSH username.')
                if not config['device']:raise f.Failure('Select the matching Spotify playback device before binding local playback.')
            f.private_write(self.data/'local-player.json',config)
            self.local_config=config;self.local=RemoteMPD(self.data,config)
            return {'ok':True}
        if route=='/api/host/local-scan':return self.scan_local()
        if route=='/api/host/local-test':return self.local.call('status')
        return None

