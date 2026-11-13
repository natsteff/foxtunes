# UnicornTunes room features (0.2.0)

## Guest identity and attribution

Phones remember a private guest identity for a year using an HttpOnly, SameSite cookie; the database stores a hash of its random token. Host authorization remains a separate, expiring session and is never restored by a guest cookie. Clearing cookies or using another device creates another identity. Display names are not authentication, and anonymous guests cannot be made cheat-proof without accounts.

The guest page asks for a name once. Skipping uses the literal `<Shy guy - name not provided>`. Requests and additional votes retain the name used when submitted. Changing your name affects future attribution. Your requests are highlighted and labelled in the shared queue. The requester contributes the initial vote and cannot vote twice. Existing requests receive their initial vote during migration; legacy names remain unknown.

`/display` is a touch-oriented view for the tablet/Pine64 enclosure. It keeps a join QR visible and asks for a fresh guest name before each request/reaction so one visitor does not silently inherit another visitor's identity. For recurring personal identity and voting, guests should scan the QR and use their own phones. Switching guests does not sign a host out: sign out before leaving a shared tablet unattended.

## Fair queue

Fair mode defaults on. Each guest's first request per gathering gets a protected first turn. First turns are served in arrival order ahead of ordinary vote-ranked requests, except that every third selection takes the oldest waiting request. Subsequent requests are ranked by votes, with arrival order breaking ties.

One next request is reserved immediately and cannot be bumped by later votes or new arrivals. Reservations persist across restarts. Queue positions behind it show the projected selection order. The selection counter advances atomically when a request is committed for sending, before the Spotify operation. Failed/uncertain operations hold that selection for host reconciliation rather than retrying it.

The protected slot is the next **UnicornTunes request**, not a promise that it is the next item in Spotify's independent queue. Existing Spotify tracks can precede it. Host removal can cancel a waiting reservation, while handed-off requests require reconciliation. Turning fair mode off restores vote ranking behind the protected next request.

**New gathering** stops handoff, archives waiting requests, resets first-turn eligibility and the three-selection cycle, and starts a new Tonight period. It preserves history and the currently playing track. It refuses to reset while a handoff remains unresolved. Restarting the server never resets the gathering or resumes handoff automatically.

## Host configuration

Authenticated `/host` users can:

- Save named playback locations, select a player, and start/move the existing shared session there.
- Curate House Favorites from search. Saving a favorite does not request it. Guests can browse/request all favorites ordered by counted plays; the host can remove favorites.
- Set explicit-content filtering, maximum length, blocked track names/URIs, blocked artist names, and repeat cooldown. Zero disables a numeric limit. Missing content ratings/durations fail closed when the respective restriction is enabled. Rules apply to favorites and are rechecked before dispatch. Newly blocked waiting songs remain visible for host removal; they cannot dispatch. Already handed-off Spotify songs cannot be recalled by a rule change.
- Set a theme banner and suggested searches. Themes are invitations, not automatically enforced genre restrictions.
- Customize all five text areas, select suggested wording, hide the eyebrow, or restore the original wording. All five original defaults are retained. Custom text is rendered as text, never HTML.
- Set the timezone used for month boundaries. Default: America/Chicago.

The header displays the build's actual version, supplied to Docker through APP_VERSION at release time. Source runs show `0.3.0-dev` unless UNICORNTUNES_VERSION is set.

## Observations, history, and stats

History starts when this version is installed; older listening is not invented. We record observed starts, plausible progress between fresh polls, host skips, end-near-finish transitions, and unknown outcomes. Seeks, long polling gaps, server downtime, and playback on another selected device do not earn listening credit. A counted play requires 30 observed seconds, or half the duration for a track shorter than 60 seconds. Counted does not mean completed or audibly verified. An end near the duration is labelled `ended-near-finish`, not proof of a full listen.

Room stats includes recent observations, Top 40 counted plays, additional upvotes, distinct-supporter crowd favorites, users ranked by plays or additional votes received, personal request history, a recap, weekly most-supported tracks, a monthly anthem, and a discovery spotlight. The spotlight is the played request with most additional votes and no earlier counted play in recorded history. Identical provided names are combined for user leaderboards; unnamed guests remain in history but are excluded from that leaderboard.

Tonight means the current host-started gathering, week means the last seven days, month means the current calendar month in the host's timezone, and All time covers retained data. Aggregate rankings cover the whole period; recent observations and personal history each show up to 100 entries. Recent history can be downloaded as text, and the recap copied for sharing. Reactions allow one current choice per guest per observed track, independently of queue votes. A phone receives an in-page notice when its request becomes the protected next request.

## Playback health and Spotify upcoming

The guest view distinguishes requests open/paused, handoff status, playback pause, no active player, and stale observations. The host gets device/reconnection guidance and existing refresh/start/reconciliation controls. A collapsed read-only Spotify queue view uses one shared cache refreshed at most once per 30 seconds, not one API request per guest refresh. Errors retain the last successful timestamp rather than implying a fresh snapshot. These tracks have no guest votes or removal controls.

## Upgrade and validation

Use `ghcr.io/natsteff/unicorntunes:latest` or the numbered `0.2.0` tag. Existing volumes, credentials, queue and configuration are retained. Before migrating a database with requests, the application creates a private `web-pre-0.2.0.sqlite3` backup in the data directory. Host handoff starts stopped after restart.

Automated tests cover identity persistence, authorization, vote attribution, guaranteed turns, third-oldest selection, protected reservations, failed-operation holds, migration, music rules, favorites, stats, reactions, locations and observed play counts. Browser checks cover real Spotify search, guest onboarding, host configuration save, Room stats, and phone/tablet layouts. Audible long-run playback acceptance remains to be verified on the deployment.


## Host-selected playlists (0.3.0)

Hosts can browse their Spotify library, filter loaded pages, search Spotify playlists or open a playlist link/ID. Enabling a playlist verifies contents access and shares its metadata/tracks with guests; disabling it blocks further guest reads even while a cached page exists. Guests browse enabled playlists and request individual supported tracks through the existing queue. Local-file entries remain visible but cannot be requested until playback support is established.

Existing Spotify authorization must be renewed for playlist-read-private and playlist-read-collaborative. Spotify may limit track access to owned/collaborative playlists. Playlist discovery does not override that restriction. This feature provides no playlist downloads or MP3 transfer.


## Empty-queue recovery (0.3.1)

An enabled session remains ready after an observed track end. A fresh selected-device observation near the end followed by an elapsed, exhausted/empty playback state arms idle recovery. When a waiting request arrives, the worker starts the protected next request directly on the selected device; inserting it into Spotify's queue alone cannot restart an exhausted player. Acceptance still waits for observed playback, and uncertain starts are held without automatic retries.

Host Pause/Stop and mid-track pauses do not trigger recovery. Other active devices are not taken over; server restarts still require host Start. A stale observation is not treated as proof of exhaustion. Spotify polling cannot perfectly distinguish a manual stop exactly at the end from a natural ending. No automatic background playlist is selected when there are no requests.
