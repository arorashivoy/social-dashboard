# Social dashboard ("Cutting Room")

A dashboard on the Raspberry Pi that publishes a clip as an Instagram Reel and a YouTube Short, and shows the analytics.

- **Address:** `https://social.arorashivoy.win` (Cloudflare Access, email code), `http://100.75.113.105:8097` (Tailscale)
  or `http://192.168.0.200:8097` (home network).
- **Clip folder:** `/mnt/storage/social-clips/inbox`. After a full upload, the clip moves to `posted/`.
- **Service:** `systemctl --user restart social-dashboard` (unit in `~/.config/systemd/user`).
  The template of the unit is `systemd/social-dashboard.service`. Copy it and put the AUD tag in it.
- **Secrets and state:** `data/config.json` and `data/state.json` (mode 600). Do not commit `data/`.

## How it works

1. Copy a vertical MP4 or MOV clip to the inbox.
2. Write one line about the clip and select **Suggest 3 captions**. The server runs `claude -p` on 4 frames
   of the clip. The tools `humanize.py` and `caption.py` of the `ig-caption` and `ig-human` skills clean and lint the text.
3. Select one option (or write your own), edit it, and select **Upload**.

- **YouTube:** resumable upload with the YouTube Data API v3. Stats come from the Data API and the Analytics API.
- **Instagram:** Instagram API with Instagram login (`graph.instagram.com`). This API reads the video from a public
  address. During the upload the server opens a Cloudflare quick tunnel to port 8098. That port serves only the one
  clip, under a random token. The tunnel closes when the Reel is published.
  The server always uses this public link. The direct upload to `rupload.facebook.com` always failed, so it is removed.

The steps for the Instagram token and the YouTube client are on the **Setup** tab.

### Cover images

The clipping tool can put a cover next to a short: `X.jpg` for `X.mp4`, in the same folder (JPEG, 1080x1920).
The pairing by file name is the rule. The block `thumbnail` in the MP4 record is only extra information.

- **Preview:** the inbox and the Uploaded tab show the cover. If there is no cover, they show a frame of the clip.
- **Instagram:** the server sends the cover as `cover_url` in the REELS container. The tunnel serves the cover at
  `/v/<token>/cover.jpg`, with the same token as the clip.
- **YouTube:** after the upload, the server tries `thumbnails.set`. If YouTube refuses it (for example, the channel
  is not verified), the job shows a warning and the upload stays complete.
- **Files:** the cover moves to `posted/` with the clip, and a delete removes the cover too. A `.jpg` alone is
  never a clip.

## Access rules

The server has no login of its own. It accepts two types of request:

- **Local:** loopback, private LAN and Tailscale addresses, with an IP address or the Pi hostname as the host.
- **Public hostname:** `social.arorashivoy.win` goes through the shared tunnel `rpi-dashboard`
  (`~/.cloudflared/rpi-dashboard.yml`). The Access application "Social Dashboard" permits only Shivoy's email.
  The server also checks the signature and the audience of the header `Cf-Access-Jwt-Assertion`. If the Access
  application is removed, the public hostname gives 403.

The values `SOCIAL_PUBLIC_HOST`, `CF_ACCESS_TEAM` and `CF_ACCESS_AUD` are in the systemd unit. If you make the
Access application again, put the new AUD tag in the unit.
