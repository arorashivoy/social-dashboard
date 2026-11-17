#!/usr/bin/env python3
"""Social dashboard: upload clips as Instagram Reels and YouTube Shorts, and read the analytics.

The server uses only the Python standard library and `requests`.
Read README.md for the setup of the Instagram token and the YouTube client.
"""

import contextlib
import hashlib
import html
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CLIPS = Path(os.environ.get("SOCIAL_CLIPS", "/mnt/storage/social-clips"))
INBOX, POSTED = CLIPS / "inbox", CLIPS / "posted"
PORT = int(os.environ.get("SOCIAL_PORT", "8097"))
PUBLIC_PORT = PORT + 1          # loopback only; the temporary tunnel points here
SKILLS = Path.home() / ".claude" / "skills"
CLAUDE = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local/bin/claude"))
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}

IG_API = "https://graph.instagram.com/v27.0"      # token of the Instagram login (starts with IGAA)
FB_API = "https://graph.facebook.com/v27.0"       # token of the unified login or the Facebook login (starts with EAA)
YT_API = "https://www.googleapis.com/youtube/v3"
YT_SCOPES = ("https://www.googleapis.com/auth/youtube.upload "
             "https://www.googleapis.com/auth/youtube.readonly "
             "https://www.googleapis.com/auth/yt-analytics.readonly")
YT_REDIRECT = f"http://127.0.0.1:{PORT}/oauth/youtube"

for d in (DATA, DATA / "thumbs", DATA / "frames", INBOX, POSTED):
    d.mkdir(parents=True, exist_ok=True)

lock = threading.RLock()
jobs = {}                 # job id -> {"state": "running|done|error", "steps": [...], ...}
public_tokens = {}        # token -> file path, for the temporary public link
cache = {}                # small in-memory cache for analytics and probes


# ---------------------------------------------------------------- storage

def load(name, default):
    p = DATA / name
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def save(name, value):
    p = DATA / name
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def config():
    return load("config.json", {})


def set_config(**kw):
    with lock:
        c = config()
        c.update(kw)
        save("config.json", c)
        return c


def state():
    return load("state.json", {"drafts": {}, "history": []})


class ApiError(Exception):
    pass


# ---------------------------------------------------------------- clips

def clip_path(name):
    """Return the path of a clip. Accept only a plain file name that exists."""
    if not name or name != os.path.basename(name) or name.startswith("."):
        return None
    for folder in (INBOX, POSTED):
        p = folder / name
        if p.is_file() and p.suffix.lower() in VIDEO_EXT:
            return p
    return None


def cover_path(path):
    """The cover image that the clipping tool puts next to a short (X.mp4 -> X.jpg), or None."""
    c = path.with_suffix(".jpg")
    return c if c.is_file() else None


def preview_version(path):
    """A number that changes when the clip or its cover changes. The page adds it to the thumbnail address."""
    c = cover_path(path)
    return round(max(path.stat().st_mtime, c.stat().st_mtime if c else 0))


probe_cache = load("probe.json", {})      # "name|mtime|size" -> ffprobe result, kept across restarts


def short_meta(tags):
    """Read the record that the clipping tool puts in the MP4 tags. Return (title, text for the note field)."""
    tags = {k.lower(): v for k, v in (tags or {}).items()}
    try:
        meta = json.loads(tags.get("comment", ""))
    except ValueError:
        return "", ""
    if not isinstance(meta, dict) or meta.get("schema") != "clipping.short/1":
        return "", ""
    summary = str(meta.get("summary") or tags.get("description") or "").strip()
    lines = [summary] if summary else []
    groups = [str(g) for g in meta.get("groups") or [] if g and str(g) not in summary]
    if groups:
        lines.append("Opponent: " + ", ".join(groups) + ".")
    if meta.get("transcript"):
        lines.append("Speech in the clip: " + str(meta["transcript"]).strip()[:1200])
    return str(meta.get("title") or tags.get("title") or "")[:200], "\n".join(lines)[:2000]


def probe(path):
    st = path.stat()
    key = f"{path.name}|{st.st_mtime}|{st.st_size}"
    if key in probe_cache:
        return probe_cache[key]
    info = {"duration": 0, "width": 0, "height": 0, "vcodec": "", "acodec": "", "meta_title": "", "meta_note": ""}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=60).stdout
        j = json.loads(out)
        info["duration"] = float(j.get("format", {}).get("duration", 0) or 0)
        info["meta_title"], info["meta_note"] = short_meta(j.get("format", {}).get("tags", {}))
        for s in j.get("streams", []):
            if s.get("codec_type") == "video" and not info["width"]:
                w, h = int(s.get("width", 0)), int(s.get("height", 0))
                rot = str(s.get("tags", {}).get("rotate", ""))
                for sd in s.get("side_data_list", []):
                    if "rotation" in sd:
                        rot = str(abs(int(sd["rotation"])))
                if rot in ("90", "270"):
                    w, h = h, w
                info.update(width=w, height=h, vcodec=s.get("codec_name", ""))
            elif s.get("codec_type") == "audio":
                info["acodec"] = s.get("codec_name", "")
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    with lock:
        probe_cache[key] = info
        if info["width"]:
            save("probe.json", probe_cache)
    return info


def clip_checks(path, info):
    """Compare the clip with the rules for a Reel and a Short."""
    out = []
    d, w, h = info["duration"], info["width"], info["height"]
    size_mb = path.stat().st_size / 1e6
    if not w:
        return [{"level": "fail", "text": "ffprobe cannot read this file."}]
    if h <= w:
        out.append({"level": "fail", "text": f"The clip is {w}x{h}, not vertical. YouTube does not make it a Short."})
    elif abs(w / h - 9 / 16) > 0.03:
        out.append({"level": "warn", "text": f"The clip is {w}x{h}. The best ratio is 9:16."})
    if d > 180:
        out.append({"level": "fail", "text": f"The clip is {d:.0f} s. A Short has a limit of 180 s."})
    elif d > 90:
        out.append({"level": "warn", "text": f"The clip is {d:.0f} s. Instagram recommends 90 s or less for Reels."})
    elif d < 3:
        out.append({"level": "fail", "text": "The clip is shorter than 3 s. Instagram rejects it."})
    if path.suffix.lower() not in (".mp4", ".mov"):
        out.append({"level": "fail", "text": "Instagram accepts only MP4 and MOV files."})
    elif info["vcodec"] not in ("h264", "hevc"):
        out.append({"level": "warn", "text": f"The video codec is {info['vcodec']}. Instagram wants H.264 or HEVC."})
    if size_mb > 300:
        out.append({"level": "fail", "text": f"The file is {size_mb:.0f} MB. Instagram has a limit of 300 MB."})
    if not out:
        out.append({"level": "ok", "text": "The clip passes the checks for a Reel and a Short."})
    return out


def list_clips():
    drafts = state()["drafts"]
    out = []
    for p in sorted(INBOX.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXT or p.name.startswith("."):
            continue
        if time.time() - p.stat().st_mtime < 5:
            continue            # the copy of the file is possibly not complete
        info = probe(p)
        out.append({"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime, **info,
                    "cover": bool(cover_path(p)), "thumb_v": preview_version(p),
                    "checks": clip_checks(p, info), "draft": drafts.get(p.name, {})})
    return out


ffmpeg_slot = threading.Semaphore(1)      # the Pi decodes one 1080p frame at a time


def grab_frame(path, at, width, out):
    """Write one scaled frame of a clip. Seek before the input and skip all frames that are not key frames."""
    with ffmpeg_slot:
        if not out.exists():
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-threads", "2", "-skip_frame", "nokey", *(["-ss", f"{at:.2f}"] if at else []),
                            "-i", str(path), "-frames:v", "1", "-an", "-vf", f"scale={width}:-2", "-q:v", "5", str(out)],
                           timeout=120)
    return out if out.exists() else None


def posted_list():
    """The uploaded clips: each history entry, and each file in posted/ that has no history entry."""
    rows, seen = [], set()
    for x in state()["history"]:
        name = x.get("file") or x.get("clip")
        f = POSTED / name if name and name == os.path.basename(name) else None
        on_disk = bool(f and f.is_file())
        seen.add(name)
        rows.append({"file": name, "title": x.get("yt_title") or x.get("clip"), "time": x.get("time"),
                     "results": x.get("results", {}), "errors": x.get("errors", {}),
                     "exists": on_disk, "size": f.stat().st_size if on_disk else 0,
                     "thumb_v": preview_version(f) if on_disk else None,
                     "in_inbox": not on_disk and (INBOX / name).is_file() if name else False})
    for f in sorted(POSTED.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file() and f.suffix.lower() in VIDEO_EXT and f.name not in seen and not f.name.startswith("."):
            rows.append({"file": f.name, "title": f.name, "time": f.stat().st_mtime, "results": {}, "errors": {},
                         "exists": True, "size": f.stat().st_size, "in_inbox": False, "thumb_v": preview_version(f)})
    return rows


def delete_posted(name):
    """Delete the raw file of an uploaded clip. Only a file in posted/ is permitted."""
    if not name or name != os.path.basename(name) or name.startswith("."):
        raise ApiError("Bad file name.")
    f = POSTED / name
    if not f.is_file() or f.suffix.lower() not in VIDEO_EXT:
        raise ApiError("The file is not in the posted folder.")
    t, cover = thumb_file(f), cover_path(f)
    size = f.stat().st_size
    f.unlink()
    t.unlink(missing_ok=True)
    if cover:
        cover.unlink(missing_ok=True)
    print(f"deleted the posted clip {name} ({size} bytes)", flush=True)
    return size


def delete_inbox(name):
    """Delete a clip that is not uploaded. Only a file in the inbox is permitted, and no job can use it."""
    if not name or name != os.path.basename(name) or name.startswith("."):
        raise ApiError("Bad file name.")
    f = INBOX / name
    if not f.is_file() or f.suffix.lower() not in VIDEO_EXT:
        raise ApiError("The clip is not in the inbox.")
    if any(j["state"] == "running" and j.get("clip") == name for j in jobs.values()):
        raise ApiError("A caption job or an upload uses this clip. Wait until it is complete.")
    t, cover = thumb_file(f), cover_path(f)
    size = f.stat().st_size
    f.unlink()
    t.unlink(missing_ok=True)
    if cover:
        cover.unlink(missing_ok=True)
    with lock:
        st = state()
        if st["drafts"].pop(name, None) is not None:
            save("state.json", st)
    print(f"deleted the inbox clip {name} ({size} bytes)", flush=True)
    return size


def thumb_file(path):
    """The cache file of the preview. The key has the time of the cover, so a new cover makes a new preview."""
    c = cover_path(path)
    key = f"{path.name}{path.stat().st_mtime}" + (f"|cover{c.stat().st_mtime}" if c else "")
    return DATA / "thumbs" / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".jpg")


def thumb(path):
    """The preview of a clip: the cover X.jpg if it exists, else a frame of the clip. Both are scaled to 360 px."""
    t, c = thumb_file(path), cover_path(path)
    if t.exists():
        return t
    if c:
        return grab_frame(c, 0, 360, t) or c
    return grab_frame(path, min(1.0, probe(path)["duration"] / 2), 360, t)


def clean_cache(days=30):
    """Delete thumbnails and frames that are older than 30 days."""
    limit = time.time() - days * 86400
    for f in (DATA / "thumbs").iterdir():
        if f.stat().st_mtime < limit:
            f.unlink(missing_ok=True)
    for d in (DATA / "frames").iterdir():
        if d.is_dir() and d.stat().st_mtime < limit:
            shutil.rmtree(d, ignore_errors=True)
    live = {f"{p.name}|{p.stat().st_mtime}|{p.stat().st_size}" for folder in (INBOX, POSTED)
            for p in folder.iterdir() if p.is_file()}
    for k in [k for k in probe_cache if k not in live]:
        probe_cache.pop(k)


# ---------------------------------------------------------------- captions

def skill_tool(rel, text, *args):
    """Run one of the Python tools of the Instagram skill pack on a text."""
    tool = SKILLS / rel
    if not tool.exists():
        return None
    try:
        r = subprocess.run(["python3", str(tool), "-", *args], input=text, capture_output=True, text=True, timeout=30)
        return r.stdout
    except (OSError, subprocess.SubprocessError):
        return None


def humanize(text):
    out = skill_tool("ig-human/humanize.py", text)
    return out.strip() if out and out.strip() else text.strip()


def lint(text, keywords=()):
    kw = ",".join(k.replace(",", " ").strip() for k in keywords if k.strip())
    out = skill_tool("ig-caption/caption.py", text, "--json", *(["--keywords", kw] if kw else []))
    try:
        j = json.loads(out)
        return {"verdict": j["verdict"], "characters": j["characters"], "visible": j["visible"],
                "checks": [c for c in j["checks"] if c["status"] != "PASS"]}
    except (TypeError, ValueError, KeyError):
        return None


SKILLS = Path.home() / ".claude/skills"      # the pack github.com/Jakeschincariol/instagram-agent-skill


def skill_text(name):
    """The instructions of one skill of the Instagram pack, without the front matter. Read at each run."""
    try:
        text = (SKILLS / name / "SKILL.md").read_text()
    except OSError:
        raise ApiError(f"The skill {name} is not installed in {SKILLS}. The captions need it.") from None
    return re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S).strip()


CAPTION_PROMPT = """Write the text for ONE vertical gameplay clip that goes out as an Instagram Reel and as a YouTube Short.
Follow the skill `ig-caption` below for the Instagram caption. It is the authority for the caption.

<skill name="ig-caption">
{ig_caption}
</skill>

<skill name="ig-human">
{ig_human}
</skill>

How to apply the skills in this run:
- The channel profile below sets the order of the caption: line 1 is the hook, and the ask comes after it.
  Where the profile and the skill differ, follow the profile. Do not ask which job it is.
- Do not run `caption.py`, `humanize.py` or `detect.py`. The dashboard runs them on your answer and shows the result.
  Write text that passes them: follow each rule of the two skills now.
- Do not ask the creator for the search terms. Select 2 or 3 from the clip data and the channel profile, and use
  each one as a normal phrase in the caption. Return them in `keywords`.
- Do not print the receipt or the copy-ready block. Reply with the JSON object only.

The clip:
{frames}
What the creator says about the clip: {note}
Clip length: {duration:.0f} seconds. File name: {name}

Channel profile (ignore empty fields):
{profile}

Write 3 different options. Each option uses a different angle (for example: curiosity, direct value, story).
Rules for `yt_title`: 70 characters or fewer, specific, no clickbait that the clip does not deliver, no hashtags.
Rules for `yt_description`: 1 or 2 plain sentences with the search terms, then a line with 3 hashtags.
The skill `ig-human` applies to all three texts.
Do not invent facts, numbers or names that the frames and the creator's note do not show.

Reply with ONLY a JSON object, no code fence, in this shape:
{{"options":[{{"angle":"two or three words","keywords":["term","term"],"ig_caption":"...","yt_title":"...","yt_description":"..."}}]}}"""

REPAIR_PROMPT = """You wrote Instagram captions with the skill `ig-caption`. The linter `caption.py` of the skill
found a FAIL in some of them. The skill says: fix every FAIL.

<skill name="ig-caption">
{ig_caption}
</skill>

The captions with their linter result:
{items}

Write each caption again so that the linter passes. Keep the angle, the facts and the search terms.
Keep the order: the hook is line 1, and the ask comes after the hook. Never move the ask into line 1.
Do not invent facts. Do not use tools.
Reply with ONLY a JSON object, no code fence, in this shape:
{{"fixed":[{{"index":0,"ig_caption":"..."}}]}}"""


def extract_frames(path):
    h = hashlib.sha1(f"{path}{path.stat().st_mtime}".encode()).hexdigest()[:16]
    d = DATA / "frames" / h
    d.mkdir(exist_ok=True)
    dur = probe(path)["duration"] or 1
    frames = []
    for i, frac in enumerate((0.08, 0.35, 0.62, 0.9), 1):
        f = d / f"frame{i}.jpg"
        if f.exists() or grab_frame(path, dur * frac, 480, f):
            frames.append(f)
    return d, frames


def ask_sonnet(prompt, folder):
    """Run one Claude call with the Sonnet model. Refuse the answer if a different model made a part of it."""
    r = subprocess.run([CLAUDE, "-p", prompt, "--model", "sonnet", "--allowedTools", "Read", "--output-format", "json"],
                       cwd=folder, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise ApiError(f"Claude gave no options. {(r.stderr or r.stdout)[-300:]}") from None
    if r.returncode != 0 or out.get("is_error"):
        raise ApiError(f"Claude gave no options. {str(out.get('result') or r.stderr)[-300:]}")
    models = sorted(out.get("modelUsage") or {})
    if not models or any("sonnet" not in m for m in models):
        raise ApiError(f"The captions did not come from Sonnet only (models: {', '.join(models) or 'unknown'}). They are not used.")
    return str(out.get("result", "")).strip(), models


def answer_json(text):
    """The JSON object in an answer of Claude, or an empty object."""
    m = re.search(r"\{.*\}", text, re.S)
    try:
        out = json.loads(m.group(0)) if m else {}
    except ValueError:
        out = {}
    return out if isinstance(out, dict) else {}


def caption_job(job, name, note):
    path = clip_path(name)
    step(job, "Read 4 frames of the clip")
    folder, frames = extract_frames(path)
    frame_text = ("First use the Read tool on these frames of the clip, in order of time:\n"
                  + "\n".join(str(f) for f in frames)) if frames else "No frames are available."
    profile = config().get("profile", "").strip()     # the channel profile from the Setup tab is the only voice source
    ig_caption = skill_text("ig-caption")
    prompt = CAPTION_PROMPT.format(ig_caption=ig_caption, ig_human=skill_text("ig-human"), frames=frame_text,
                                   note=note.strip() or "(no note)", name=name,
                                   duration=probe(path)["duration"], profile=profile.strip() or "(empty)")
    step(job, "Ask Claude for 3 options with the skill ig-caption (30 to 90 seconds)")
    text, models = ask_sonnet(prompt, folder)
    step(job, "Model: " + ", ".join(models))
    options = [o for o in answer_json(text).get("options", []) if isinstance(o, dict)][:3]
    if not options:
        raise ApiError(f"Claude gave no options. {text[-300:]}")

    def finish(o):
        for k in ("ig_caption", "yt_title", "yt_description"):
            o[k] = humanize(str(o.get(k, "")))
        o["yt_title"] = o["yt_title"][:100]
        o["keywords"] = [str(k)[:60] for k in o.get("keywords") or [] if isinstance(k, str)][:3]
        o["lint"] = lint(o["ig_caption"], o["keywords"])

    def fails(o):
        return [c for c in (o["lint"] or {}).get("checks", []) if c["status"] == "FAIL"]

    step(job, "Clean the text with ig-human and run the caption linter")
    for o in options:
        finish(o)
    bad = [i for i, o in enumerate(options) if fails(o)]
    if bad:                                   # the skill: fix every FAIL. One more call, not a loop.
        step(job, f"The linter found a FAIL in {len(bad)} of {len(options)} options. Ask Claude to fix them")
        items = "\n\n".join(
            f"index {i}, search terms: {', '.join(options[i]['keywords']) or 'none'}\n<caption>\n{options[i]['ig_caption']}\n</caption>\n"
            + "\n".join(f"FAIL {c['check']}: {c['detail']}" for c in fails(options[i])) for i in bad)
        try:
            text, _ = ask_sonnet(REPAIR_PROMPT.format(ig_caption=ig_caption, items=items), folder)
            for f in answer_json(text).get("fixed", []):
                i = f.get("index") if isinstance(f, dict) else None
                if i in bad and str(f.get("ig_caption", "")).strip():
                    options[i]["ig_caption"] = str(f["ig_caption"])
                    finish(options[i])
        except ApiError as e:
            step(job, f"The fix did not work ({e}). The first text stays.")
        left = sum(1 for o in options if fails(o))
        step(job, "The linter passes all options." if not left else f"{left} options still have a FAIL. Edit them before the upload.")
    with lock:
        s = state()
        dr = s["drafts"].setdefault(name, {})
        dr.update(note=note, options=options)
        save("state.json", s)
    job["options"] = options


# ---------------------------------------------------------------- Instagram

def ig_token():
    c = config()
    tok = c.get("ig_token")
    if not tok:
        raise ApiError("Instagram is not connected. Open Setup and paste the access token.")
    # A long-lived token lasts 60 days. Refresh it when it is older than 7 days.
    if time.time() - c.get("ig_token_time", 0) > 7 * 86400:
        try:
            if c.get("ig_mode") == "fb":
                new = fb_long_token(tok) if c.get("fb_app_id") and c.get("fb_app_secret") else None
            else:
                new = requests.get("https://graph.instagram.com/refresh_access_token", timeout=30,
                                   params={"grant_type": "ig_refresh_token", "access_token": tok}).json().get("access_token")
            if new:
                tok = new
                set_config(ig_token=tok, ig_token_time=time.time())
        except (requests.RequestException, ApiError):
            pass
    return tok


def fb_long_token(tok):
    """Change a Facebook user token into a token that lasts 60 days. This needs the app ID and the app secret."""
    c = config()
    r = requests.get(f"{FB_API}/oauth/access_token", timeout=30, params={
        "grant_type": "fb_exchange_token", "client_id": c.get("fb_app_id"),
        "client_secret": c.get("fb_app_secret"), "fb_exchange_token": tok}).json()
    if "access_token" not in r:
        raise ApiError(f"Meta did not give a 60-day token: {r.get('error', {}).get('message', r)}")
    return r["access_token"]


def fb_connect(tok):
    """Find the Instagram account that an EAA token can use.

    Unified login: the account is in /me/assigned_instagram_accounts, and no Facebook Page is necessary.
    Facebook login: the account is behind a Facebook Page in /me/accounts.
    """
    def get(path, fields):
        return requests.get(f"{FB_API}/{path}", timeout=30,
                            params={"fields": fields, "access_token": tok, "limit": 100}).json()

    r = get("me/assigned_instagram_accounts", "id,username")
    accounts = r.get("data") or []
    first_error = r.get("error", {}).get("message")
    if not accounts:
        r = get("me/accounts", "name,instagram_business_account{id,username}")
        accounts = [p["instagram_business_account"] for p in r.get("data", []) if p.get("instagram_business_account")]
    if not accounts:
        why = first_error or r.get("error", {}).get("message") or "the token is valid, but no Instagram account is assigned to it"
        raise ApiError(f"Instagram: {why}. In the Meta app, add the Instagram account as a tester, accept the "
                       "invite in Instagram, and generate the token again with the permissions instagram_basic, instagram_content_publish and "
                       "instagram_manage_insights.")
    acct = accounts[0]
    note = "The life of this token is set in the configuration of the Meta app."
    if config().get("fb_app_id") and config().get("fb_app_secret"):
        try:                                   # only a user token of the Facebook login can change to 60 days
            tok = fb_long_token(tok)
            note = "The token lasts 60 days."
        except (ApiError, requests.RequestException):
            pass
    set_config(ig_token=tok, ig_token_time=time.time(), ig_mode="fb", ig_user_id=acct["id"],
               ig_username=acct.get("username", ""))
    if len(accounts) > 1:
        note += f" The token can use {len(accounts)} accounts. The dashboard uses @{acct.get('username')}."
    return note


def ig_uid():
    c = config()
    return c.get("ig_user_id") if c.get("ig_mode") == "fb" else "me"


def ig(method, path, **kw):
    tok = ig_token()
    # Send the token in the header and as a parameter. Some Instagram endpoints read only one of them.
    kw["params"] = {**kw.get("params", {}), "access_token": tok}
    try:
        base = FB_API if config().get("ig_mode") == "fb" else IG_API
        r = requests.request(method, f"{base}/{path}", headers={"Authorization": f"Bearer {tok}"}, timeout=60, **kw)
        j = r.json()
    except (requests.RequestException, ValueError) as e:
        raise ApiError(f"Instagram request failed: {e}")
    if "error" in j:
        raise ApiError(f"Instagram: {j['error'].get('error_user_msg') or j['error'].get('message')}")
    return j


def ig_me():
    if config().get("ig_mode") == "fb":
        j = ig("GET", ig_uid(), params={"fields": "username,followers_count,media_count"})
        set_config(ig_username=j.get("username"))
        return j
    try:
        j = ig("GET", "me", params={"fields": "user_id,username,followers_count,media_count"})
    except ApiError:
        j = ig("GET", "me", params={"fields": "user_id,username"})
    set_config(ig_user_id=j.get("user_id") or j.get("id"), ig_username=j.get("username"))
    return j


def start_tunnel(job):
    """Open a temporary Cloudflare link that points only at the public file port."""
    proc = subprocess.Popen(["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PUBLIC_PORT}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    url, deadline = None, time.time() + 45
    while time.time() < deadline and proc.poll() is None:
        line = proc.stderr.readline()
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m:
            url = m.group(0)
            break
    if not url:
        proc.terminate()
        raise ApiError("The temporary Cloudflare link did not start.")
    threading.Thread(target=lambda: [None for _ in proc.stderr], daemon=True).start()   # drain the log
    return proc, url


def ig_wait_publish(job, uid, cid):
    step(job, "Instagram: wait while Instagram processes the video")
    for _ in range(90):
        time.sleep(10)
        st = ig("GET", cid, params={"fields": "status_code,status"})
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise ApiError(f"Instagram rejected the video: {st.get('status')}")
    else:
        raise ApiError("Instagram did not finish the video in 15 minutes.")
    step(job, "Instagram: publish the Reel")
    mid = ig("POST", f"{uid}/media_publish", json={"creation_id": cid})["id"]
    link = ig("GET", mid, params={"fields": "permalink"}).get("permalink", "")
    return {"id": mid, "url": link}


@contextlib.contextmanager
def public_link(job, path):
    """Open a temporary public link for a clip and its cover. Yield the base address. Close the link at the end.

    The video is at <base>/clip.<ext> and the cover at <base>/cover.jpg. Both use the same secret token.
    """
    token = secrets.token_urlsafe(24)
    public_tokens[token] = path
    proc = None
    try:
        step(job, "Instagram: open a temporary public link for the clip")
        proc, host = start_tunnel(job)
        base = f"{host}/v/{token}"
        url = f"{base}/clip{path.suffix.lower()}"
        for _ in range(20):                       # wait until the link answers
            # Ask a public DNS. The DNS of the Pi does not know the new name for some minutes.
            code = subprocess.run(["curl", "-s", "-I", "-o", "/dev/null", "-w", "%{http_code}", "-m", "15",
                                   "--doh-url", "https://1.1.1.1/dns-query", url], capture_output=True, text=True).stdout
            if code == "200":
                break
            time.sleep(3)
        else:
            raise ApiError("The temporary public link did not answer in 60 seconds.")
        yield base
    finally:
        public_tokens.pop(token, None)
        if proc:
            proc.terminate()


def ig_upload_direct(job, path, caption, cover_url=None):
    """Facebook login: send the file from the Pi to Meta. A public link is necessary only for the cover."""
    uid = ig_uid()
    step(job, "Instagram: create the Reel container" + (" with the cover" if cover_url else ""))
    c = ig("POST", f"{uid}/media", json={"media_type": "REELS", "upload_type": "resumable", "caption": caption,
                                         "share_to_feed": True, **({"cover_url": cover_url} if cover_url else {})})
    step(job, "Instagram: upload the file")
    with open(path, "rb") as f:
        r = requests.post(c.get("uri") or f"https://rupload.facebook.com/ig-api-upload/v27.0/{c['id']}", data=f,
                          timeout=1800, headers={"Authorization": f"OAuth {ig_token()}", "offset": "0",
                                                 "file_size": str(path.stat().st_size)})
    try:
        ok = r.json().get("success")
    except ValueError:
        ok = False
    if not ok:
        raise ApiError(f"Instagram refused the file: {r.text[:200]}")
    return ig_wait_publish(job, uid, c["id"])


def ig_upload(job, path, caption):
    c = config()
    cover = cover_path(path)
    with contextlib.ExitStack() as stack:
        base = None

        def link():                               # open the public link one time, and keep it to the end
            nonlocal base
            if base is None:
                base = stack.enter_context(public_link(job, path))
            return base

        if c.get("ig_mode") == "fb":
            cover_url = None
            if cover:
                try:
                    cover_url = link() + "/cover.jpg"
                except ApiError as e:
                    step(job, f"Instagram: warning, no public link for the cover ({e}). Upload without the cover.")
            try:
                return ig_upload_direct(job, path, caption, cover_url)
            except ApiError as e:
                if "rejected the video" in str(e) or "did not finish" in str(e):
                    raise
                step(job, f"Instagram: the direct upload failed ({str(e)[:120]}). Try the public link.")
        uid = c.get("ig_user_id") or ig_me().get("user_id")
        b = link()
        step(job, "Instagram: create the Reel container" + (" with the cover" if cover else ""))
        cid = ig("POST", f"{uid}/media", json={"media_type": "REELS", "video_url": f"{b}/clip{path.suffix.lower()}",
                                               "caption": caption, "share_to_feed": True,
                                               **({"cover_url": f"{b}/cover.jpg"} if cover else {})})["id"]
        return ig_wait_publish(job, uid, cid)


def ig_analytics():
    me = ig_me()
    media = ig("GET", f"{ig_uid()}/media", params={
        "fields": "id,caption,media_product_type,permalink,timestamp,like_count,comments_count", "limit": 15})
    rows, warning = [], None
    for m in media.get("data", []):
        row = {"id": m["id"], "title": (m.get("caption") or "(no caption)").split("\n")[0][:80],
               "url": m.get("permalink"), "date": m.get("timestamp", "")[:10], "type": m.get("media_product_type"),
               "likes": m.get("like_count"), "comments": m.get("comments_count")}
        rows.append(row)
        if warning and "permission" in warning:
            continue                          # the token cannot read insights. Do not ask again for each post.
        metrics = "views,reach,saved,shares,total_interactions"
        if m.get("media_product_type") == "REELS":
            metrics += ",ig_reels_avg_watch_time"
        try:
            ins = ig("GET", f"{m['id']}/insights", params={"metric": metrics})
            for d in ins.get("data", []):
                row[d["name"]] = (d.get("values") or [{}])[0].get("value")
        except ApiError as e:
            row["note"] = str(e)[:120]
            warning = warning or str(e)
    if warning and "(#10)" in warning:
        warning = ("Instagram does not give the views, reach, saves or shares: the token does not have the "
                   "permission instagram_manage_insights. Generate the token again with this permission "
                   "(Setup, step 'Configure your app') and paste it on the Setup tab.")
    elif warning:
        warning = "Instagram did not give the insights of some posts. " + warning
    return {"account": {"name": me.get("username"), "followers": me.get("followers_count"),
                        "posts": me.get("media_count")}, "rows": rows, "warning": warning}


# ---------------------------------------------------------------- YouTube

def yt_access():
    c = config()
    if not c.get("yt_refresh_token"):
        raise ApiError("YouTube is not connected. Open Setup and connect the channel.")
    hit = cache.get("yt_access")
    if hit and hit[1] > time.time() + 60:
        return hit[0]
    r = requests.post("https://oauth2.googleapis.com/token", timeout=30, data={
        "client_id": c["yt_client_id"], "client_secret": c["yt_client_secret"],
        "refresh_token": c["yt_refresh_token"], "grant_type": "refresh_token"}).json()
    if "access_token" not in r:
        raise ApiError(f"YouTube login expired ({r.get('error_description') or r.get('error')}). Connect it again.")
    cache["yt_access"] = (r["access_token"], time.time() + r.get("expires_in", 3600))
    return r["access_token"]


def yt(path, base=YT_API, **params):
    try:
        r = requests.get(f"{base}/{path}", params=params, timeout=60,
                         headers={"Authorization": f"Bearer {yt_access()}"})
        j = r.json()
    except (requests.RequestException, ValueError) as e:
        raise ApiError(f"YouTube request failed: {e}")
    if "error" in j:
        raise ApiError(f"YouTube: {j['error'].get('message')}")
    return j


def yt_auth_url():
    c = config()
    if not c.get("yt_client_id") or not c.get("yt_client_secret"):
        raise ApiError("Save the client ID and the client secret first.")
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": c["yt_client_id"], "redirect_uri": YT_REDIRECT, "response_type": "code",
        "scope": YT_SCOPES, "access_type": "offline", "prompt": "consent"})


def yt_exchange(code):
    c = config()
    r = requests.post("https://oauth2.googleapis.com/token", timeout=30, data={
        "client_id": c.get("yt_client_id"), "client_secret": c.get("yt_client_secret"), "code": code,
        "redirect_uri": YT_REDIRECT, "grant_type": "authorization_code"}).json()
    if "refresh_token" not in r:
        raise ApiError(f"Google gave no refresh token: {r.get('error_description') or r.get('error') or r}")
    cache.pop("yt_access", None)
    set_config(yt_refresh_token=r["refresh_token"])
    ch = yt("channels", part="snippet", mine="true").get("items", [])
    set_config(yt_channel=ch[0]["snippet"]["title"] if ch else "")


def yt_upload(job, path, title, description, privacy):
    size = path.stat().st_size
    meta = {"snippet": {"title": title[:100], "description": description[:5000], "categoryId": "22"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    step(job, "YouTube: start the upload")
    r = requests.post("https://www.googleapis.com/upload/youtube/v3/videos",
                      params={"uploadType": "resumable", "part": "snippet,status"}, json=meta, timeout=60,
                      headers={"Authorization": f"Bearer {yt_access()}", "X-Upload-Content-Length": str(size),
                               "X-Upload-Content-Type": mimetypes.guess_type(path.name)[0] or "video/mp4"})
    if r.status_code != 200 or "Location" not in r.headers:
        try:
            msg = r.json()["error"]["message"]
        except (ValueError, KeyError):
            msg = r.text[:200]
        raise ApiError(f"YouTube refused the upload: {msg}")
    session, chunk, sent, vid = r.headers["Location"], 8 * 1024 * 1024, 0, None
    with open(path, "rb") as f:
        while sent < size:
            data = f.read(chunk)
            for attempt in range(4):
                try:
                    r = requests.put(session, data=data, timeout=300, headers={
                        "Content-Range": f"bytes {sent}-{sent + len(data) - 1}/{size}"})
                    if r.status_code in (200, 201, 308):
                        break
                except requests.RequestException:
                    pass
                time.sleep(5 * (attempt + 1))
            else:
                raise ApiError("YouTube upload failed after 4 attempts on one part.")
            sent += len(data)
            job["progress"] = round(100 * sent / size)
            step(job, f"YouTube: upload {job['progress']}%", replace="YouTube: upload")
            if r.status_code in (200, 201):
                vid = r.json()["id"]
    if not vid:
        raise ApiError("YouTube gave no video ID.")
    out = {"id": vid, "url": f"https://youtube.com/shorts/{vid}", "privacy": privacy}
    try:
        st = yt("videos", part="status", id=vid)["items"][0]["status"]
        out["privacy"] = st.get("privacyStatus", privacy)
    except (ApiError, LookupError):
        pass
    if out["privacy"] != privacy:
        out["note"] = (f"YouTube set the video to {out['privacy']}. An API project without an audit "
                       "cannot publish. Change the visibility in YouTube Studio.")
    yt_set_thumbnail(job, vid, path)
    return out


def yt_set_thumbnail(job, vid, path):
    """Try to set the cover X.jpg as the thumbnail. A failure is only a warning: the video is uploaded."""
    cover = cover_path(path)
    if not cover:
        return
    step(job, "YouTube: set the cover as the thumbnail")
    try:
        r = requests.post("https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
                          params={"videoId": vid, "uploadType": "media"}, data=cover.read_bytes(), timeout=120,
                          headers={"Authorization": f"Bearer {yt_access()}", "Content-Type": "image/jpeg"})
        if r.status_code == 200:
            step(job, "YouTube: the cover is the thumbnail. YouTube can still show a frame for a Short.")
            return
        try:
            msg = r.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            msg = r.text[:200]
    except (requests.RequestException, ApiError, OSError) as e:
        msg = str(e)
    step(job, f"YouTube: warning, the thumbnail is not set ({msg[:200]}). The video is uploaded.")
    print(f"warning: thumbnails.set failed for {vid}: {msg[:300]}", flush=True)


def iso_seconds(d):
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    return sum(int(v or 0) * k for v, k in zip(m.groups(), (3600, 60, 1))) if m else 0


def yt_analytics():
    ch = yt("channels", part="snippet,statistics,contentDetails", mine="true")["items"][0]
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    ids = [i["contentDetails"]["videoId"] for i in
           yt("playlistItems", part="contentDetails", playlistId=uploads, maxResults=15).get("items", [])]
    rows = []
    if ids:
        extra = {}
        try:
            rep = yt("reports", base="https://youtubeanalytics.googleapis.com/v2", ids="channel==MINE",
                     startDate="2005-01-01", endDate=time.strftime("%Y-%m-%d"), dimensions="video",
                     metrics="views,averageViewPercentage,averageViewDuration", filters="video==" + ",".join(ids),
                     sort="-views", maxResults=50)
            for r in rep.get("rows", []):
                extra[r[0]] = {"avg_pct": round(r[2]), "avg_sec": r[3]}
        except ApiError:
            pass
        for v in yt("videos", part="snippet,statistics,contentDetails,status", id=",".join(ids)).get("items", []):
            s = v.get("statistics", {})
            rows.append({"id": v["id"], "title": v["snippet"]["title"][:80], "date": v["snippet"]["publishedAt"][:10],
                         "url": f"https://youtu.be/{v['id']}", "seconds": iso_seconds(v["contentDetails"].get("duration")),
                         "privacy": v["status"].get("privacyStatus"), "views": int(s.get("viewCount", 0)),
                         "likes": int(s.get("likeCount", 0)), "comments": int(s.get("commentCount", 0)),
                         **extra.get(v["id"], {})})
    st = ch["statistics"]
    return {"account": {"name": ch["snippet"]["title"], "followers": int(st.get("subscriberCount", 0)),
                        "posts": int(st.get("videoCount", 0)), "views": int(st.get("viewCount", 0))}, "rows": rows}


def analytics(refresh=False):
    hit = cache.get("analytics")
    if hit and not refresh and time.time() - hit["time"] < 900:
        return hit
    out = {"time": time.time()}
    for key, fn in (("instagram", ig_analytics), ("youtube", yt_analytics)):
        try:
            out[key] = fn()
        except ApiError as e:
            out[key] = {"error": str(e)}
        except Exception as e:  # noqa: BLE001 - one platform must not break the other
            out[key] = {"error": f"{type(e).__name__}: {e}"}
    cache["analytics"] = out
    return out


# ---------------------------------------------------------------- jobs

def step(job, text, replace=None):
    with lock:
        if replace and job["steps"] and job["steps"][-1].startswith(replace):
            job["steps"][-1] = text
        else:
            job["steps"].append(text)


def run_job(kind, fn, *args):
    jid = secrets.token_hex(6)
    job = jobs[jid] = {"id": jid, "kind": kind, "state": "running", "steps": [], "time": time.time(),
                       "clip": args[0] if args else None}

    def work():
        try:
            fn(job, *args)
            job["state"] = "done"
        except Exception as e:  # noqa: BLE001 - report every failure to the page
            job["state"] = "error"
            job["error"] = str(e) if isinstance(e, (ApiError, subprocess.TimeoutExpired)) else f"{type(e).__name__}: {e}"
    threading.Thread(target=work, daemon=True).start()
    return job


def upload_job(job, name, body):
    path = clip_path(name)
    platforms = body.get("platforms") or []
    results, errors = {}, {}
    job["results"], job["errors"] = results, errors
    if "youtube" in platforms:
        try:
            results["youtube"] = yt_upload(job, path, body["yt_title"], body["yt_description"],
                                           body.get("privacy", "public"))
        except Exception as e:  # noqa: BLE001
            errors["youtube"] = str(e)
            step(job, f"YouTube failed: {e}")
    if "instagram" in platforms:
        try:
            results["instagram"] = ig_upload(job, path, body["ig_caption"])
        except Exception as e:  # noqa: BLE001
            errors["instagram"] = str(e)
            step(job, f"Instagram failed: {e}")
    with lock:
        s = state()
        if results:
            s["history"].insert(0, {"clip": name, "file": name, "time": time.time(),
                                    "ig_caption": body.get("ig_caption"),
                                    "yt_title": body.get("yt_title"), "results": results, "errors": errors})
        if results and not errors:
            s["drafts"].pop(name, None)
            dest = POSTED / name
            if dest.exists():
                dest = POSTED / f"{int(time.time())}-{name}"
            cover = cover_path(path)
            shutil.move(str(path), str(dest))
            if cover:
                shutil.move(str(cover), str(dest.with_suffix(".jpg")))
            s["history"][0]["file"] = dest.name
            step(job, "Move the clip to the posted folder" + (" with its cover" if cover else ""))
        save("state.json", s)
    cache.pop("analytics", None)
    if errors and not results:
        raise ApiError(" | ".join(f"{k}: {v}" for k, v in errors.items()))
    if errors:
        step(job, "One platform failed. The clip stays in the inbox. Upload again to the failed platform only.")


# ---------------------------------------------------------------- HTTP

def allowed_ip(ip):
    a = ipaddress.ip_address(ip.split("%")[0])
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return a.is_loopback or a.is_private or a in ipaddress.ip_network("100.64.0.0/10")


# Cloudflare Tunnel: requests for the public hostname must carry a valid Cloudflare Access token.
PUBLIC_HOST = os.environ.get("SOCIAL_PUBLIC_HOST", "").lower()
CF_TEAM = os.environ.get("CF_ACCESS_TEAM", "")
CF_AUD = os.environ.get("CF_ACCESS_AUD", "")
_jwks = None


def access_ok(token):
    """Return True if the Cloudflare Access token is valid for this application."""
    global _jwks
    if not (token and CF_TEAM and CF_AUD):
        return False
    try:
        import jwt
        if _jwks is None:
            _jwks = jwt.PyJWKClient(f"https://{CF_TEAM}/cdn-cgi/access/certs")
        key = _jwks.get_signing_key_from_jwt(token).key
        jwt.decode(token, key, algorithms=["RS256"], audience=CF_AUD, issuer=f"https://{CF_TEAM}")
        return True
    except Exception:  # noqa: BLE001 - every failure means no access
        return False


def allowed_host(host):
    h = (host or "").rsplit(":", 1)[0].strip("[]").lower()
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return h == "localhost" or h.split(".")[0] == socket.gethostname().lower()


WATCH_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>{{name}}</title>
<link rel="icon" href="/icon.png" type="image/png">
<style>
:root { color-scheme: dark; }
body { margin: 0; min-height: 100vh; min-height: 100dvh; display: grid; grid-template-rows: auto 1fr; background: #0c0b09;
  color: #f1eadf; font: 14px/1.4 ui-monospace, Menlo, monospace; }
header { display: flex; gap: 16px; align-items: baseline; flex-wrap: wrap; padding: 10px 16px; border-bottom: 1px solid #38322b; }
h1 { font-size: 14px; font-weight: 500; margin: 0; overflow-wrap: anywhere; flex: 1; }
a { color: #ffb224; }
video { width: 100%; height: 100%; max-height: calc(100dvh - 48px); object-fit: contain; background: #000; min-height: 0; }
</style></head><body>
<header><h1>{{name}}</h1><a href="{{src}}" download>Download</a><a href="/">Dashboard</a></header>
<video src="{{src}}" controls autoplay playsinline></video>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "social-dashboard"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, text):
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_file(self, path, ctype=None, cache_ctl="no-cache"):
        st = path.stat()
        size = st.st_size
        etag = f'"{int(st.st_mtime)}-{size}"'
        if self.headers.get("If-None-Match") == etag and "Range" not in self.headers:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache_ctl)
            self.send_header("Content-Length", "0")
            return self.end_headers()
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), end) if m.group(2) else end
            else:
                start = max(0, size - int(m.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", cache_ctl)
        self.end_headers()
        if self.command == "HEAD" or size == 0:
            return
        try:
            self.wfile.flush()
            with open(path, "rb") as f:
                # The kernel copies the file to the socket. Python does not hold the data in memory.
                self.connection.sendfile(f, offset=start, count=end - start + 1)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def guard(self):
        host = (self.headers.get("Host") or "").lower()
        if PUBLIC_HOST and host == PUBLIC_HOST:
            ok = (ipaddress.ip_address(self.client_address[0]).is_loopback
                  and access_ok(self.headers.get("Cf-Access-Jwt-Assertion")))
        else:
            # A request that came through Cloudflare must use the public hostname.
            ok = (allowed_ip(self.client_address[0]) and allowed_host(host)
                  and not self.headers.get("Cf-Connecting-Ip"))
        if not ok:
            self.send_json({"error": "This address is not permitted."}, 403)
        return ok

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.guard():
            return
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = urllib.parse.unquote(u.path)
        try:
            if path == "/":
                return self.send_file(ROOT / "static" / "index.html", "text/html; charset=utf-8")
            if path in ("/icon.png", "/icon-180.png", "/favicon.ico"):
                name = "icon-180.png" if path == "/icon-180.png" else "icon.png"
                return self.send_file(ROOT / "static" / name, "image/png", "max-age=86400")
            if path.startswith("/watch/"):
                p = clip_path(path.split("/", 2)[2])
                if not p:
                    return self.send_json({"error": "not found"}, 404)
                return self.send_html(WATCH_PAGE.replace("{{name}}", html.escape(p.name))
                                      .replace("{{src}}", "/media/" + urllib.parse.quote(p.name)))
            if path.startswith(("/media/", "/thumb/")):
                p = clip_path(path.split("/", 2)[2])
                is_thumb = path.startswith("/thumb/")
                if p and is_thumb:
                    p = thumb(p)
                if not p:
                    return self.send_json({"error": "not found"}, 404)
                # The page adds the file time to the thumbnail address, so the browser can keep it.
                return self.send_file(p, cache_ctl="max-age=31536000, immutable" if is_thumb and "v" in q else "no-cache")
            if path == "/api/status":
                c = config()
                return self.send_json({
                    "clips_dir": str(INBOX), "redirect": YT_REDIRECT, "profile": c.get("profile", ""),
                    "privacy": c.get("privacy", "public"),
                    "instagram": {"connected": bool(c.get("ig_token")), "name": c.get("ig_username"),
                                  "mode": c.get("ig_mode") or "ig", "has_app": bool(c.get("fb_app_id") and c.get("fb_app_secret"))},
                    "youtube": {"connected": bool(c.get("yt_refresh_token")), "name": c.get("yt_channel"),
                                "has_client": bool(c.get("yt_client_id") and c.get("yt_client_secret"))}})
            if path == "/api/clips":
                return self.send_json({"clips": list_clips(), "history": state()["history"][:30]})
            if path == "/api/posted":
                return self.send_json({"rows": posted_list(), "dir": str(POSTED)})
            if path.startswith("/api/job/"):
                job = jobs.get(path.rsplit("/", 1)[1])
                return self.send_json(job or {"state": "error", "error": "The job is gone. The server restarted."})
            if path == "/api/analytics":
                return self.send_json(analytics(refresh="refresh" in q))
            if path == "/api/yt/auth-url":
                return self.send_json({"url": yt_auth_url()})
            if path == "/oauth/youtube":
                yt_exchange(q.get("code", [""])[0])
                self.send_response(302)
                self.send_header("Location", "/#setup")
                self.send_header("Content-Length", "0")
                return self.end_headers()
            self.send_json({"error": "not found"}, 404)
        except ApiError as e:
            self.send_json({"error": str(e)}, 400)

    def do_POST(self):
        if not self.guard():
            return
        # A JSON content type forces a CORS preflight, so another site cannot send this request.
        if "application/json" not in self.headers.get("Content-Type", ""):
            return self.send_json({"error": "JSON only"}, 415)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            path = urllib.parse.urlparse(self.path).path
            name = body.get("clip")
            if path in ("/api/captions", "/api/draft", "/api/upload") and not clip_path(name):
                raise ApiError("The clip is not in the folder.")
            if path == "/api/clips/delete":
                return self.send_json({"ok": True, "freed": delete_inbox(str(body.get("file", "")))})
            if path == "/api/posted/delete":
                return self.send_json({"ok": True, "freed": delete_posted(str(body.get("file", "")))})
            if path == "/api/captions":
                return self.send_json(run_job("captions", caption_job, name, str(body.get("note", ""))[:2000]))
            if path == "/api/lint":
                return self.send_json({"lint": lint(str(body.get("text", ""))[:3000])})
            if path == "/api/draft":
                with lock:
                    s = state()
                    s["drafts"].setdefault(name, {}).update(
                        {k: body[k] for k in ("note", "selected", "ig_caption", "yt_title", "yt_description") if k in body})
                    save("state.json", s)
                return self.send_json({"ok": True})
            if path == "/api/upload":
                platforms = [p for p in body.get("platforms", []) if p in ("instagram", "youtube")]
                if not platforms:
                    raise ApiError("Select one platform or more.")
                if "instagram" in platforms and not str(body.get("ig_caption", "")).strip():
                    raise ApiError("Select or write an Instagram caption first.")
                if "youtube" in platforms and not str(body.get("yt_title", "")).strip():
                    raise ApiError("Select or write a YouTube title first.")
                if any(j["kind"] == "upload" and j["state"] == "running" and j.get("clip") == name for j in jobs.values()):
                    raise ApiError("An upload of this clip is in progress.")
                body["platforms"] = platforms
                job = run_job("upload", upload_job, name, body)
                job["clip"] = name
                return self.send_json(job)
            if path == "/api/config":
                allowed = {k: str(v).strip() for k, v in body.items()
                           if k in ("ig_token", "fb_app_id", "fb_app_secret", "yt_client_id", "yt_client_secret", "profile", "privacy")
                           and (str(v).strip() or k == "profile")}
                if "ig_token" in allowed:
                    allowed["ig_token"] = re.sub(r"[\s\"']", "", allowed["ig_token"])
                    if allowed["ig_token"].startswith("EAA"):
                        tok = allowed.pop("ig_token")
                        set_config(**allowed)
                        try:
                            return self.send_json({"ok": True, "note": fb_connect(tok)})
                        except ApiError as e:
                            print(f"facebook token refused (length {len(tok)}): {e}", flush=True)
                            raise
                    if not allowed["ig_token"].startswith("IG"):
                        t = allowed["ig_token"]
                        kind = ("the app secret or an app ID" if re.fullmatch(r"[0-9a-f]{32}|\d{10,20}", t)
                                else "not an Instagram user token")
                        print(f"instagram token refused before the test (length {len(t)}, starts {t[:4]})", flush=True)
                        raise ApiError(f"This text is {kind} (length {len(t)}). An Instagram token starts with IGAA "
                                       "and has more than 100 characters. In the Meta app, open Instagram > API setup "
                                       "with Instagram login > 2. Generate access tokens > Generate token.")
                    allowed["ig_token_time"] = time.time()
                    allowed["ig_mode"] = "ig"
                set_config(**allowed)
                if "ig_token" in allowed:
                    try:
                        ig_me()
                    except ApiError as e:
                        tok = allowed["ig_token"]
                        set_config(ig_token="", ig_username="")
                        print(f"instagram token refused (length {len(tok)}, starts {tok[:4]}): {e}", flush=True)
                        raise
                return self.send_json({"ok": True})
            if path == "/api/yt/exchange":
                code = urllib.parse.parse_qs(urllib.parse.urlparse(str(body.get("url", ""))).query).get("code", [""])[0]
                if not code:
                    raise ApiError("The pasted address has no code. Copy the full address from the browser.")
                yt_exchange(code)
                return self.send_json({"ok": True})
            if path == "/api/disconnect":
                if body.get("platform") == "instagram":
                    set_config(ig_token="", ig_username="", ig_user_id="", ig_mode="")
                elif body.get("platform") == "youtube":
                    set_config(yt_refresh_token="", yt_channel="")
                cache.clear()
                return self.send_json({"ok": True})
            self.send_json({"error": "not found"}, 404)
        except ApiError as e:
            self.send_json({"error": str(e)}, 400)
        except (ValueError, KeyError) as e:
            self.send_json({"error": f"Bad request: {e}"}, 400)


class PublicHandler(Handler):
    """Serves one clip and its cover under a secret token, only while an Instagram upload is in progress."""

    def do_GET(self):
        m = re.fullmatch(r"/v/([\w-]+)/(clip\.\w+|cover\.jpg)", urllib.parse.urlparse(self.path).path)
        p = public_tokens.get(m.group(1)) if m else None
        if p and m.group(2) == "cover.jpg":
            p = cover_path(p)                     # only the cover of the clip of this token
        if not p or not p.is_file():
            return self.send_json({"error": "not found"}, 404)
        self.send_file(p, ctype="image/jpeg" if m.group(2) == "cover.jpg" else None)

    def do_POST(self):
        self.send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    pub = ThreadingHTTPServer(("127.0.0.1", PUBLIC_PORT), PublicHandler)
    threading.Thread(target=pub.serve_forever, daemon=True).start()
    clean_cache()
    print(f"social dashboard on port {PORT}, clips in {INBOX}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
