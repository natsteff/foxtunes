#!/usr/bin/env bash
# Run as the same user that runs Soloist/PulseAudio. Does not start music.
set -euo pipefail
if [ "$(id -u)" -eq 0 ]; then
  echo 'Run this as the audio service user, not root, so it shares the user audio session.' >&2
  exit 1
fi
for tool in mpd mpc systemctl; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "Missing $tool. Install the MPD/mpc packages before running this script." >&2
    exit 1
  fi
done
if ! mpd --version | grep -qw pulse; then
  echo 'This MPD build does not advertise the pulse output plugin; stop and inspect before proceeding.' >&2
  exit 1
fi
music_dir="$HOME/Music/UnicornTunes"
config_dir="$HOME/.config/unicorntunes-local"
state_dir="$HOME/.local/share/unicorntunes-local"
unit_dir="$HOME/.config/systemd/user"
mpd_bin="$(command -v mpd)"
mkdir -p "$music_dir" "$config_dir" "$state_dir/playlists" "$unit_dir"
# Preserve any prior trial configuration instead of replacing it silently.
if [ -e "$config_dir/mpd.conf" ] || [ -e "$unit_dir/unicorntunes-local.service" ]; then
  echo 'Existing UnicornTunes local-player configuration found; leaving it unchanged.' >&2
  exit 1
fi
umask 077
cat > "$config_dir/mpd.conf" <<EOF
music_directory "$music_dir"
playlist_directory "$state_dir/playlists"
db_file "$state_dir/database"
state_file "$state_dir/state"
sticker_file "$state_dir/stickers.sqlite3"
bind_to_address "127.0.0.1"
port "6601"
auto_update "yes"
restore_paused "yes"
follow_outside_symlinks "no"
audio_output {
  type "pulse"
  name "UnicornTunes local music"
  mixer_type "software"
}
EOF
cat > "$unit_dir/unicorntunes-local.service" <<EOF
[Unit]
Description=UnicornTunes local music player (MPD)
After=pulseaudio.service

[Service]
Type=simple
ExecStart="$mpd_bin" --no-daemon "$config_dir/mpd.conf"
Restart=on-failure
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now unicorntunes-local.service
ready=0
for attempt in {1..20}; do
  if mpc --host 127.0.0.1 --port 6601 status >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.25
done
if [ "$ready" -ne 1 ]; then
  echo 'MPD did not become ready. Inspect: journalctl --user -u unicorntunes-local.service -n 30 --no-pager' >&2
  exit 1
fi
mpc --host 127.0.0.1 --port 6601 update
printf '\nLocal player started without playing music. Upload files to: %s\n' "$music_dir"
printf 'List tracks: mpc --host 127.0.0.1 --port 6601 listall\n'
printf 'Soloist was not changed. Local control port is loopback-only.\n'
