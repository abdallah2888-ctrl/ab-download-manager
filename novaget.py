import os
import re
import sys
import json
import time
import math
import shutil
import queue
import hashlib
import threading
import subprocess
from pathlib import Path
from urllib.parse import urlparse, unquote, parse_qs
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# في وضع --windowed لا يوجد stdout/stderr، وبعض المكتبات (yt-dlp) تكتب عليهما
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import requests
import customtkinter as ctk
from tkinter import filedialog, messagebox
from PIL import Image, ImageDraw

try:
    import pystray
    if os.name == "nt":
        import pystray._win32  # noqa: F401  (ليضمّنه PyInstaller)
except Exception:
    pystray = None

APP_NAME = "NovaGet Downloader"
APP_VERSION = "2.0.0"
BRIDGE_PORT = 48721

DATA_DIR = Path.home() / ".novaget"
DATA_DIR.mkdir(parents=True, exist_ok=True)
ICON_PATH = DATA_DIR / "app_icon.ico"
STATE_FILE = DATA_DIR / "downloads_state.json"
CONFIG_FILE = DATA_DIR / "config.json"

DEFAULT_CONFIG = {
    "download_dir": str(Path.home() / "Downloads" / "NovaGet"),
    "threads": 6,
    "auto_capture": True,
    "auto_classify": True,
    "browser_bridge": True,
    "appearance": "Dark",
    "speed_limit_kbps": 0,
    "video_quality": "best",
    "autostart": False,
}

FILE_CATEGORIES = {
    "فيديو وصوت": {".mp4", ".mkv", ".avi", ".mov", ".webm", ".mp3", ".wav", ".flac", ".m4a"},
    "مضغوطة": {".zip", ".rar", ".7z", ".tar", ".gz", ".iso"},
    "مستندات": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv"},
    "برامج": {".exe", ".msi", ".apk", ".dmg", ".deb"},
}

# اسم العرض -> قيمة داخلية
QUALITIES = {
    "أفضل جودة": "best",
    "1080p": "1080",
    "720p": "720",
    "480p": "480",
    "360p": "360",
    "صوت MP3": "mp3",
}
QUALITY_LABELS = {v: k for k, v in QUALITIES.items()}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NovaGet/2.0"


# ---------------------------------------------------------------- أدوات مساعدة
def resource_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def find_tool(name: str):
    """يبحث عن ffmpeg / deno داخل الـ exe أو بجانبه أو في PATH."""
    for base in (resource_dir(), Path(sys.executable).resolve().parent, Path(__file__).resolve().parent):
        p = base / f"{name}.exe"
        if p.exists():
            return str(p)
    return shutil.which(name)


# لتجد yt-dlp أداة deno/ffmpeg المضمّنة
os.environ["PATH"] = str(resource_dir()) + os.pathsep + os.environ.get("PATH", "")


def set_autostart(enabled: bool):
    """تشغيل NovaGet مع ويندوز (مصغّراً في الأيقونة) عبر مفتاح Run للمستخدم الحالي."""
    if os.name != "nt":
        return
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        try:
            if enabled:
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}" --minimized'
                else:
                    cmd = f'"{sys.executable}" "{Path(__file__).resolve()}" --minimized'
                winreg.SetValueEx(key, "NovaGet", 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, "NovaGet")
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
    except Exception:
        pass


def generate_app_icon(out_path: Path):
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    images = []
    for w, h in sizes:
        img = Image.new("RGBA", (w, h), (15, 23, 42, 255))
        draw = ImageDraw.Draw(img)
        pad = max(1, w // 12)
        draw.rounded_rectangle([pad, pad, w - pad, h - pad], radius=max(3, w // 5), fill=(37, 99, 235, 255))
        cx = w // 2
        top_y = int(h * 0.22)
        stem_bot = int(h * 0.62)
        stem_w = max(2, w // 7)
        draw.rectangle([cx - stem_w, top_y, cx + stem_w, stem_bot], fill=(255, 255, 255, 255))
        head_w = max(4, w // 3)
        head_h = max(3, h // 5)
        draw.polygon(
            [(cx - head_w, stem_bot - 1), (cx + head_w, stem_bot - 1), (cx, stem_bot + head_h)],
            fill=(255, 255, 255, 255),
        )
        bar_y = int(h * 0.80)
        bar_h = max(2, h // 16)
        draw.rounded_rectangle(
            [int(w * 0.24), bar_y, int(w * 0.76), bar_y + bar_h],
            radius=max(1, bar_h // 2),
            fill=(147, 197, 253, 255),
        )
        images.append(img)
    # Pillow يحفظ في ICO الأحجام الأصغر من الصورة الأساسية، فنحفظ من الأكبر
    images[-1].save(out_path, format="ICO", sizes=sizes)


def format_size(bytes_val: float) -> str:
    if not bytes_val or bytes_val <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = min(len(units) - 1, int(math.floor(math.log(bytes_val, 1024))))
    p = math.pow(1024, i)
    return f"{round(bytes_val / p, 2)} {units[i]}"


def format_time(seconds: float) -> str:
    if not seconds or seconds <= 0 or math.isinf(seconds) or math.isnan(seconds):
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


def detect_category(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    for cat, exts in FILE_CATEGORIES.items():
        if ext in exts:
            return cat
    return "أخرى"


def sanitize_filename(name: str) -> str:
    name = unquote(name).strip()
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    return name if name else f"download_{int(time.time())}"


def extract_filename_from_response(url: str, headers) -> str:
    cd = headers.get("Content-Disposition", "")
    if cd:
        match = re.findall(r"filename\*=UTF-8''(.+)", cd, flags=re.IGNORECASE)
        if match:
            return sanitize_filename(match[0])
        match = re.findall(r'filename="?([^";]+)"?', cd, flags=re.IGNORECASE)
        if match:
            return sanitize_filename(match[0])
    base = os.path.basename(urlparse(url).path)
    if base and "." in base:
        return sanitize_filename(base)
    return f"file_{int(time.time())}.bin"


def is_video_page_url(text: str) -> bool:
    """روابط يوتيوب (فيديو / شورتس / youtu.be)."""
    if not text.startswith(("http://", "https://")) or " " in text or "\n" in text:
        return False
    u = urlparse(text)
    host = u.netloc.lower().replace("www.", "").replace("m.", "")
    if host == "youtu.be":
        return len(u.path) > 1
    if host.endswith("youtube.com"):
        if u.path == "/watch":
            return "v" in parse_qs(u.query)
        return u.path.startswith("/shorts/")
    return False


class _SilentLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


class _StopDownload(Exception):
    pass


# ---------------------------------------------------------------- المهام والمحرك
class DownloadTask:
    def __init__(self, task_id: str, url: str, filename: str, save_dir: str,
                 threads: int = 6, total_size: int = 0, downloaded: int = 0,
                 status: str = "queued", category: str = "أخرى", support_range: bool = False,
                 kind: str = "file", quality: str = "best", extra_headers: dict = None):
        self.id = task_id
        self.url = url
        self.filename = filename
        self.save_dir = save_dir
        self.threads = max(1, min(16, threads))
        self.total_size = total_size
        self.downloaded = downloaded
        self.status = status
        self.category = category
        self.support_range = support_range
        self.kind = kind          # "file" أو "video"
        self.quality = quality
        self.extra_headers = extra_headers or {}   # Cookie/Referer من كروم (في الذاكرة فقط)
        self.speed = 0.0
        self.eta = 0.0
        self.error_msg = ""
        self.run_id = 0           # يزيد عند كل تشغيل لإيقاف الخيوط القديمة
        self._pause_event = threading.Event()
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()

    @property
    def filepath(self) -> Path:
        return Path(self.save_dir) / self.filename

    def stop_requested(self, rid: int) -> bool:
        return self._cancel_event.is_set() or self._pause_event.is_set() or rid != self.run_id

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "filename": self.filename,
            "save_dir": self.save_dir,
            "threads": self.threads,
            "total_size": self.total_size,
            "downloaded": self.downloaded,
            "status": "paused" if self.status in ("downloading", "queued") else self.status,
            "category": self.category,
            "support_range": self.support_range,
            "kind": self.kind,
            "quality": self.quality,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DownloadTask":
        data = dict(data)
        data["task_id"] = data.pop("id")
        return cls(**data)


class DownloadEngine:
    def __init__(self, on_update_callback, get_config_callback):
        self.tasks: dict = {}
        self.on_update = on_update_callback
        self.get_config = get_config_callback
        self.load_state()

    # ---- الحالة
    def save_state(self):
        try:
            data = [t.to_dict() for t in self.tasks.values()]
            STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def load_state(self):
        if STATE_FILE.exists():
            try:
                raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                for item in raw:
                    try:
                        task = DownloadTask.from_dict(item)
                        self.tasks[task.id] = task
                    except Exception:
                        continue
            except Exception:
                pass

    # ---- ملفات عادية
    def probe_url(self, url: str, extra: dict = None) -> dict:
        headers = {"User-Agent": UA, **(extra or {})}
        try:
            resp = requests.head(url, headers=headers, allow_redirects=True, timeout=12)
            if resp.status_code >= 400:
                resp = requests.get(url, headers=headers, stream=True, allow_redirects=True, timeout=12)
                resp.close()
            size = int(resp.headers.get("Content-Length", 0))
            accept_ranges = resp.headers.get("Accept-Ranges", "").lower() == "bytes"
            return {
                "ok": True,
                "url": resp.url,
                "filename": extract_filename_from_response(resp.url, resp.headers),
                "size": size,
                "support_range": accept_ranges and size > 1024 * 512,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def add_task(self, url: str, custom_name: str = "", custom_dir: str = "", threads: int = 6,
                 extra_headers: dict = None) -> DownloadTask:
        cfg = self.get_config()
        info = self.probe_url(url, extra_headers)
        filename = sanitize_filename(custom_name) if custom_name else info.get("filename", f"file_{int(time.time())}.bin")
        category = detect_category(filename)

        base_dir = Path(custom_dir) if custom_dir else Path(cfg["download_dir"])
        target_dir = (base_dir / category) if (cfg.get("auto_classify", True) and not custom_dir) else base_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        final_path = target_dir / filename
        counter = 1
        while final_path.exists():
            final_path = target_dir / f"{Path(filename).stem} ({counter}){Path(filename).suffix}"
            counter += 1
        filename = final_path.name

        task_id = hashlib.md5(f"{url}_{time.time()}".encode()).hexdigest()[:12]
        task = DownloadTask(
            task_id=task_id,
            url=info.get("url", url),
            filename=filename,
            save_dir=str(target_dir),
            threads=threads,
            total_size=info.get("size", 0),
            support_range=info.get("support_range", False),
            category=category,
            extra_headers=extra_headers,
        )
        self.tasks[task_id] = task
        self.save_state()
        self.start_task(task_id)
        return task

    # ---- فيديو
    def add_video_task(self, url: str, quality: str = "best", custom_dir: str = "") -> DownloadTask:
        cfg = self.get_config()
        base_dir = Path(custom_dir) if custom_dir else Path(cfg["download_dir"])
        target_dir = (base_dir / "فيديو وصوت") if (cfg.get("auto_classify", True) and not custom_dir) else base_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        task_id = hashlib.md5(f"{url}_{time.time()}".encode()).hexdigest()[:12]
        task = DownloadTask(
            task_id=task_id,
            url=url,
            filename="جارٍ جلب معلومات الفيديو...",
            save_dir=str(target_dir),
            threads=1,
            category="فيديو وصوت",
            kind="video",
            quality=quality if quality in QUALITY_LABELS else "best",
        )
        self.tasks[task_id] = task
        self.save_state()
        self.start_task(task_id)
        return task

    # ---- التحكم
    def start_task(self, task_id: str):
        task = self.tasks.get(task_id)
        if not task or task.status == "downloading":
            return
        task._pause_event.clear()
        task._cancel_event.clear()
        task.run_id += 1
        task.status = "downloading"
        task.error_msg = ""
        threading.Thread(target=self._run_download, args=(task, task.run_id), daemon=True).start()
        self.on_update()

    def pause_task(self, task_id: str):
        task = self.tasks.get(task_id)
        if task and task.status == "downloading":
            task._pause_event.set()
            task.status = "paused"
            task.speed = 0
            self.save_state()
            self.on_update()

    def remove_task(self, task_id: str, delete_file: bool = False):
        task = self.tasks.get(task_id)
        if not task:
            return
        task._cancel_event.set()
        task.run_id += 1
        if delete_file and task.filepath.exists():
            try:
                task.filepath.unlink()
            except Exception:
                pass
        if task.kind == "file":
            for i in range(task.threads):
                part = Path(f"{task.filepath}.part{i}")
                if part.exists():
                    try:
                        part.unlink()
                    except Exception:
                        pass
        self.tasks.pop(task_id, None)
        self.save_state()
        self.on_update()

    # ---- التنفيذ
    def _run_download(self, task: DownloadTask, rid: int):
        headers = {"User-Agent": UA, **task.extra_headers}
        try:
            if task.kind == "video":
                self._download_video(task, rid)
                return
            if task.total_size == 0:
                info = self.probe_url(task.url, task.extra_headers)
                if info.get("ok"):
                    task.total_size = info["size"]
                    task.support_range = info["support_range"]
            if task.support_range and task.total_size > 0 and task.threads > 1:
                try:
                    self._download_multithreaded(task, headers, rid)
                except Exception:
                    if task.stop_requested(rid):
                        return
                    # بعض الروابط لا تقبل التجزئة: نعيد المحاولة بخيط واحد
                    for i in range(task.threads):
                        pp = Path(f"{task.filepath}.part{i}")
                        if pp.exists():
                            pp.unlink()
                    task.support_range = False
                    self._download_single(task, headers, rid)
            else:
                self._download_single(task, headers, rid)
        except Exception as e:
            if not task.stop_requested(rid):
                task.status = "error"
                task.error_msg = str(e)
                task.speed = 0
                self.save_state()
                self.on_update()

    def _download_video(self, task: DownloadTask, rid: int):
        import yt_dlp

        cfg = self.get_config()
        ffmpeg = find_tool("ffmpeg")
        deno = find_tool("deno")
        q = task.quality

        if ffmpeg:
            if q == "mp3":
                fmt = "bestaudio/best"
            elif q == "best":
                fmt = "bestvideo*+bestaudio/best"
            else:
                fmt = f"bestvideo*[height<={q}]+bestaudio/best[height<={q}]/best"
        else:  # بدون ffmpeg لا يمكن الدمج، فنأخذ صيغة جاهزة
            if q == "mp3":
                fmt = "bestaudio[ext=m4a]/bestaudio/best"
            elif q == "best":
                fmt = "best[ext=mp4]/best"
            else:
                fmt = f"best[height<={q}][ext=mp4]/best[height<={q}]/best"

        last = {"t": time.time(), "b": 0}

        def hook(d):
            if task.stop_requested(rid):
                raise _StopDownload()
            if d.get("status") == "downloading":
                title = (d.get("info_dict") or {}).get("title")
                if title and task.filename.startswith("جارٍ"):
                    task.filename = sanitize_filename(title)
                task.total_size = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                task.downloaded = d.get("downloaded_bytes") or 0
                task.speed = d.get("speed") or 0
                task.eta = d.get("eta") or 0
                now = time.time()
                if now - last["t"] >= 0.4:
                    last["t"] = now
                    self.on_update()

        opts = {
            "format": fmt,
            "outtmpl": str(Path(task.save_dir) / "%(title).150B [%(id)s].%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "logger": _SilentLogger(),
            "progress_hooks": [hook],
            "continuedl": True,
            "retries": 10,
            "fragment_retries": 10,
            "concurrent_fragment_downloads": 4,
            "windowsfilenames": True,
            "merge_output_format": "mp4",
        }
        if ffmpeg:
            opts["ffmpeg_location"] = ffmpeg
            if q == "mp3":
                opts["postprocessors"] = [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
                ]
        if deno:
            opts["js_runtimes"] = {"deno": {"path": deno}}
        limit = cfg.get("speed_limit_kbps", 0)
        if limit > 0:
            opts["ratelimit"] = limit * 1024

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(task.url, download=True)
            rd = ((info or {}).get("requested_downloads") or [{}])[0]
            fp = rd.get("filepath") or ""
            if fp:
                task.filename = os.path.basename(fp)
                task.save_dir = os.path.dirname(fp)
                task.category = detect_category(task.filename) if detect_category(task.filename) != "أخرى" else "فيديو وصوت"
            try:
                task.total_size = Path(task.filepath).stat().st_size
            except Exception:
                task.total_size = max(task.total_size, task.downloaded)
            task.downloaded = task.total_size
            task.status = "completed"
            task.speed = 0
            task.eta = 0
        except Exception as e:
            if task.stop_requested(rid):
                self.save_state()
                return
            task.status = "error"
            task.error_msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
            task.speed = 0
        self.save_state()
        self.on_update()

    def _download_single(self, task: DownloadTask, headers: dict, rid: int):
        temp_path = Path(f"{task.filepath}.part0")
        existing = temp_path.stat().st_size if temp_path.exists() else 0
        req_headers = dict(headers)
        if existing > 0 and task.support_range:
            req_headers["Range"] = f"bytes={existing}-"
        else:
            existing = 0

        last_time = time.time()
        task.downloaded = existing
        last_bytes = existing

        with requests.get(task.url, headers=req_headers, stream=True, timeout=20) as r:
            r.raise_for_status()
            mode = "wb"
            if existing > 0:
                if r.status_code == 206:
                    mode = "ab"
                else:  # الخادم تجاهل Range: نبدأ من الصفر حتى لا يفسد الملف
                    task.downloaded = 0
                    last_bytes = 0
            if task.total_size == 0:
                task.total_size = int(r.headers.get("Content-Length", 0))

            with open(temp_path, mode) as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if task.stop_requested(rid):
                        self.save_state()
                        return
                    if chunk:
                        f.write(chunk)
                        task.downloaded += len(chunk)
                        self._throttle_speed(len(chunk))
                        now = time.time()
                        if now - last_time >= 0.4:
                            task.speed = (task.downloaded - last_bytes) / (now - last_time)
                            if task.total_size > 0 and task.speed > 0:
                                task.eta = (task.total_size - task.downloaded) / task.speed
                            last_time = now
                            last_bytes = task.downloaded
                            self.on_update()

        if temp_path.exists():
            temp_path.replace(task.filepath)
        task.status = "completed"
        task.speed = 0
        task.eta = 0
        if task.total_size == 0:
            task.total_size = task.downloaded
        self.save_state()
        self.on_update()

    def _download_multithreaded(self, task: DownloadTask, headers: dict, rid: int):
        num_parts = task.threads
        part_size = task.total_size // num_parts
        ranges = [
            (i, i * part_size, task.total_size - 1 if i == num_parts - 1 else (i + 1) * part_size - 1)
            for i in range(num_parts)
        ]

        task.downloaded = sum(
            Path(f"{task.filepath}.part{i}").stat().st_size
            for i, _, _ in ranges
            if Path(f"{task.filepath}.part{i}").exists()
        )

        stop_monitor = threading.Event()

        def monitor_progress():
            last_t = time.time()
            last_b = task.downloaded
            while not stop_monitor.is_set():
                time.sleep(0.4)
                now = time.time()
                with task._lock:
                    cur_b = task.downloaded
                task.speed = max(0.0, (cur_b - last_b) / max(0.05, now - last_t))
                if task.speed > 0 and task.total_size > cur_b:
                    task.eta = (task.total_size - cur_b) / task.speed
                last_t = now
                last_b = cur_b
                self.on_update()

        threading.Thread(target=monitor_progress, daemon=True).start()

        def download_part(part_idx: int, start: int, end: int):
            part_path = Path(f"{task.filepath}.part{part_idx}")
            existing = part_path.stat().st_size if part_path.exists() else 0
            cur_start = start + existing
            if cur_start > end:
                return
            req_headers = dict(headers)
            req_headers["Range"] = f"bytes={cur_start}-{end}"

            with requests.get(task.url, headers=req_headers, stream=True, timeout=20) as r:
                r.raise_for_status()
                if r.status_code != 206:
                    raise RuntimeError("الخادم لا يدعم التحميل المجزأ")
                with open(part_path, "ab") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if task.stop_requested(rid):
                            return
                        if chunk:
                            f.write(chunk)
                            with task._lock:
                                task.downloaded += len(chunk)
                            self._throttle_speed(len(chunk) * num_parts)

        try:
            with ThreadPoolExecutor(max_workers=num_parts) as executor:
                futures = [executor.submit(download_part, idx, s, e) for idx, s, e in ranges]
                for fut in futures:
                    fut.result()
        finally:
            stop_monitor.set()

        if task.stop_requested(rid):
            self.save_state()
            return

        with open(task.filepath, "wb") as outfile:
            for i in range(num_parts):
                part_path = Path(f"{task.filepath}.part{i}")
                with open(part_path, "rb") as infile:
                    while True:
                        buf = infile.read(1024 * 1024)
                        if not buf:
                            break
                        outfile.write(buf)
                part_path.unlink()

        task.status = "completed"
        task.downloaded = task.total_size
        task.speed = 0
        task.eta = 0
        self.save_state()
        self.on_update()

    def _throttle_speed(self, chunk_bytes: int):
        limit_kbps = self.get_config().get("speed_limit_kbps", 0)
        if limit_kbps > 0:
            sleep_time = chunk_bytes / (limit_kbps * 1024)
            if sleep_time > 0:
                time.sleep(sleep_time)


# ---------------------------------------------------------------- جسر المتصفح
def start_browser_bridge(app_ref):
    class BridgeHandler(BaseHTTPRequestHandler):
        def _reply(self, code: int, body: bytes = b""):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _trusted(self) -> bool:
            # المواقع العادية لا تستطيع إرسال هذا الهيدر دون preflight، وإضافة كروم فقط تستطيع
            origin = self.headers.get("Origin", "")
            if self.headers.get("X-NovaGet") != "1":
                return False
            return origin == "" or origin.startswith("chrome-extension://")

        def do_GET(self):
            if self.path == "/ping":
                self._reply(200, b'{"ok":true}')
            else:
                self._reply(404)

        def do_POST(self):
            if self.path not in ("/download", "/video") or not self._trusted():
                self._reply(403)
                return
            if not app_ref.cfg.get("browser_bridge", True):
                self._reply(403)
                return
            try:
                length = min(int(self.headers.get("Content-Length", 0)), 65536)
                payload = json.loads(self.rfile.read(length).decode("utf-8", errors="ignore"))
                url = (payload.get("url") or "").strip()
                if not url.startswith(("http://", "https://")):
                    raise ValueError("bad url")
                if self.path == "/video":
                    quality = payload.get("quality") or app_ref.cfg.get("video_quality", "best")
                    threading.Thread(
                        target=app_ref.engine.add_video_task, args=(url, quality), daemon=True
                    ).start()
                else:
                    filename = (payload.get("filename") or "").strip()
                    raw = payload.get("headers") or {}
                    extra = {k: v for k, v in raw.items()
                             if k in ("Cookie", "Referer", "User-Agent") and isinstance(v, str) and v}
                    threading.Thread(
                        target=app_ref.engine.add_task,
                        args=(url, filename, "", app_ref.cfg.get("threads", 6), extra),
                        daemon=True,
                    ).start()
                self._reply(200, b'{"ok":true}')
            except Exception:
                self._reply(400)

        def log_message(self, format, *args):
            return

    try:
        server = ThreadingHTTPServer(("127.0.0.1", BRIDGE_PORT), BridgeHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    except Exception:
        pass


# ---------------------------------------------------------------- النوافذ
class AddDownloadDialog(ctk.CTkToplevel):
    def __init__(self, parent, engine: DownloadEngine, prefill_url: str = "", auto_captured: bool = False):
        super().__init__(parent)
        self.engine = engine
        if ICON_PATH.exists():
            self.after(150, lambda: self.iconbitmap(str(ICON_PATH)))
        self.title("التقاط رابط تلقائي" if auto_captured else "إضافة تحميل جديد")
        self.geometry("540x320")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.grab_set()
        self.grid_columnconfigure(0, weight=1)

        header_text = "⚡ تم التقاط رابط من الحافظة" if auto_captured else "➕ إضافة رابط تحميل جديد"
        ctk.CTkLabel(self, text=header_text, font=ctk.CTkFont(size=18, weight="bold")).grid(
            row=0, column=0, padx=20, pady=(18, 10), sticky="e"
        )

        self.url_entry = ctk.CTkEntry(self, placeholder_text="الصق رابط التحميل المباشر هنا (https://...)", height=38)
        self.url_entry.grid(row=1, column=0, padx=20, pady=6, sticky="ew")
        if prefill_url:
            self.url_entry.insert(0, prefill_url)

        self.name_entry = ctk.CTkEntry(self, placeholder_text="اسم الملف (اختياري - يُكتشف تلقائياً)", height=36)
        self.name_entry.grid(row=2, column=0, padx=20, pady=6, sticky="ew")

        dir_frame = ctk.CTkFrame(self, fg_color="transparent")
        dir_frame.grid(row=3, column=0, padx=20, pady=6, sticky="ew")
        dir_frame.grid_columnconfigure(0, weight=1)
        self.dir_entry = ctk.CTkEntry(dir_frame, placeholder_text="مجلد الحفظ الافتراضي", height=36)
        self.dir_entry.grid(row=0, column=0, padx=(0, 8), sticky="ew")
        ctk.CTkButton(dir_frame, text="استعراض...", width=95, height=36, command=self.browse_dir).grid(row=0, column=1)

        threads_frame = ctk.CTkFrame(self, fg_color="transparent")
        threads_frame.grid(row=4, column=0, padx=20, pady=8, sticky="ew")
        self.threads_var = ctk.IntVar(value=parent.cfg.get("threads", 6))
        ctk.CTkLabel(threads_frame, text="عدد أجزاء التسريع (Threads):").pack(side="right", padx=(10, 0))
        seg = ctk.CTkSegmentedButton(
            threads_frame, values=["1", "4", "6", "8", "12"], command=lambda v: self.threads_var.set(int(v))
        )
        seg.set(str(self.threads_var.get()) if str(self.threads_var.get()) in ["1", "4", "6", "8", "12"] else "6")
        seg.pack(side="left")

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=5, column=0, padx=20, pady=(14, 18), sticky="ew")
        ctk.CTkButton(
            btn_frame, text="إلغاء", fg_color="#3A3F4B", hover_color="#4B5160", width=110, height=38, command=self.destroy
        ).pack(side="left")
        ctk.CTkButton(
            btn_frame, text="🚀 بدء التحميل", width=160, height=38, font=ctk.CTkFont(weight="bold"), command=self.start_download
        ).pack(side="right")

    def browse_dir(self):
        folder = filedialog.askdirectory()
        if folder:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, folder)

    def start_download(self):
        url = self.url_entry.get().strip()
        if not url.startswith(("http://", "https://")):
            messagebox.showwarning("رابط غير صالح", "يرجى إدخال رابط يبدأ بـ http:// أو https://")
            return
        threading.Thread(
            target=self.engine.add_task,
            args=(url, self.name_entry.get().strip(), self.dir_entry.get().strip(), self.threads_var.get()),
            daemon=True,
        ).start()
        self.destroy()


class AddVideoDialog(ctk.CTkToplevel):
    def __init__(self, parent, engine: DownloadEngine, prefill_url: str = "", auto_captured: bool = False):
        super().__init__(parent)
        self.engine = engine
        self.parent_app = parent
        if ICON_PATH.exists():
            self.after(150, lambda: self.iconbitmap(str(ICON_PATH)))
        self.title("فيديو من الحافظة" if auto_captured else "تحميل فيديو")
        self.geometry("540x300")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.grab_set()
        self.grid_columnconfigure(0, weight=1)

        header = "🎬 تم التقاط رابط فيديو" if auto_captured else "🎬 تحميل فيديو (يوتيوب وغيره)"
        ctk.CTkLabel(self, text=header, font=ctk.CTkFont(size=18, weight="bold")).grid(
            row=0, column=0, padx=20, pady=(18, 10), sticky="e"
        )

        self.url_entry = ctk.CTkEntry(self, placeholder_text="الصق رابط الفيديو هنا", height=38)
        self.url_entry.grid(row=1, column=0, padx=20, pady=6, sticky="ew")
        if prefill_url:
            self.url_entry.insert(0, prefill_url)

        q_frame = ctk.CTkFrame(self, fg_color="transparent")
        q_frame.grid(row=2, column=0, padx=20, pady=6, sticky="ew")
        ctk.CTkLabel(q_frame, text="الجودة:").pack(side="right", padx=(10, 0))
        self.q_menu = ctk.CTkOptionMenu(q_frame, values=list(QUALITIES.keys()), width=160)
        self.q_menu.set(QUALITY_LABELS.get(parent.cfg.get("video_quality", "best"), "أفضل جودة"))
        self.q_menu.pack(side="left")

        dir_frame = ctk.CTkFrame(self, fg_color="transparent")
        dir_frame.grid(row=3, column=0, padx=20, pady=6, sticky="ew")
        dir_frame.grid_columnconfigure(0, weight=1)
        self.dir_entry = ctk.CTkEntry(dir_frame, placeholder_text="مجلد الحفظ الافتراضي", height=36)
        self.dir_entry.grid(row=0, column=0, padx=(0, 8), sticky="ew")
        ctk.CTkButton(dir_frame, text="استعراض...", width=95, height=36, command=self.browse_dir).grid(row=0, column=1)

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=4, column=0, padx=20, pady=(16, 18), sticky="ew")
        ctk.CTkButton(
            btn_frame, text="إلغاء", fg_color="#3A3F4B", hover_color="#4B5160", width=110, height=38, command=self.destroy
        ).pack(side="left")
        ctk.CTkButton(
            btn_frame, text="🚀 تحميل الفيديو", width=170, height=38, font=ctk.CTkFont(weight="bold"), command=self.start_download
        ).pack(side="right")

    def browse_dir(self):
        folder = filedialog.askdirectory()
        if folder:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, folder)

    def start_download(self):
        url = self.url_entry.get().strip()
        if not url.startswith(("http://", "https://")):
            messagebox.showwarning("رابط غير صالح", "يرجى إدخال رابط يبدأ بـ http:// أو https://")
            return
        quality = QUALITIES.get(self.q_menu.get(), "best")
        self.parent_app.cfg["video_quality"] = quality
        self.parent_app.save_config()
        threading.Thread(
            target=self.engine.add_video_task, args=(url, quality, self.dir_entry.get().strip()), daemon=True
        ).start()
        self.destroy()


class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        if ICON_PATH.exists():
            self.after(150, lambda: self.iconbitmap(str(ICON_PATH)))
        self.title("إعدادات NovaGet")
        self.geometry("500x440")
        self.resizable(False, False)
        self.grab_set()
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self, text="⚙️ إعدادات البرنامج", font=ctk.CTkFont(size=18, weight="bold")).grid(
            row=0, column=0, padx=20, pady=(16, 12), sticky="e"
        )

        dir_frame = ctk.CTkFrame(self, fg_color="transparent")
        dir_frame.grid(row=1, column=0, padx=20, pady=6, sticky="ew")
        dir_frame.grid_columnconfigure(0, weight=1)
        self.dir_entry = ctk.CTkEntry(dir_frame, height=35)
        self.dir_entry.insert(0, parent.cfg["download_dir"])
        self.dir_entry.grid(row=0, column=0, padx=(0, 8), sticky="ew")
        ctk.CTkButton(dir_frame, text="تغيير المجلد", width=100, command=self.choose_dir).grid(row=0, column=1)

        self.auto_cap_var = ctk.BooleanVar(value=parent.cfg["auto_capture"])
        ctk.CTkSwitch(self, text="الالتقاط التلقائي للروابط من الحافظة", variable=self.auto_cap_var).grid(
            row=2, column=0, padx=20, pady=6, sticky="e"
        )

        self.bridge_var = ctk.BooleanVar(value=parent.cfg.get("browser_bridge", True))
        ctk.CTkSwitch(self, text="استقبال التحميلات من إضافة Google Chrome", variable=self.bridge_var).grid(
            row=3, column=0, padx=20, pady=6, sticky="e"
        )

        self.auto_cls_var = ctk.BooleanVar(value=parent.cfg["auto_classify"])
        ctk.CTkSwitch(self, text="تصنيف الملفات تلقائياً في مجلدات", variable=self.auto_cls_var).grid(
            row=4, column=0, padx=20, pady=6, sticky="e"
        )

        speed_frame = ctk.CTkFrame(self, fg_color="transparent")
        speed_frame.grid(row=5, column=0, padx=20, pady=6, sticky="ew")
        ctk.CTkLabel(speed_frame, text="حد السرعة (KB/s) [0 = غير محدود]:").pack(side="right", padx=(10, 0))
        self.speed_entry = ctk.CTkEntry(speed_frame, width=100)
        self.speed_entry.insert(0, str(parent.cfg.get("speed_limit_kbps", 0)))
        self.speed_entry.pack(side="left")

        theme_frame = ctk.CTkFrame(self, fg_color="transparent")
        theme_frame.grid(row=6, column=0, padx=20, pady=6, sticky="ew")
        ctk.CTkLabel(theme_frame, text="مظهر الواجهة:").pack(side="right", padx=(10, 0))
        self.theme_menu = ctk.CTkOptionMenu(theme_frame, values=["Dark", "Light"])
        self.theme_menu.set(parent.cfg.get("appearance", "Dark"))
        self.theme_menu.pack(side="left")

        self.autostart_var = ctk.BooleanVar(value=parent.cfg.get("autostart", False))
        ctk.CTkSwitch(self, text="تشغيل NovaGet مع ويندوز (مصغّراً بجانب الساعة)", variable=self.autostart_var).grid(
            row=7, column=0, padx=20, pady=6, sticky="e"
        )

        ctk.CTkButton(self, text="💾 حفظ الإعدادات", height=38, command=self.save).grid(
            row=8, column=0, padx=20, pady=(16, 16), sticky="ew"
        )

    def choose_dir(self):
        d = filedialog.askdirectory()
        if d:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, d)

    def save(self):
        try:
            speed = max(0, int(self.speed_entry.get().strip()))
        except ValueError:
            speed = 0
        cfg = self.parent.cfg
        cfg["download_dir"] = self.dir_entry.get().strip() or DEFAULT_CONFIG["download_dir"]
        cfg["auto_capture"] = self.auto_cap_var.get()
        cfg["browser_bridge"] = self.bridge_var.get()
        cfg["auto_classify"] = self.auto_cls_var.get()
        cfg["speed_limit_kbps"] = speed
        cfg["appearance"] = self.theme_menu.get()
        cfg["autostart"] = self.autostart_var.get()
        set_autostart(cfg["autostart"])
        self.parent.save_config()
        ctk.set_appearance_mode(cfg["appearance"])
        self.parent.update_capture_badge()
        self.destroy()


class TaskCard(ctk.CTkFrame):
    ICONS = {"فيديو وصوت": "🎬", "مضغوطة": "📦", "مستندات": "📄", "برامج": "💿"}

    def __init__(self, parent, task: DownloadTask, engine: DownloadEngine):
        super().__init__(
            parent, corner_radius=12, fg_color=("#EAECEF", "#1E222A"), border_width=1, border_color=("#D0D5DD", "#2D333F")
        )
        self.task = task
        self.engine = engine
        self.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, padx=14, pady=(10, 4), sticky="ew")
        top.grid_columnconfigure(0, weight=1)

        self.title_lbl = ctk.CTkLabel(top, text="", font=ctk.CTkFont(size=14, weight="bold"), anchor="w")
        self.title_lbl.grid(row=0, column=0, sticky="w")

        self.speed_lbl = ctk.CTkLabel(top, text="", font=ctk.CTkFont(size=12, weight="bold"), text_color="#3B82F6")
        self.speed_lbl.grid(row=0, column=1, padx=(10, 0), sticky="e")

        self.progress = ctk.CTkProgressBar(self, height=8, corner_radius=4)
        self.progress.grid(row=1, column=0, padx=14, pady=6, sticky="ew")

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=2, column=0, padx=14, pady=(2, 10), sticky="ew")
        bottom.grid_columnconfigure(0, weight=1)

        self.info_lbl = ctk.CTkLabel(bottom, text="", font=ctk.CTkFont(size=12), text_color=("#475467", "#98A2B3"), anchor="w")
        self.info_lbl.grid(row=0, column=0, sticky="w")

        btns = ctk.CTkFrame(bottom, fg_color="transparent")
        btns.grid(row=0, column=1, sticky="e")

        self.toggle_btn = ctk.CTkButton(btns, text="⏸️ إيقاف", width=78, height=28, font=ctk.CTkFont(size=12), command=self.toggle_pause_resume)
        self.toggle_btn.pack(side="left", padx=4)

        self.open_btn = ctk.CTkButton(
            btns, text="📂 المجلد", width=75, height=28, font=ctk.CTkFont(size=12), fg_color="#334155", hover_color="#475569", command=self.open_folder
        )
        self.open_btn.pack(side="left", padx=4)

        self.del_btn = ctk.CTkButton(
            btns, text="🗑️", width=34, height=28, font=ctk.CTkFont(size=12), fg_color="#7F1D1D", hover_color="#991B1B", command=self.delete_task
        )
        self.del_btn.pack(side="left", padx=(4, 0))

        self.refresh()

    def refresh(self):
        t = self.task
        icon = self.ICONS.get(t.category, "📁")
        self.title_lbl.configure(text=f"{icon}  {t.filename}")

        pct = (t.downloaded / t.total_size) if t.total_size > 0 else (1.0 if t.status == "completed" else 0.0)
        pct = max(0.0, min(1.0, pct))
        self.progress.set(pct)

        if t.status == "downloading":
            self.progress.configure(progress_color="#3B82F6")
            self.speed_lbl.configure(text=f"⚡ {format_size(t.speed)}/s  |  باقٍ: {format_time(t.eta)}", text_color="#3B82F6")
            self.toggle_btn.configure(text="⏸️ إيقاف", state="normal", fg_color="#D97706", hover_color="#B45309")
        elif t.status == "paused":
            self.progress.configure(progress_color="#F59E0B")
            self.speed_lbl.configure(text="⏸️ متوقف مؤقتاً", text_color="#F59E0B")
            self.toggle_btn.configure(text="▶️ استئناف", state="normal", fg_color="#2563EB", hover_color="#1D4ED8")
        elif t.status == "completed":
            self.progress.configure(progress_color="#10B981")
            self.speed_lbl.configure(text="✅ مكتمل", text_color="#10B981")
            self.toggle_btn.configure(text="✔️ تم", state="disabled", fg_color="#065F46")
        elif t.status == "error":
            self.progress.configure(progress_color="#EF4444")
            self.speed_lbl.configure(text="❌ خطأ في التحميل", text_color="#EF4444")
            self.toggle_btn.configure(text="🔄 إعادة", state="normal", fg_color="#2563EB")

        if t.status == "error" and t.error_msg:
            self.info_lbl.configure(text=t.error_msg[:110])
            return
        total_str = format_size(t.total_size) if t.total_size > 0 else "غير معروف"
        pct_str = f"{int(pct * 100)}%" if t.total_size > 0 else ""
        extra = f"الجودة: {QUALITY_LABELS.get(t.quality, t.quality)}" if t.kind == "video" else f"الأجزاء: {t.threads}"
        self.info_lbl.configure(
            text=f"{format_size(t.downloaded)} / {total_str}  {pct_str}   •   التصنيف: {t.category}   •   {extra}"
        )

    def toggle_pause_resume(self):
        if self.task.status == "downloading":
            self.engine.pause_task(self.task.id)
        else:
            self.engine.start_task(self.task.id)

    def open_folder(self):
        folder = Path(self.task.save_dir)
        if folder.exists():
            if os.name == "nt":
                os.startfile(str(folder))
            else:
                subprocess.Popen(["xdg-open", str(folder)])

    def delete_task(self):
        self.engine.remove_task(self.task.id, delete_file=False)


class NovaGetApp(ctk.CTk):
    def __init__(self, start_minimized: bool = False):
        super().__init__()
        generate_app_icon(ICON_PATH)
        try:
            self.iconbitmap(str(ICON_PATH))
        except Exception:
            pass

        # ملاحظة: لا نسميه self.config لأنه اسم دالة داخل Tkinter
        self.cfg = self.load_config()
        ctk.set_appearance_mode(self.cfg.get("appearance", "Dark"))
        ctk.set_default_color_theme("blue")

        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.geometry("1020x640")
        self.minsize(860, 520)

        self.current_filter = "الكل"
        self.search_query = ""
        self.cards: dict = {}
        self.last_clipboard = ""
        self._ui_dirty = False

        self.engine = DownloadEngine(on_update_callback=self.mark_ui_dirty, get_config_callback=lambda: self.cfg)
        start_browser_bridge(self)

        self._build_layout()
        self.rebuild_list()
        self.after(250, self._ui_loop)
        self.after(1000, self._clipboard_watcher_loop)
        self._setup_tray()
        if start_minimized and self.tray:
            self.after(300, self.withdraw)

    # ---- أيقونة منطقة الإشعارات (بجانب الساعة)
    def _tray_call(self, fn):
        self._tray_queue.put(fn)

    def _setup_tray(self):
        self._tray_queue = queue.Queue()
        self.tray = None
        if pystray is None:
            return
        try:
            image = Image.open(ICON_PATH).convert("RGBA").resize((64, 64))
            menu = pystray.Menu(
                pystray.MenuItem("فتح NovaGet", lambda i, it: self._tray_call(self.show_window), default=True),
                pystray.MenuItem("إيقاف الكل", lambda i, it: self._tray_call(self.pause_all)),
                pystray.MenuItem("استئناف الكل", lambda i, it: self._tray_call(self.resume_all)),
                pystray.MenuItem(
                    "التقاط تحميلات كروم",
                    lambda i, it: self._tray_call(self.toggle_bridge),
                    checked=lambda it: self.cfg.get("browser_bridge", True),
                ),
                pystray.MenuItem("خروج", lambda i, it: self._tray_call(self.quit_app)),
            )
            self.tray = pystray.Icon("NovaGet", image, "NovaGet", menu)
            self.tray.run_detached()
            # زر الإغلاق X يخفي النافذة بدل إنهاء البرنامج (الخروج من قائمة الأيقونة)
            self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        except Exception:
            self.tray = None

    def show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def hide_to_tray(self):
        self.withdraw()

    def toggle_bridge(self):
        self.cfg["browser_bridge"] = not self.cfg.get("browser_bridge", True)
        self.save_config()
        self.update_capture_badge()
        if self.tray:
            self.tray.update_menu()

    def quit_app(self):
        self.engine.save_state()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.destroy()

    def load_config(self) -> dict:
        if CONFIG_FILE.exists():
            try:
                return {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text(encoding="utf-8"))}
            except Exception:
                pass
        return dict(DEFAULT_CONFIG)

    def save_config(self):
        CONFIG_FILE.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    def _build_layout(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        topbar = ctk.CTkFrame(self, height=62, corner_radius=0, fg_color=("#F8FAFC", "#14171D"))
        topbar.grid(row=0, column=0, columnspan=2, sticky="ew")
        topbar.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(topbar, text="⚡ NovaGet", font=ctk.CTkFont(size=21, weight="bold"), text_color="#3B82F6").grid(
            row=0, column=0, padx=22, pady=14
        )

        self.search_entry = ctk.CTkEntry(topbar, placeholder_text="🔍 ابحث باسم الملف...", height=36)
        self.search_entry.grid(row=0, column=1, padx=20, pady=12, sticky="ew")
        self.search_entry.bind("<KeyRelease>", self.on_search)

        actions = ctk.CTkFrame(topbar, fg_color="transparent")
        actions.grid(row=0, column=2, padx=18, pady=12)

        ctk.CTkButton(actions, text="➕ رابط جديد", height=36, font=ctk.CTkFont(weight="bold"),
                      command=lambda: AddDownloadDialog(self, self.engine)).pack(side="left", padx=5)
        ctk.CTkButton(actions, text="🎬 فيديو", height=36, font=ctk.CTkFont(weight="bold"),
                      fg_color="#DC2626", hover_color="#B91C1C",
                      command=lambda: AddVideoDialog(self, self.engine)).pack(side="left", padx=5)
        ctk.CTkButton(
            actions, text="⚙️", width=40, height=36, fg_color="#334155", hover_color="#475569", command=lambda: SettingsDialog(self)
        ).pack(side="left", padx=5)

        sidebar = ctk.CTkFrame(self, width=215, corner_radius=0, fg_color=("#F1F5F9", "#171A21"))
        sidebar.grid(row=1, column=0, sticky="ns")
        sidebar.grid_propagate(False)

        ctk.CTkLabel(sidebar, text="التصنيفات", font=ctk.CTkFont(size=13, weight="bold"), text_color="#94A3B8").pack(
            anchor="e", padx=18, pady=(18, 8)
        )

        filters = [
            ("📂 الكل", "الكل"),
            ("⬇️ قيد التحميل", "قيد التحميل"),
            ("✅ المكتملة", "المكتملة"),
            ("🎬 فيديو وصوت", "فيديو وصوت"),
            ("📦 مضغوطة", "مضغوطة"),
            ("📄 مستندات", "مستندات"),
            ("💿 برامج", "برامج"),
        ]
        self.filter_buttons = {}
        for label, key in filters:
            btn = ctk.CTkButton(
                sidebar, text=label, anchor="e", height=36, corner_radius=8, fg_color="transparent",
                text_color=("#1E293B", "#E2E8F0"), hover_color=("#E2E8F0", "#252A34"),
                command=lambda k=key: self.set_filter(k),
            )
            btn.pack(fill="x", padx=12, pady=3)
            self.filter_buttons[key] = btn

        self._highlight_active_filter()

        self.capture_badge = ctk.CTkLabel(sidebar, text="", font=ctk.CTkFont(size=12, weight="bold"))
        self.capture_badge.pack(side="bottom", pady=16, padx=12)
        self.update_capture_badge()

        main_area = ctk.CTkFrame(self, corner_radius=0, fg_color=("#FFFFFF", "#111318"))
        main_area.grid(row=1, column=1, sticky="nsew")
        main_area.grid_columnconfigure(0, weight=1)
        main_area.grid_rowconfigure(1, weight=1)

        bulk_bar = ctk.CTkFrame(main_area, fg_color="transparent")
        bulk_bar.grid(row=0, column=0, padx=20, pady=(14, 6), sticky="ew")

        self.section_title = ctk.CTkLabel(bulk_bar, text="جميع التحميلات", font=ctk.CTkFont(size=16, weight="bold"))
        self.section_title.pack(side="right")

        ctk.CTkButton(bulk_bar, text="⏸️ إيقاف الكل", width=95, height=30, fg_color="#334155",
                      hover_color="#475569", command=self.pause_all).pack(side="left", padx=4)
        ctk.CTkButton(bulk_bar, text="▶️ استئناف الكل", width=105, height=30, fg_color="#2563EB",
                      hover_color="#1D4ED8", command=self.resume_all).pack(side="left", padx=4)

        self.scroll_frame = ctk.CTkScrollableFrame(main_area, fg_color="transparent")
        self.scroll_frame.grid(row=1, column=0, padx=16, pady=8, sticky="nsew")
        self.scroll_frame.grid_columnconfigure(0, weight=1)

        self.empty_label = ctk.CTkLabel(
            self.scroll_frame,
            text="لا توجد تحميلات هنا حالياً.\nانسخ رابطاً، أو اضغط زر «NovaGet» تحت فيديو يوتيوب في كروم!",
            font=ctk.CTkFont(size=14),
            text_color="#64748B",
        )

        statusbar = ctk.CTkFrame(self, height=32, corner_radius=0, fg_color=("#F1F5F9", "#14171D"))
        statusbar.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.status_lbl = ctk.CTkLabel(statusbar, text="جاهز", font=ctk.CTkFont(size=12), text_color="#94A3B8")
        self.status_lbl.pack(side="right", padx=18, pady=4)

    def update_capture_badge(self):
        status = []
        if self.cfg.get("auto_capture", True):
            status.append("الحافظة")
        if self.cfg.get("browser_bridge", True):
            status.append("Chrome")
        if status:
            self.capture_badge.configure(text=f"🟢 التقاط: {' + '.join(status)}", text_color="#10B981")
        else:
            self.capture_badge.configure(text="⚪ الالتقاط التلقائي: متوقف", text_color="#94A3B8")

    def _highlight_active_filter(self):
        for k, btn in self.filter_buttons.items():
            if k == self.current_filter:
                btn.configure(fg_color=("#DBEAFE", "#1E3A8A"), text_color=("#1D4ED8", "#93C5FD"))
            else:
                btn.configure(fg_color="transparent", text_color=("#1E293B", "#E2E8F0"))

    def set_filter(self, key: str):
        self.current_filter = key
        self.section_title.configure(text=f"التصنيف: {key}")
        self._highlight_active_filter()
        self.rebuild_list()

    def on_search(self, _event=None):
        self.search_query = self.search_entry.get().strip().lower()
        self.rebuild_list()

    def _matches_filter(self, task: DownloadTask) -> bool:
        if self.search_query and self.search_query not in task.filename.lower():
            return False
        if self.current_filter == "الكل":
            return True
        if self.current_filter == "قيد التحميل":
            return task.status in ("downloading", "paused", "queued")
        if self.current_filter == "المكتملة":
            return task.status == "completed"
        return task.category == self.current_filter

    def mark_ui_dirty(self):
        self._ui_dirty = True

    def rebuild_list(self):
        for widget in self.scroll_frame.winfo_children():
            if widget is not self.empty_label:
                widget.destroy()
        self.cards.clear()

        visible = [t for t in reversed(list(self.engine.tasks.values())) if self._matches_filter(t)]
        if not visible:
            self.empty_label.grid(row=0, column=0, pady=80)
        else:
            self.empty_label.grid_forget()
            for idx, task in enumerate(visible):
                card = TaskCard(self.scroll_frame, task, self.engine)
                card.grid(row=idx, column=0, pady=6, padx=4, sticky="ew")
                self.cards[task.id] = card

        self._update_statusbar()

    def _ui_loop(self):
        try:
            while True:
                self._tray_queue.get_nowait()()
        except queue.Empty:
            pass
        if self._ui_dirty:
            self._ui_dirty = False
            visible_ids = [t.id for t in list(self.engine.tasks.values()) if self._matches_filter(t)]
            if set(visible_ids) != set(self.cards.keys()):
                self.rebuild_list()
            else:
                for card in list(self.cards.values()):
                    card.refresh()
                self._update_statusbar()
        self.after(300, self._ui_loop)

    def _update_statusbar(self):
        tasks = list(self.engine.tasks.values())
        total_speed = sum(t.speed for t in tasks if t.status == "downloading")
        active = sum(1 for t in tasks if t.status == "downloading")
        self.status_lbl.configure(
            text=f"إجمالي المهام: {len(tasks)}   |   نشط حالياً: {active}   |   السرعة الإجمالية: {format_size(total_speed)}/s"
        )

    def pause_all(self):
        for tid in list(self.engine.tasks.keys()):
            self.engine.pause_task(tid)

    def resume_all(self):
        for tid, task in list(self.engine.tasks.items()):
            if task.status in ("paused", "error"):
                self.engine.start_task(tid)

    def _clipboard_watcher_loop(self):
        try:
            if self.cfg.get("auto_capture", True):
                clip = self.clipboard_get().strip()
                if clip != self.last_clipboard:
                    self.last_clipboard = clip
                    if not any(t.url == clip for t in self.engine.tasks.values()):
                        if is_video_page_url(clip):
                            self.deiconify()
                            self.lift()
                            AddVideoDialog(self, self.engine, prefill_url=clip, auto_captured=True)
                        elif self._is_direct_download_url(clip):
                            self.deiconify()
                            self.lift()
                            AddDownloadDialog(self, self.engine, prefill_url=clip, auto_captured=True)
        except Exception:
            pass
        self.after(900, self._clipboard_watcher_loop)

    def _is_direct_download_url(self, text: str) -> bool:
        if not text.startswith(("http://", "https://")) or " " in text or "\n" in text:
            return False
        path = urlparse(text).path.lower()
        all_exts = set().union(*FILE_CATEGORIES.values()) | {".bin", ".tar.xz", ".img", ".srt", ".epub"}
        return any(path.endswith(ext) for ext in all_exts)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--make-icon":
        generate_app_icon(Path(sys.argv[2]))
        sys.exit(0)
    app = NovaGetApp(start_minimized="--minimized" in sys.argv)
    app.mainloop()
