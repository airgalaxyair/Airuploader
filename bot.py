# ============================================================
#  COURSE UPLOADER BOT
#  Bot runs on main thread, Flask runs in background thread
# ============================================================

import os
import re
import time
import asyncio
import aiohttp
import aiofiles
import logging
import subprocess
import threading
from pathlib import Path
from bs4 import BeautifulSoup
from pyrogram import Client, filters, idle
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from vars import API_ID, API_HASH, BOT_TOKEN, AUTH_USERS, CHANNEL_ID
from flask import Flask

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
    datefmt="%d-%b-%y %H:%M:%S"
)
logger = logging.getLogger(__name__)

logger.info(f"API_ID     : {API_ID}")
logger.info(f"AUTH_USERS : {AUTH_USERS}")
logger.info(f"CHANNEL_ID : {CHANNEL_ID}")
logger.info(f"BOT_TOKEN  : {BOT_TOKEN[:10]}...")

# ─── Flask in background thread ───────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/")
def home(): return "Bot is running!", 200

@flask_app.route("/health")
def health(): return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Flask starting on port {port}")
    flask_app.run(host="0.0.0.0", port=port)

flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()

# ─── Bot on main thread ───────────────────────────────────────
bot = Client(
    "CourseUploaderBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=4,
)

TEMP_DIR = Path("./downloads")
TEMP_DIR.mkdir(exist_ok=True)
user_state = {}

# ─── Helpers ──────────────────────────────────────────────────
def safe_filename(name, max_len=60):
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip()[:max_len]

def human_size(b):
    for u in ["B","KB","MB","GB"]:
        if b < 1024: return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"

def get_duration(fp):
    try:
        r = subprocess.run(
            ["ffprobe","-v","error","-show_entries","format=duration",
             "-of","default=noprint_wrappers=1:nokey=1",fp],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return int(float(r.stdout.decode().strip()))
    except: return 0

async def make_thumbnail(fp):
    thumb = fp + ".jpg"
    try:
        subprocess.run(
            ["ffmpeg","-y","-i",fp,"-ss","00:00:05",
             "-vframes","1","-vf","scale=320:-1",thumb],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return thumb if os.path.exists(thumb) else None
    except: return None

_last_edit = {}
async def progress_cb(current, total, msg, action="", name=""):
    now = time.time()
    key = id(msg)
    if now - _last_edit.get(key, 0) < 4: return
    _last_edit[key] = now
    try:
        pct = current * 100 / total
        bar = "█" * int(pct/10) + "░" * (10 - int(pct/10))
        spd = current / max(now - _last_edit.get(f"{key}_s", now), 1)
        eta = int((total - current) / max(spd, 1))
        await msg.edit(
            f"**{action}** `{name[:35]}`\n"
            f"`[{bar}]` {pct:.1f}%\n"
            f"📦 {human_size(current)}/{human_size(total)} "
            f"⚡{human_size(int(spd))}/s ⏱{eta}s"
        )
    except: pass

def parse_html(html):
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    batch = h1.get_text(strip=True) if h1 else "Unknown Batch"
    videos, pdfs = [], []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        css  = a.get("class", [])
        if "video-link" in css or "master.m3u8" in href or "youtube.com/embed" in href:
            videos.append({"name": text, "url": href})
        elif "pdf-link" in css or href.endswith(".pdf"):
            pdfs.append({"name": text, "url": href})
    return {"batch_name": batch, "videos": videos, "pdfs": pdfs}

async def download_pdf(url, dest, prog_msg, name):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=300)) as r:
                if r.status != 200: return None
                total = int(r.headers.get("Content-Length", 0))
                done  = 0
                _last_edit[f"{id(prog_msg)}_s"] = time.time()
                async with aiofiles.open(dest, "wb") as f:
                    async for chunk in r.content.iter_chunked(65536):
                        await f.write(chunk)
                        done += len(chunk)
                        if total: await progress_cb(done, total, prog_msg, "⬇️ PDF", name)
        return dest
    except Exception as e:
        logger.error(f"PDF dl: {e}"); return None

def dl_video(url, dest, name):
    out = str(dest / f"{safe_filename(name)}.%(ext)s")
    cmd = ["yt-dlp","-f","bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
           "--merge-output-format","mp4","--no-playlist","--retries","10",
           "--fragment-retries","10","-o",out,"--no-warnings","--quiet",url]
    try:
        subprocess.run(cmd, timeout=3600)
        for ext in ["mp4","mkv","webm"]:
            p = dest / f"{safe_filename(name)}.{ext}"
            if p.exists(): return p
        m = list(dest.glob(f"{safe_filename(name)}.*"))
        return m[0] if m else None
    except Exception as e:
        logger.error(f"yt-dlp: {e}"); return None

async def send_video(client, cid, fp, caption, prog):
    thumb = await make_thumbnail(str(fp))
    _last_edit[f"{id(prog)}_s"] = time.time()
    try:
        await client.send_video(
            chat_id=cid, video=str(fp), caption=caption,
            duration=get_duration(str(fp)), thumb=thumb,
            supports_streaming=True, progress=progress_cb,
            progress_args=(prog, "📤 Video", fp.name))
        return True
    except FloodWait as e:
        await asyncio.sleep(e.value+5)
        return await send_video(client, cid, fp, caption, prog)
    except Exception as e:
        logger.error(f"vid upload: {e}"); return False
    finally:
        if thumb and os.path.exists(thumb): os.remove(thumb)

async def send_doc(client, cid, fp, caption, prog):
    _last_edit[f"{id(prog)}_s"] = time.time()
    try:
        await client.send_document(
            chat_id=cid, document=str(fp), caption=caption,
            progress=progress_cb,
            progress_args=(prog, "📤 PDF", fp.name))
        return True
    except FloodWait as e:
        await asyncio.sleep(e.value+5)
        return await send_doc(client, cid, fp, caption, prog)
    except Exception as e:
        logger.error(f"doc upload: {e}"); return False

async def process_course(client, m, html, channel, start_from, dl_v, dl_p):
    data   = parse_html(html)
    batch  = data["batch_name"]
    videos = data["videos"]
    pdfs   = data["pdfs"]

    header = (f"📚 **{batch}**\n"
              f"🎬 {len(videos)} videos | 📄 {len(pdfs)} PDFs\n"
              f"▶️ From #{start_from}")
    await client.send_message(channel, header)
    await m.reply(f"✅ Starting!\n\n{header}")

    sdir = TEMP_DIR / safe_filename(batch)
    sdir.mkdir(exist_ok=True)
    done, failed = 0, []

    if dl_v:
        for i, item in enumerate(videos, 1):
            if i < start_from: continue
            name = item["name"] or f"Video {i}"
            prog = await m.reply(f"⏳ Video [{i}/{len(videos)}]\n`{name}`")
            try:
                vp = await asyncio.get_event_loop().run_in_executor(
                    None, dl_video, item["url"], sdir, f"{i:04d}_{name}")
                if not vp: raise Exception("No file")
                await prog.edit(f"📤 Video [{i}/{len(videos)}]\n`{name}`")
                if await send_video(client, channel, vp,
                        f"🎬 {i}. **{name}**\n📚 `{batch}`", prog):
                    done += 1
                    await prog.edit(f"✅ [{i}/{len(videos)}] `{name}`")
                else: raise Exception("Upload failed")
            except Exception as e:
                failed.append(f"V{i}: {name}")
                await prog.edit(f"❌ [{i}/{len(videos)}] `{name}`\n{e}")
            finally:
                for f in sdir.glob(f"{i:04d}_{safe_filename(name)[:40]}*"):
                    try: f.unlink()
                    except: pass
            await asyncio.sleep(2)

    if dl_p:
        for i, item in enumerate(pdfs, 1):
            name = item["name"] or f"PDF {i}"
            dest = sdir / f"pdf_{i:04d}_{safe_filename(name)}.pdf"
            prog = await m.reply(f"⏳ PDF [{i}/{len(pdfs)}]\n`{name}`")
            try:
                path = await download_pdf(item["url"], dest, prog, name)
                if not path: raise Exception("Download failed")
                await prog.edit(f"📤 PDF [{i}/{len(pdfs)}]\n`{name}`")
                if await send_doc(client, channel, path,
                        f"📄 {i}. **{name}**\n📚 `{batch}`", prog):
                    done += 1
                    await prog.edit(f"✅ [{i}/{len(pdfs)}] `{name}`")
                else: raise Exception("Upload failed")
            except Exception as e:
                failed.append(f"P{i}: {name}")
                await prog.edit(f"❌ [{i}/{len(pdfs)}] `{name}`\n{e}")
            finally:
                if dest.exists():
                    try: dest.unlink()
                    except: pass
            await asyncio.sleep(1)

    total = (len(videos) if dl_v else 0) + (len(pdfs) if dl_p else 0)
    summary = f"🏁 **Done! {batch}**\n✅ {done}/{total}\n❌ {len(failed)}"
    if failed: summary += "\n" + "\n".join(f"• {x}" for x in failed[:15])
    await m.reply(summary)
    await client.send_message(channel, summary)
    try:
        import shutil; shutil.rmtree(sdir, ignore_errors=True)
    except: pass

# ─── Handlers ─────────────────────────────────────────────────
@bot.on_message(filters.command("start"))
async def cmd_start(client, m: Message):
    logger.info(f"/start from {m.from_user.id}")
    await m.reply(
        "👋 **Course Uploader Bot**\n\n"
        "/upload — Upload course\n"
        "/myid — Get your user ID\n"
        "/help — Help"
    )

@bot.on_message(filters.command("myid"))
async def cmd_myid(client, m: Message):
    await m.reply(f"Your user ID: `{m.from_user.id}`")

@bot.on_message(filters.command("help"))
async def cmd_help(client, m: Message):
    await m.reply(
        f"Auth users: `{AUTH_USERS}`\n"
        f"Channel: `{CHANNEL_ID}`\n\n"
        "Steps:\n1. /upload\n2. Send HTML\n"
        "3. videos/pdfs/both\n4. start number\n5. channel or /d"
    )

@bot.on_message(filters.command("upload"))
async def cmd_upload(client: Client, m: Message):
    uid = m.from_user.id
    logger.info(f"/upload from {uid}")
    if AUTH_USERS and uid not in AUTH_USERS:
        await m.reply(f"⛔ Not authorized. Your ID: `{uid}`")
        return
    user_state[uid] = {"step": "wait_file"}
    await m.reply("📂 Send the HTML course file:")

@bot.on_message(filters.command("cancel"))
async def cmd_cancel(client, m: Message):
    user_state.pop(m.from_user.id, None)
    await m.reply("❌ Cancelled.")

@bot.on_message(~filters.command(["start","help","upload","cancel","myid"]))
async def handle_input(client: Client, m: Message):
    uid = m.from_user.id
    state = user_state.get(uid)
    if not state: return
    step = state.get("step")

    if step == "wait_file":
        if not m.document or not (m.document.file_name or "").endswith(".html"):
            await m.reply("❌ Send a `.html` file.")
            return
        path = await m.download(file_name=str(TEMP_DIR / m.document.file_name))
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            html = f.read()
        os.remove(path)
        data = parse_html(html)
        state.update({"html": html, "data": data, "step": "wait_choice"})
        await m.reply(
            f"📊 **{data['batch_name']}**\n"
            f"🎬 Videos: `{len(data['videos'])}`\n"
            f"📄 PDFs: `{len(data['pdfs'])}`\n\n"
            "Reply: `videos` / `pdfs` / `both`"
        )

    elif step == "wait_choice":
        c = (m.text or "both").strip().lower()
        state["dl_v"] = "video" in c or "both" in c or c in ["v","1","all"]
        state["dl_p"] = "pdf"   in c or "both" in c or c in ["p","2","all"]
        if not state["dl_v"] and not state["dl_p"]:
            state["dl_v"] = state["dl_p"] = True
        state["step"] = "wait_start"
        await m.reply("From which number? (e.g. `1`)")

    elif step == "wait_start":
        try: state["start"] = int((m.text or "1").strip())
        except: state["start"] = 1
        state["step"] = "wait_channel"
        await m.reply(f"Channel ID or `/d` for default (`{CHANNEL_ID}`):")

    elif step == "wait_channel":
        txt = (m.text or "/d").strip()
        state["channel"] = CHANNEL_ID if "/d" in txt else txt
        user_state.pop(uid, None)
        await m.reply(
            f"🚀 Starting!\n"
            f"📚 `{state['data']['batch_name']}`\n"
            f"🎬 `{'Yes' if state['dl_v'] else 'No'}` "
            f"📄 `{'Yes' if state['dl_p'] else 'No'}`\n"
            f"▶️ From `{state['start']}` → `{state['channel']}`"
        )
        await process_course(
            client, m, state["html"], state["channel"],
            state["start"], state["dl_v"], state["dl_p"]
        )

# ─── Run ──────────────────────────────────────────────────────
async def main():
    await bot.start()
    logger.info("✅ Bot started on main thread!")
    await idle()
    await bot.stop()

if __name__ == "__main__":
    asyncio.run(main())
