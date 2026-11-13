# Local music playback

Soloist handles Spotify audio. A separate MPD user service on the same playback device handles uploaded music. MPD stays on loopback port 6601 and shares the user's PulseAudio output.

The host desk tests the connection and indexes the library. Guests can search Spotify and uploaded local tracks together, or select either source. Local results are marked **Local file** and use the same request, vote and fair-queue rules as Spotify results. Installing the connection does not start music or change Soloist.

First follow [the playback-device setup guide](SOLOIST_DEVICE_SETUP.md) to install MPD and create `unicorntunes-local.service`. Upload music under `$HOME/Music/UnicornTunes` on the playback device.

## 1. Create a dedicated key on the Docker server

From the directory containing your Compose file:

```sh
docker compose exec foxtunes ssh-keygen -t ed25519 -N '' -f /data/local-player.key
docker compose exec foxtunes cat /data/local-player.key.pub
```

Do not overwrite an existing key. Copy only the displayed **public** key to a file named `unicorntunes-local.pub` on the playback device. The private key stays in the Docker data volume.

## 2. Authorize the key on the playback device

Run as the same audio user that runs Soloist and MPD:

```sh
curl -fL https://github.com/natsteff/unicorntunes/archive/refs/heads/main.tar.gz -o /tmp/unicorntunes-source.tar.gz
tar -xzf /tmp/unicorntunes-source.tar.gz -C /tmp
python3 /tmp/unicorntunes-main/deploy/authorize-local-player.py < unicorntunes-local.pub
```

This installs a small agent under `$HOME/.local/lib/unicorntunes/`. Its SSH key can only invoke the agent; SSH shell, port forwarding and PTY are disabled. The agent accepts library/status/scan and bounded player commands, connecting only to local MPD. Do not open MPD's port to the LAN.

## 3. Pin the playback device's SSH host key

On the playback device, display its host fingerprint:

```sh
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

On the Docker server, replace `PLAYER_ADDRESS` with the playback device's IP or resolvable hostname:

```sh
docker compose exec foxtunes sh -c 'ssh-keyscan -t ed25519 PLAYER_ADDRESS > /data/local-player.candidate'
docker compose exec foxtunes ssh-keygen -lf /data/local-player.candidate
```

Compare the fingerprints. Only if they match:

```sh
docker compose exec foxtunes mv /data/local-player.candidate /data/local-player.known_hosts
```

For a nonstandard SSH port add `-p PORT` to `ssh-keyscan`. A changed address may require pinning its host key under the new address again; verify the fingerprint each time.

## 4. Test from the host desk

Open `/host`, sign in, and expand **Local music player**. Enter the playback device's address, audio username and SSH port. Select the matching Spotify playback device, enable the connection, and save. Press **Test connection**, then **Scan local library**. Successful results show MPD's state and an indexed track count.

Prefer a stable hostname or DHCP reservation. Connection settings and keys persist in `/data`; the Mac is not required for normal operation. Back up that volume securely.

The default local volume of 50 is a trial setting. ReplayGain can use existing track tags, but loudness analysis and matching Spotify volume remain pending. Avoid treating a percentage as normalized loudness.

## Mixed playback

Start a session with the matching Spotify playback device selected. Local files are bound to that device, so selecting another location does not route its requests to the wrong speakers. A session can begin with a local request while Spotify is idle, provided the selected Spotify device is available.

When the next request is local, UnicornTunes waits for the current Spotify track to end. It confirms Spotify is paused before asking the dedicated MPD queue to play one file. Brief gaps or a short start of Spotify's next background track can occur while the end is observed. MPD has single-track mode enabled, so the application chooses the next request across both sources. After a local track ends, the next Spotify request starts directly on the selected Spotify device. With no requests left, playback remains idle; a newly requested track starts automatically on its own source. Background Spotify contexts are not automatically resumed.

**Pause playback** pauses the current source and disables automatic handoff. **Skip to next request** stops the owned local item before starting a replacement. **Stop session** disables further handoff while allowing the current track to continue. Restarting a session during a local track stops that track before activating Spotify. A server restart preserves local ownership but leaves automatic handoff disabled.

Unconfirmed starts, unexpected MPD item changes, early stops and Spotify device changes hold further dispatch for host review. A lost start response is not automatically retried. Reconciliation of an unresolved local request stops the owned MPD track before releasing it. If MPD is playing a different item, check it and stop it manually first. A local track too short to be observed, or missing duration metadata, may need host review at its end.

Local files use their available tags, with the filename as the title fallback. Spotify's playlist-local entries remain inspection-only: these are separate from the uploaded MPD library and are not matched by title. Content rules apply to both sources; when explicit content is blocked, local tracks with unknown content ratings are blocked as well.

### First playback test

1. Keep the local trial volume conservative and verify **FoxTunes** is the linked Spotify device.
2. Search for a Spotify track, request it, then request one short uploaded file using **Local files only**.
3. Request another Spotify track after the local one. Listen for Spotify → local → Spotify, with no simultaneous audio.
4. Let the requests run out, then add another local or Spotify request and verify it starts.

The mixed source state machine is covered by automated tests. Audible transitions and volume balance still need validation on your playback hardware.
