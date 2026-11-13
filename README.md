# FoxTunes

**Docker Compose deployment:** see [DEPLOY.md](DEPLOY.md) for the private image, Nginx Proxy Manager, persistent data and server-side Spotify sign-in.

A local command-line experiment for Spotify host authentication, simulated anonymous requests, device discovery and playback observations. Requires Python 3.9+; no packages to install. This is an early test tool, not the full jukebox or a phone-accessible PWA.

## Configure Spotify

1. In the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard), create an app named **FoxTunes**. Suggested description: **Private local music-request feasibility test**. Select **Web API** if prompted.
2. Register this redirect URI exactly: **http://127.0.0.1:8765/callback**.
3. Save the app and copy its **Client ID**. A client secret is not needed: the harness uses [Authorization Code with PKCE](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow).
4. From this folder, run:

```sh
python3 foxtunes.py setup
python3 foxtunes.py login
```

Paste the Client ID into the local terminal prompt. Login opens Spotify in the browser and waits three minutes for the callback. Use the same computer for browser and CLI. Port 8765 must be available. `login --no-browser` prints a temporary authorization URL for manual opening on that computer; keep that URL private.

The host needs Premium. If an additional account is used, check the app's allowed users. Scopes requested are playback read, currently playing read and playback modify. Tokens stay in ignored local files with owner-only permissions. Never paste credentials into chat or commit runtime files.

## First live test

Open the official Spotify app on a computer or another Connect device, play a song, and keep it awake. Start at low volume. No operating-system reinstall is required.

```sh
python3 foxtunes.py devices
python3 foxtunes.py search "song artist"
```

Record a nonrestricted device ID and choose three different track URIs from search results. Replace the capitalized placeholders below with actual values; they are not runnable examples as written.

```sh
python3 foxtunes.py three-guests TRACK_URI_A TRACK_URI_B TRACK_URI_C
python3 foxtunes.py status
python3 foxtunes.py transfer --device DEVICE_ID --confirm
python3 foxtunes.py observe
python3 foxtunes.py dispatch --device DEVICE_ID
python3 foxtunes.py spotify-queue
python3 foxtunes.py observe
```

`three-guests` simulates three independent guest labels locally. It needs no Spotify login and saves all three requests atomically. It does not demonstrate remote browser sessions, authentication isolation over a network, or music playback. Alternatively use `submit --guest guest-1 TRACK_URI`.

`dispatch` sends the oldest requested track, one at a time, to Spotify's existing queue. It does not start that track immediately. Wait for it to become current, then run `observe` again. Only a matching track playing on its selected device changes the request to `observed-playing`; completion and audible output are never inferred. After that observation, dispatch the next request. Existing Spotify queue items may play first. Do not clear a personal queue implicitly or assume the test controls external clients.

Transfer and `skip --device DEVICE_ID --confirm` can interrupt playback. Their results report command acceptance; always observe the result. The local worker never sends mutations automatically or retries queue additions. An accepted or uncertain dispatch blocks another until observed or manually reconciled. `spotify-queue` records `queued-observed` for a uniquely matching accepted request when the active device is stable across the read; this permits dispatching the next request. It does not prove playback.

If a request fails or remains uncertain, inspect the actual Spotify queue and current playback. Once reconciled, use:

```sh
python3 foxtunes.py resolve REQUEST_ID --note "What was checked and why it is safe to close"
```

This manually closes the attempt without asserting played/completed. Re-submit only after checking for an existing remote queue entry. An interrupted `dispatching` attempt also requires reconciliation. A 429 error requires waiting; quota exhaustion may last longer than the rate-limit interval. Authentication or device failures require correcting the setup, not repeatedly sending playback commands.

## Dedicated player test

Once the existing-device test works, configure [Spotify Soloist](https://developer.spotify.com/documentation/soloist) on a supported audio computer using its separate private API key and the official pairing instructions. Select it in Spotify and repeat `devices`, transfer and the request test above. This harness tests Soloist through the Web API as a Connect target; direct Soloist WebSocket control is not implemented. Download Soloist directly from Spotify; no player binary is bundled here.

## Local verification

```sh
python3 -m unittest discover -s tests -v
```

Tests use temporary databases and mocked Spotify transport. They validate persistence, duplicates, credential handling and conservative failure/observation behavior. They do not validate Spotify account access, devices, audio, actual track order, browser isolation, or Docker deployment.

## Disconnect and data

`python3 foxtunes.py logout` removes stored local tokens. Revoke FoxTunes in Spotify account settings to invalidate access fully. Runtime config, tokens, requests and playback observations are stored locally under `.local-work/runtime/`; these contain private account/activity data. Keep the directory out of backups or protect backups appropriately. The CLI binds only its temporary OAuth callback to loopback and serves no guest interface. Do not expose it as a network service.

## Browser prototype

Start the web test on the computer where the host Spotify login was saved:

```sh
python3 web_server.py
```

Open **http://127.0.0.1:8863** for the guest interface. Search songs/artists, page through results, request tracks and vote. Guests have no playback, device-selection or removal permission. One vote per browser session per request; ties keep request order. Each session can have three waiting requests. Sessions expire after eight hours and are not permanent identities; clearing cookies can create a new session, so this is a trusted-party prototype rather than strong abuse prevention.

Host page: **http://127.0.0.1:8863/host**. The first launch generates a private host password. Read it locally:

```sh
python3 -c 'import json; print(json.load(open(".local-work/runtime/web-host.json"))["password"])'
```

Sign in on `/host`. This password protects the host desk; it is separate from Spotify OAuth. Host login does not expose Spotify tokens to the browser. Select the active device and apply settings. Host-only actions include disabling new requests, removing waiting items, pausing/skipping and enabling automatic handoff. Pause/skip turn handoff off. Locked requests cannot be removed from Spotify through this prototype. Reconcile an uncertain/accepted handoff only after checking the actual Spotify queue; it closes the local attempt without asserting completed playback, and stops automatic handoff.

**Handoff defaults OFF on every server start.** To test it, turn off shuffle/repeat in Spotify, start playback on the selected device, select that device in the host desk, check the handoff box and apply. Within the current song's last 15 seconds, FoxTunes sends one highest-voted waiting request to Spotify. The request locks immediately; the remainder stays local and can be voted up. It sends no additional request until the handed-off track is actually observed playing. Network/API errors or device takeover stop the worker. Uncertain mutations are never automatically retried. The host must reconcile and deliberately re-enable after a failure.

The upcoming panel shows FoxTunes requests, not every item/context already in Spotify. Existing Spotify queue entries, other clients, gaps in polling, unsupported devices and API quotas can affect order/timing. A 15-second handoff is an experimental threshold, not a gapless-playback guarantee. A track leaving playback is recorded as no longer current, not proof it completed. The service polls Spotify once per 20 seconds while idle and once per 5 seconds during handoff; browser refreshes use the shared cached state. A failure backs off for 60 seconds and disables handoff.

### Phones on the same trusted LAN

Stop the loopback server first. Replace the sample IP with this computer's actual LAN IP:

```sh
python3 web_server.py --bind 0.0.0.0 --lan-host 192.168.1.50
```

Phones on the same network open **http://192.168.1.50:8863**. Bind only for a deliberate LAN test; do not forward the port to the internet. Plain HTTP is for the trusted-LAN experiment: host passwords and session traffic are not encrypted. Prefer host sign-in through loopback on the server computer. Guest isolation/router settings may prevent phones from reaching it. HTTPS, PWA installation, QR access, Docker deployment and dedicated-player setup remain separate steps; no public hosting is configured.

The web prototype uses its own `web.sqlite3` so earlier CLI experiments remain intact. Run one server instance per database; in-process session/worker locks are not a multi-process deployment design. Server restarts retain requests/votes but expire browser sessions and disable handoff. Stop with Control-C. Leave the CLI idle while the browser server uses the same Spotify credentials to avoid concurrent refresh operations.

## Starting a dedicated player (v0.1.2+)

On the host desk, select the paired, online Soloist/Connect device and click **Start session**. FoxTunes transfers playback and verifies the target is playing before enabling request handoff. If the player has no playback context, add a request first; FoxTunes uses the highest-voted request as its first track. Start also disables shuffle/repeat. **Stop session** stops new handoffs without stopping the audio. **Move / restart session here** starts the selected destination. These controls replace the earlier handoff checkbox and Apply settings workflow described above. No Spotify desktop app is required for normal startup. Live behavior depends on the account's device-control permissions and the player being available; local control is not implemented as a fallback.
