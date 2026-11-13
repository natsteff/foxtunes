# Local music player connection trial

Soloist handles Spotify audio. A separate MPD user service on the same playback device handles uploaded music. MPD stays on loopback port 6601 and shares the user's PulseAudio output.

**Current stage:** the host desk can test the connection and index the library. Local tracks are not yet included in guest search or mixed playback. Installing this connection does not start music or change Soloist.

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

Open `/host`, sign in, and expand **Local music player · connection test**. Enter the playback device's address, audio username and SSH port. Select the matching Spotify playback device, enable the connection, and save. Press **Test connection**, then **Scan local library**. Successful results show MPD's state and an indexed track count.

Prefer a stable hostname or DHCP reservation. Connection settings and keys persist in `/data`; the Mac is not required for normal operation. Back up that volume securely.

The default local volume of 50 is a trial setting. ReplayGain can use existing track tags, but loudness analysis and matching Spotify volume remain pending. Avoid treating a percentage as normalized loudness.
