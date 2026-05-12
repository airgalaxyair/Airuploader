# ============================================================
#  COURSE UPLOADER BOT - Web Service Mode (Koyeb Free Tier)
# ============================================================

import os
import re
import time
import asyncio
import aiohttp
import aiofiles
import logging
import subprocess
import traceback
import threading
from pathlib import Path
from bs4 import BeautifulSoup
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from pyromod import listen
from vars import API_ID, API_HASH, BOT_TOKEN, AUTH_USERS, CHANNEL_ID
from flask import Flask

# ─── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
    datefmt="%d-%b-%y %H:%M:%S"
)
logger = logging.getLogger(__name__)

# ─── Flask app (Koyeb health check) ───────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Bot is running!", 200

@flask_app.route("/health")
def health():
    return "OK", 200

# ─── Bot Client ───────────────────────────────────────────────
bot = Client(
    "CourseUploaderBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=4,
)

# ─── Temp directory ───────────────────────────────────────────
TEMP_DIR = Path("./downloads")
TEMP_DIR.mkdir(exist_ok=True)

# ─── Helpers ──────────────────────────────────────────────────

def safe_filename(name: str, max_len: int = 60) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    return name[:max_len]


def human_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def get_duration(filepath: str) -> int:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", filepath],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        return int(float(result.stdout.decode().strip()))
    except:
        return 0


async def make_thumbnail(filepath: str):
    thumb = filepath + ".jpg"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", filepath, "-ss", "00:00:05",
             "-vframes", "1", "-vf", "scale=320:-1", thumb],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return thumb if os.path.exists(thumb) else None
    except:
        return None


# ─── Progress bar ─────────────────────────────────────────────
_last_edit = {}

async def progress_cb(current, total, msg, action="Uploading", name=""):
    now = time.time()
    key = id(msg)
    if now - _last_edit.get(key, 0) < 4:
        return
    _last_edit[key] = now
    try:
        pct = current * 100 / total
        filled = int(pct / 10)
        bar = "█" * filled + "░" * (10 - filled)
        speed = current / max(now - _last_edit.get(f"{key}_start", now), 1)
        eta = int((total - current) / max(speed, 1))
        await msg.edit(
            f"**{action}** `{name[:40]}`\n\n"
            f"`[{bar}]` **{pct:.1f}%**\n"
            f"📦 `{human_size(current)}` / `{human_size(total)}`\n"
            f"⚡ `{human_size(int(speed))}/s`  ⏱ `{eta}s`"
        )
    except FloodWait as e:
        await asyncio.sleep(e.value)
    except:
        pass


# ─── HTML Parser ──────────────────────────────────────────────
def parse_html(html_content: str) -> dict:
    soup = BeautifulSoup(html_content, "html.parser")
    title = soup.find("h1")
    batch_name = title.get_text(strip=True) if title else "Unknown Batch"
    videos, pdfs, others = [], [], []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        css = a.get("class", [])

        if "video-link" in css or "master.m3u8" in href or "youtube.com" in href:
            videos.append({"name": text, "url": href})
        elif "pdf-link" in css or href.endswith(".pdf"):
            pdfs.append({"name": text, "url": href})
        elif "other-link" in css:
            others.append({"name": text, "url": href})

    return {"batch_name": batch_name, "videos": videos, "pdfs": pdfs, "others": others}


# ─── Downloaders ──────────────────────────────────────────────
async def download_pdf(url, dest, prog_msg=None, name=""):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                if resp.status != 200:
                    return None
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                _last_edit[f"{id(prog_msg)}_start"] = time.time()
                async with aiofiles.open(dest, "wb") as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        await f.write(chunk)
                        downloaded += len(chunk)
                        if prog_msg and total:
                            await progress_cb(downloaded, total, prog_msg, "Downloading PDF", name)
        return dest
    except Exception as e:
        logger.error(f"PDF download error: {e}")
        return None


def download_video_ytdlp(url, dest, name):
    out = str(dest / f"{safe_filename(name)}.%(ext)s")
    cmd = [
        "yt-dlp", "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4", "--no-playlist",
        "--retries", "10", "--fragment-retries", "10",
        "--external-downloader", "aria2c",
        "--downloader-args", "aria2c:-x 16 -j 32 -k 1M",
        "-o", out, "--no-warnings", "--quiet", url,
    ]
    try:
        r = subprocess.run(cmd, timeout=3600)
        if r.returncode != 0:
            cmd2 = [c for c in cmd if c not in ["--external-downloader", "aria2c", "--downloader-args", "aria2c:-x 16 -j 32 -k 1M"]]
            subprocess.run(cmd2, timeout=3600)
        for ext in ["mp4", "mkv", "webm"]:
            p = dest / f"{safe_filename(name)}.{ext}"
            if p.exists():
                return p
        matches = list(dest.glob(f"{safe_filename(name)}.*"))
        return matches[0] if matches else None
    except Exception as e:
        logger.error(f"yt-dlp error: {e}")
        return None


# ─── Uploaders ────────────────────────────────────────────────
async def upload_video(client, channel_id, filepath, caption, prog_msg):
    thumb = await make_thumbnail(str(filepath))
    duration = get_duration(str(filepath))
    _last_edit[f"{id(prog_msg)}_start"] = time.time()
    try:
        await client.send_video(
            chat_id=channel_id, video=str(filepath), caption=caption,
            duration=duration, thumb=thumb, supports_streaming=True,
            progress=progress_cb, progress_args=(prog_msg, "Uploading Video", filepath.name),
        )
        return True
    except FloodWait as e:
        await asyncio.sleep(e.value + 5)
        return await upload_video(client, channel_id, filepath, caption, prog_msg)
    except Exception as e:
        logger.error(f"Video upload error: {e}")
        return False
    finally:
        if thumb and os.path.exists(thumb):
            os.remove(thumb)


async def upload_pdf(client, channel_id, filepath, caption, prog_msg):
    _last_edit[f"{id(prog_msg)}_start"] = time.time()
    try:
        await client.send_document(
            chat_id=channel_id, document=str(filepath), caption=caption,
            progress=progress_cb, progress_args=(prog_msg, "Uploading PDF", filepath.name),
        )
        return True
    except FloodWait as e:
        await asyncio.sleep(e.value + 5)
        return await upload_pdf(client, channel_id, filepath, caption, prog_msg)
    except Exception as e:
        logger.error(f"PDF upload error: {e}")
        return False


# ─── Core processor ───────────────────────────────────────────
async def process_course(client, m, html_content, channel_id,
                         start_from=1, dl_videos=True, dl_pdfs=True):
    data = parse_html(html_content)
    batch = data["batch_name"]
    videos, pdfs = data["videos"], data["pdfs"]

    summary = (
        f"📚 **{batch}**\n\n"
        f"🎬 Videos: `{len(videos)}`\n"
        f"📄 PDFs: `{len(pdfs)}`\n"
        f"▶️ From: `{start_from}`"
    )
    await client.send_message(channel_id, summary)
    await m.reply(f"✅ Parsed!\n\n{summary}\n\nProcessing...")

    session_dir = TEMP_DIR / safe_filename(batch)
    session_dir.mkdir(exist_ok=True)
    done, failed = 0, []

    if dl_videos:
        for idx, item in enumerate(videos, start=1):
            if idx < start_from:
                continue
            name = item["name"] or f"Video {idx}"
            caption = f"🎬 **{idx}. {name}**\n📚 `{batch}`"
            prog = await m.reply(f"⏳ **[{idx}/{len(videos)}]** Downloading:\n`{name}`")
            try:
                video_path = await asyncio.get_event_loop().run_in_executor(
                    None, download_video_ytdlp, item["url"], session_dir, f"{idx:04d}_{name}"
                )
                if not video_path:
                    raise Exception("Download failed")
                await prog.edit(f"📤 **[{idx}/{len(videos)}]** Uploading:\n`{name}`")
                if await upload_video(client, channel_id, video_path, caption, prog):
                    done += 1
                    await prog.edit(f"✅ **[{idx}/{len(videos)}]** Done: `{name}`")
                else:
                    raise Exception("Upload failed")
            except Exception as e:
                failed.append(f"V{idx}: {name}")
                await prog.edit(f"❌ **[{idx}/{len(videos)}]** Failed: `{name}`\n`{e}`")
            finally:
                for f in session_dir.glob(f"{idx:04d}_{safe_filename(name)[:50]}*"):
                    try: f.unlink()
                    except: pass
            await asyncio.sleep(2)

    if dl_pdfs:
        for idx, item in enumerate(pdfs, start=1):
            name = item["name"] or f"PDF {idx}"
            dest = session_dir / f"pdf_{idx:04d}_{safe_filename(name)}.pdf"
            caption = f"📄 **{idx}. {name}**\n📚 `{batch}`"
            prog = await m.reply(f"⏳ **[{idx}/{len(pdfs)}]** Downloading PDF:\n`{name}`")
            try:
                path = await download_pdf(item["url"], dest, prog, name)
                if not path:
                    raise Exception("Download failed")
                await prog.edit(f"📤 **[{idx}/{len(pdfs)}]** Uploading:\n`{name}`")
                if await upload_pdf(client, channel_id, path, caption, prog):
                    done += 1
                    await prog.edit(f"✅ **[{idx}/{len(pdfs)}]** Done: `{name}`")
                else:
                    raise Exception("Upload failed")
            except Exception as e:
                failed.append(f"P{idx}: {name}")
                await prog.edit(f"❌ **[{idx}/{len(pdfs)}]** Failed: `{name}`\n`{e}`")
            finally:
                if dest.exists():
                    try: dest.unlink()
                    except: pass
            await asyncio.sleep(1)

    total = (len(videos) if dl_videos else 0) + (len(pdfs) if dl_pdfs else 0)
    result = (
        f"🏁 **Done!** `{batch}`\n\n"
        f"✅ Uploaded: `{done}/{total}`\n"
        f"❌ Failed: `{len(failed)}`"
    )
    if failed:
        result += "\n\n**Failed:**\n" + "\n".join(f"• {x}" for x in failed[:20])
    await m.reply(result)
    await client.send_message(channel_id, result)
    try:
        import shutil; shutil.rmtree(session_dir, ignore_errors=True)
    except: pass


# ─── Bot Handlers ─────────────────────────────────────────────
@bot.on_message(filters.command("start"))
async def start_cmd(client, m: Message):
    await m.reply(
        "👋 **Course Uploader Bot**\n\n"
        "Send me an HTML course file and I'll upload all videos + PDFs to your channel.\n\n"
        "**Commands:**\n/upload — Start upload\n/help — Help"
    )

@bot.on_message(filters.command("help"))
async def help_cmd(client, m: Message):
    await m.reply(
        "**How to use:**\n\n"
        "1. `/upload`\n2. Send HTML file\n"
        "3. Choose: `videos` / `pdfs` / `both`\n"
        "4. Set start number\n5. Done ✅\n\n"
        f"Channel: `{CHANNEL_ID}`"
    )

@bot.on_message(filters.command("upload") & filters.user(AUTH_USERS))
async def upload_cmd(client: Client, m: Message):
    await m.reply("📂 Send the HTML course file:")
    try:
        file_msg = await client.listen(m.chat.id, timeout=120)
    except:
        await m.reply("⏰ Timed out."); return

    if not file_msg.document:
        await m.reply("❌ Send a valid HTML file."); return

    html_path = await file_msg.download(file_name=str(TEMP_DIR / file_msg.document.file_name))
    with open(html_path, "r", encoding="utf-8", errors="ignore") as f:
        html_content = f.read()
    os.remove(html_path)

    data = parse_html(html_content)
    await m.reply(
        f"📊 **`{data['batch_name']}`**\n\n"
        f"🎬 Videos: `{len(data['videos'])}`\n"
        f"📄 PDFs: `{len(data['pdfs'])}`\n\n"
        "What to download? `videos` / `pdfs` / `both`"
    )

    try:
        choice = (await client.listen(m.chat.id, timeout=60)).text.strip().lower()
    except:
        choice = "both"

    dl_videos = "video" in choice or "both" in choice or choice in ["v", "1", "all"]
    dl_pdfs   = "pdf"   in choice or "both" in choice or choice in ["p", "2", "all"]
    if not dl_videos and not dl_pdfs:
        dl_videos = dl_pdfs = True

    await m.reply("From which number? (send `1` for beginning)")
    try:
        start_from = int((await client.listen(m.chat.id, timeout=30)).text.strip())
    except:
        start_from = 1

    await m.reply(f"Channel ID or `/d` for default (`{CHANNEL_ID}`):")
    try:
        ch_text = (await client.listen(m.chat.id, timeout=30)).text.strip()
        channel = CHANNEL_ID if "/d" in ch_text else ch_text
    except:
        channel = CHANNEL_ID

    await m.reply(
        f"🚀 **Starting!**\n\n"
        f"📚 `{data['batch_name']}`\n"
        f"🎬 Videos: `{'Yes' if dl_videos else 'No'}`\n"
        f"📄 PDFs: `{'Yes' if dl_pdfs else 'No'}`\n"
        f"▶️ From: `{start_from}`\n"
        f"📢 Channel: `{channel}`"
    )
    await process_course(client, m, html_content, channel, start_from, dl_videos, dl_pdfs)


# ─── Start bot in background (no signal handling) ─────────────
def start_bot_in_thread():
    """Run the bot using start/stop instead of run() to avoid signal issues."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run():
        await bot.start()
        logger.info("Bot started successfully in background thread")
        # Keep alive forever
        while True:
            await asyncio.sleep(60)

    try:
        loop.run_until_complete(_run())
    except Exception as e:
        logger.error(f"Bot thread error: {e}")


bot_thread = threading.Thread(target=start_bot_in_thread, daemon=True)
bot_thread.start()
logger.info("Bot background thread launched")

# ─── Flask is the main process ────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting Flask on port {port}")
    flask_app.run(host="0.0.0.0", port=port)
