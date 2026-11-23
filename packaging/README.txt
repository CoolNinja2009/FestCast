FestCast {version} ({target})
==============================

Live production control for school festivals and small events: phone cameras, switching, layouts,
recording, TV output and live streaming, all driven through OBS Studio.

Everything FestCast needs is in this folder (its own Python, the built dashboard and MediaMTX).
Nothing is installed on the computer. Keep the folder somewhere writable (Documents, Desktop):
FestCast stores its settings and logs in data\ next to these files.

Requirements
------------
* OBS Studio 30 or newer, with WebSocket server enabled:
  OBS > Tools > WebSocket Server Settings > Enable. If you set a password there, put it in .env
  (FESTCAST_OBS_PASSWORD=...).
* Optional: ffmpeg (live streaming to YouTube/Twitch, MP4 remux), LibreOffice (PowerPoint slides).
    Windows: winget install Gyan.FFmpeg      macOS: brew install ffmpeg      Linux: your package manager
* Phones and TVs must be on the same network as this computer.

Start
-----
Windows   double-click start.bat
macOS     double-click start.command
          The first time, macOS blocks files downloaded from the internet. Open Terminal and run:
              xattr -dr com.apple.quarantine /path/to/FestCast
          (drag the FestCast folder onto the Terminal window to type its path), then double-click again.
Linux     run ./start.sh in a terminal

The dashboard opens in your browser. Keep the start window open during the event: if FestCast
ever crashes it restarts itself there. Closing it stops FestCast (OBS keeps recording).

To try FestCast without OBS, use start-simulation (.bat / .command / .sh): everything is simulated.

The full operator manual is built in: open the dashboard and click "Manual" in the sidebar.

Firewall
--------
Allow FestCast (its Python) and MediaMTX on private networks when the system asks, or phones and TVs
cannot connect. Linux: open the ports shown on the dashboard's Network page.

Updating
--------
Unzip the new version next to the old one and copy the old data\ folder (and .env) into it.

Licence
-------
FestCast is free software under the GNU General Public License v3 (see LICENSE).
Bundled third-party software and its licences are listed in THIRD-PARTY-NOTICES.txt.
