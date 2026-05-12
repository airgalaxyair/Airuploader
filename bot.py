# ============================================================
#  COURSE UPLOADER BOT
#  Parses HTML course files → Downloads → Uploads to Telegram
#  Supports: Brightcove HLS videos + Direct PDFs
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
from pathlib import Path
from bs4 import BeautifulSoup
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait
from pyromod import listen
from vars import API_ID, API_HASH, BOT_TOKEN, AUTH_USERS, CHANNEL_ID

# ─── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
    datefmt="%d-%b-%y %H:%M:%S"
)
logger = logging.getLogger(__name__)

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
    return name[:max_len] if len(name) > max_len else name


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


async def make_thumbnail(filepath: str) -> str | None:
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
        text = (
            f"**{action}** `{name[:40]}`\n\n"
            f"`[{bar}]` **{pct:.1f}%**\n"
            f"📦 `{human_size(current)}` / `{human_size(total)}`\n"
            f"⚡ `{human_size(int(speed))}/s`  ⏱ `{eta}s`"
        )
        await msg.edit(text)
    except FloodWait as e:
        await asyncio.sleep(e.value)
    except:
        pass


# ─── HTML Parser ──────────────────────────────────────────────

def parse_html(html_content: str) -> dict:
    soup = BeautifulSoup(html_content, "html.parser")
    title = soup.find("h1")
    batch_name = title.get_text(strip=True) if title else "Unknown Batch"

    videos = []
    pdfs = []
    others = []

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

    return {
        "batch_name": batch_name,
        "videos": videos,
        "pdfs": pdfs,
        "others": others,
    }


# ─── Downloaders ──────────────────────────────────────────────

async def download_pdf(url: str, dest: Path, prog_msg=None, name="") -> Path | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                if resp.status != 200:
                    logger.warning(f"PDF download failed [{resp.status}]: {url}")
                    return None
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                start = time.time()
                _last_edit[f"{id(prog_msg)}_start"] = start
                async with aiofiles.open(dest, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 64):
                        await f.write(chunk)
                        downloaded += len(chunk)
                        if prog_msg and total:
                            await progress_cb(downloaded, total, prog_msg, "Downloading PDF", name)
        return dest
    except Exception as e:
        logger.error(f"PDF download error: {e}")
        return None


def download_video_ytdlp(url: str, dest: Path, name: str) -> Path | None:
    """Download HLS/YouTube video using yt-dlp + aria2c."""
    out_template = str(dest / f"{safe_filename(name)}.%(ext)s")
    cmd = [
        "yt-dlp",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--retries", "10",
        "--fragment-retries", "10",
        "--external-downloader", "aria2c",
        "--downloader-args", "aria2c:-x 16 -j 32 -k 1M",
        "-o", out_template,
        "--no-warnings",
        "--quiet",
        url,
    ]
    try:
        result = subprocess.run(cmd, timeout=3600)
        if result.returncode != 0:
            # Retry without aria2c
            cmd_fallback = [c for c in cmd if c not in ["--external-downloader", "aria2c",
                            "--downloader-args", "aria2c:-x 16 -j 32 -k 1M"]]
            subprocess.run(cmd_fallback, timeout=3600)
        # Find the downloaded file
        for ext in ["mp4", "mkv", "webm"]:
            candidate = dest / f"{safe_filename(name)}.{ext}"
            if candidate.exists():
                return candidate
        # Glob search
        matches = list(dest.glob(f"{safe_filename(name)}.*"))
        return matches[0] if matches else None
    except Exception as e:
        logger.error(f"yt-dlp error: {e}")
        return None


# ─── Uploader ─────────────────────────────────────────────────

async def upload_video(client: Client, channel_id, filepath: Path,
                       caption: str, prog_msg) -> bool:
    thumb = await make_thumbnail(str(filepath))
    duration = get_duration(str(filepath))
    size = filepath.stat().st_size
    _last_edit[f"{id(prog_msg)}_start"] = time.time()
    try:
        await client.send_video(
            chat_id=channel_id,
            video=str(filepath),
            caption=caption,
            duration=duration,
            thumb=thumb,
            supports_streaming=True,
            progress=progress_cb,
            progress_args=(prog_msg, "Uploading Video", filepath.name),
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


async def upload_pdf(client: Client, channel_id, filepath: Path,
                     caption: str, prog_msg) -> bool:
    _last_edit[f"{id(prog_msg)}_start"] = time.time()
    try:
        await client.send_document(
            chat_id=channel_id,
            document=str(filepath),
            caption=caption,
            progress=progress_cb,
            progress_args=(prog_msg, "Uploading PDF", filepath.name),
        )
        return True
    except FloodWait as e:
        await asyncio.sleep(e.value + 5)
        return await upload_pdf(client, channel_id, filepath, caption, prog_msg)
    except Exception as e:
        logger.error(f"PDF upload error: {e}")
        return False


# ─── Core processor ───────────────────────────────────────────

async def process_course(client: Client, m: Message, html_content: str,
                         channel_id, start_from: int = 1,
                         dl_videos: bool = True, dl_pdfs: bool = True):
    data = parse_html(html_content)
    batch = data["batch_name"]
    videos = data["videos"]
    pdfs = data["pdfs"]

    summary = (
        f"📚 **{batch}**\n\n"
        f"🎬 Videos: `{len(videos)}`\n"
        f"📄 PDFs: `{len(pdfs)}`\n\n"
        f"Starting from: `{start_from}`"
    )
    await client.send_message(channel_id, summary)
    await m.reply(f"✅ Parsed!\n\n{summary}\n\nProcessing...")

    session_dir = TEMP_DIR / safe_filename(batch)
    session_dir.mkdir(exist_ok=True)

    total_items = (len(videos) if dl_videos else 0) + (len(pdfs) if dl_pdfs else 0)
    done = 0
    failed = []

    # ── Videos ────────────────────────────────────────────────
    if dl_videos:
        for idx, item in enumerate(videos, start=1):
            if idx < start_from:
                continue

            name = item["name"] or f"Video {idx}"
            url = item["url"]
            caption = f"🎬 **{idx}. {name}**\n📚 `{batch}`"

            prog = await m.reply(f"⏳ **[{idx}/{len(videos)}]** Downloading video:\n`{name}`")

            try:
                # Download
                video_path = await asyncio.get_event_loop().run_in_executor(
                    None, download_video_ytdlp, url, session_dir, f"{idx:04d}_{name}"
                )
                if not video_path:
                    raise Exception("yt-dlp returned no file")

                # Upload
                await prog.edit(f"📤 **[{idx}/{len(videos)}]** Uploading video:\n`{name}`")
                ok = await upload_video(client, channel_id, video_path, caption, prog)
                if ok:
                    done += 1
                    await prog.edit(f"✅ **[{idx}/{len(videos)}]** Done: `{name}`")
                else:
                    raise Exception("Upload failed")

            except Exception as e:
                failed.append(f"V{idx}: {name}")
                await prog.edit(f"❌ **[{idx}/{len(videos)}]** Failed: `{name}`\n`{e}`")
                logger.error(traceback.format_exc())
            finally:
                # Cleanup
                for f in session_dir.glob(f"{idx:04d}_{safe_filename(name)[:50]}*"):
                    try:
                        f.unlink()
                    except:
                        pass

            await asyncio.sleep(2)

    # ── PDFs ──────────────────────────────────────────────────
    if dl_pdfs:
        for idx, item in enumerate(pdfs, start=1):
            name = item["name"] or f"PDF {idx}"
            url = item["url"]
            caption = f"📄 **{idx}. {name}**\n📚 `{batch}`"
            dest = session_dir / f"pdf_{idx:04d}_{safe_filename(name)}.pdf"

            prog = await m.reply(f"⏳ **[{idx}/{len(pdfs)}]** Downloading PDF:\n`{name}`")

            try:
                path = await download_pdf(url, dest, prog, name)
                if not path:
                    raise Exception("Download failed")

                await prog.edit(f"📤 **[{idx}/{len(pdfs)}]** Uploading PDF:\n`{name}`")
                ok = await upload_pdf(client, channel_id, path, caption, prog)
                if ok:
                    done += 1
                    await prog.edit(f"✅ **[{idx}/{len(pdfs)}]** Done: `{name}`")
                else:
                    raise Exception("Upload failed")

            except Exception as e:
                failed.append(f"P{idx}: {name}")
                await prog.edit(f"❌ **[{idx}/{len(pdfs)}]** Failed: `{name}`\n`{e}`")
            finally:
                if dest.exists():
                    try:
                        dest.unlink()
                    except:
                        pass

            await asyncio.sleep(1)

    # ── Final summary ──────────────────────────────────────────
    result = (
        f"🏁 **Done!** `{batch}`\n\n"
        f"✅ Uploaded: `{done}/{total_items}`\n"
        f"❌ Failed: `{len(failed)}`"
    )
    if failed:
        result += "\n\n**Failed items:**\n" + "\n".join(f"• {x}" for x in failed[:20])

    await m.reply(result)
    await client.send_message(channel_id, result)

    # Cleanup session dir
    try:
        import shutil
        shutil.rmtree(session_dir, ignore_errors=True)
    except:
        pass


# ─── Bot Handlers ─────────────────────────────────────────────

@bot.on_message(filters.command("start"))
async def start_cmd(client, m: Message):
    await m.reply(
        "👋 **Course Uploader Bot**\n\n"
        "Send me an HTML course file and I'll upload all videos + PDFs to the channel.\n\n"
        "**Commands:**\n"
        "/upload — Upload course from HTML file\n"
        "/help — Show help"
    )


@bot.on_message(filters.command("help"))
async def help_cmd(client, m: Message):
    await m.reply(
        "**How to use:**\n\n"
        "1. Send `/upload` command\n"
        "2. Send the HTML file (exported from course extractor)\n"
        "3. Choose what to download: Videos / PDFs / Both\n"
        "4. Set start position (e.g. `1` for beginning)\n"
        "5. Bot downloads and uploads everything to the channel\n\n"
        "**Supported video sources:**\n"
        "• Brightcove HLS (m3u8 with bcov_auth)\n"
        "• YouTube embed links\n\n"
        "**PDF sources:**\n"
        "• Direct PDF links (crwilladmin, any CDN)\n\n"
        f"**Output channel:** `{CHANNEL_ID}`"
    )


@bot.on_message(filters.command("upload") & filters.user(AUTH_USERS))
async def upload_cmd(client: Client, m: Message):
    await m.reply("📂 **Send me the HTML course file:**")

    try:
        file_msg: Message = await client.listen(m.chat.id, timeout=120)
    except Exception:
        await m.reply("⏰ Timed out. Try again.")
        return

    if not file_msg.document:
        await m.reply("❌ Please send a valid HTML file.")
        return

    # Download HTML file
    html_path = await file_msg.download(file_name=str(TEMP_DIR / file_msg.document.file_name))
    with open(html_path, "r", encoding="utf-8", errors="ignore") as f:
        html_content = f.read()
    os.remove(html_path)

    # Parse and show summary
    data = parse_html(html_content)
    summary = (
        f"📊 **Found in `{data['batch_name']}`:**\n\n"
        f"🎬 Videos: `{len(data['videos'])}`\n"
        f"📄 PDFs: `{len(data['pdfs'])}`\n"
        f"🔗 Others: `{len(data['others'])}`"
    )
    await m.reply(summary + "\n\nWhat do you want to download?")

    # Ask what to download
    choice_msg: Message = await client.listen(m.chat.id, timeout=60)
    choice = choice_msg.text.strip().lower() if choice_msg.text else "both"

    dl_videos = "video" in choice or "both" in choice or choice in ["v", "1", "all"]
    dl_pdfs = "pdf" in choice or "both" in choice or choice in ["p", "2", "all"]

    if not dl_videos and not dl_pdfs:
        dl_videos = dl_pdfs = True  # default: both

    # Ask start position
    await m.reply(
        f"From which number to start?\n"
        f"(Send `1` for beginning)\n\n"
        f"Videos: 1–{len(data['videos'])} | PDFs: 1–{len(data['pdfs'])}"
    )
    try:
        start_msg: Message = await client.listen(m.chat.id, timeout=30)
        start_from = int(start_msg.text.strip())
    except:
        start_from = 1

    # Ask target channel (or use default)
    await m.reply(
        f"Send channel ID/username to upload to, or /d for default (`{CHANNEL_ID}`):"
    )
    try:
        ch_msg: Message = await client.listen(m.chat.id, timeout=30)
        if ch_msg.text and "/d" not in ch_msg.text:
            channel = ch_msg.text.strip()
        else:
            channel = CHANNEL_ID
    except:
        channel = CHANNEL_ID

    await m.reply(
        f"🚀 **Starting!**\n\n"
        f"📚 Batch: `{data['batch_name']}`\n"
        f"🎬 Videos: `{'Yes' if dl_videos else 'No'}`\n"
        f"📄 PDFs: `{'Yes' if dl_pdfs else 'No'}`\n"
        f"▶️ Start from: `{start_from}`\n"
        f"📢 Channel: `{channel}`"
    )

    await process_course(
        client, m, html_content,
        channel_id=channel,
        start_from=start_from,
        dl_videos=dl_videos,
        dl_pdfs=dl_pdfs,
    )


# ─── Allow direct HTML file upload without command ────────────
@bot.on_message(filters.document & filters.user(AUTH_USERS))
async def auto_html(client: Client, m: Message):
    fname = m.document.file_name or ""
    if not fname.lower().endswith(".html"):
        return

    await m.reply(
        "📂 Detected HTML file! Use `/upload` command for full control,\n"
        "or I'll process it now with defaults (both videos+pdfs, from start).\n\n"
        "Reply `yes` to process now, or use `/upload` for options."
    )
    try:
        confirm: Message = await client.listen(m.chat.id, timeout=20)
        if "yes" not in confirm.text.lower():
            await m.reply("Cancelled. Use `/upload` command.")
            return
    except:
        await m.reply("Timed out. Use `/upload` command.")
        return

    html_path = await m.download(file_name=str(TEMP_DIR / fname))
    with open(html_path, "r", encoding="utf-8", errors="ignore") as f:
        html_content = f.read()
    os.remove(html_path)

    await process_course(client, m, html_content, channel_id=CHANNEL_ID)


# ─── Run ──────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("Bot starting...")
    bot.run()
