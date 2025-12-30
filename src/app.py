import subprocess
import threading
import re
import socket
import queue
import json
import logging
from pathlib import Path
import sys
import os
import datetime
import signal  # for safe interrupt
import time    # for auto-resume wait
import urllib.request

from tkinter import filedialog, messagebox, StringVar
import ttkbootstrap as ttk
from ttkbootstrap.constants import *

# Optional imports: thumbnail + notification
try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    from plyer import notification as plyer_notification
except ImportError:
    plyer_notification = None

try:
    import winsound
except ImportError:
    winsound = None

# ---------------- App Version ---------------- #
APP_VERSION = "1.0.0"   # jab bhi naya build banaoge, yaha version badal dena

# ---------------- Subprocess flag (no console) ---------------- #
CREATE_NO_WINDOW = 0x08000000  # Windows-specific, others pe ignore ho jayega


# ---------------- Log Rotation ---------------- #
def rotate_logs(base_file, max_size_mb=5, backup_count=3):
    """Auto-rotate log file when size exceeds max_size_mb."""
    try:
        if not os.path.exists(base_file):
            return

        size_mb = os.path.getsize(base_file) / (1024 * 1024)

        if size_mb < max_size_mb:
            return

        # Delete oldest backup
        oldest = f"{base_file}.{backup_count}"
        if os.path.exists(oldest):
            os.remove(oldest)

        # Shift backups down
        for i in range(backup_count - 1, 0, -1):
            src = f"{base_file}.{i}"
            dst = f"{base_file}.{i + 1}"
            if os.path.exists(src):
                os.rename(src, dst)

        # Move main log -> .1
        os.rename(base_file, f"{base_file}.1")

    except Exception as e:
        print("Log rotation error:", e)


# ---------------- Utility Functions ---------------- #
def is_connected(host="8.8.8.8", port=53, timeout=3):
    """Check internet connectivity."""
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except socket.error:
        return False


def format_duration(seconds):
    """Helper: seconds -> H:MM:SS / M:SS string."""
    try:
        seconds = int(seconds)
    except Exception:
        return "-"

    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60

    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    else:
        return f"{m:02d}:{s:02d}"


def show_system_notification(title, message):
    """Desktop notification + optional beep."""
    # plyer notification
    if plyer_notification is not None:
        try:
            plyer_notification.notify(
                title=title,
                message=message,
                app_name="YT Downloader",
                timeout=5,
            )
        except Exception:
            pass

    # Beep fallback
    if winsound is not None:
        try:
            winsound.MessageBeep()
        except Exception:
            pass


# ---------------- yt-dlp Auto-update timestamp helpers ---------------- #
UPDATE_INFO_FILE = Path.home() / ".yt_dlp_update_check.json"


def should_check_update():
    """Return True only if last update check was >24h ago or never."""
    try:
        if UPDATE_INFO_FILE.exists():
            data = json.loads(UPDATE_INFO_FILE.read_text(encoding="utf-8"))
            last = datetime.datetime.fromisoformat(data.get("last_check"))
            now = datetime.datetime.now()
            if (now - last).total_seconds() < 86400:  # 24 hours
                return False
    except Exception:
        pass
    return True


def mark_update_checked():
    """Store current timestamp as last update check time."""
    try:
        data = {"last_check": datetime.datetime.now().isoformat()}
        UPDATE_INFO_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


# ---------------- Logging Setup (AppData/Roaming) ---------------- #
log_dir = Path.home() / "AppData/Roaming/yt_downloader"
log_dir.mkdir(parents=True, exist_ok=True)

LOG_FILE = log_dir / "yt_downloader.log"
rotate_logs(str(LOG_FILE), max_size_mb=5, backup_count=3)

logger = logging.getLogger("yt_downloader")
logger.setLevel(logging.INFO)

fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setLevel(logging.INFO)

# ✅ Correct formatter: format + datefmt alag-alag
formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    "%Y-%m-%d %H:%M:%S"
)
fh.setFormatter(formatter)

if not logger.handlers:
    logger.addHandler(fh)

logger.info("Logger initialized at %s", LOG_FILE)


# ---------------- Settings (resolution only) ---------------- #
CONFIG_PATH = Path.home() / ".yt_downloader_gui.json"


# ---------------- Helper: base dir, yt-dlp path, ffmpeg PATH ---------------- #
def get_base_dir() -> Path:
    """
    PyInstaller new version (v6+) me onedir build sari files `_internal`
    ke andar dalta hai. `_MEIPASS` always `_internal` folder hota hai.
    """
    # Running as PyInstaller executable?
    if getattr(sys, "frozen", False):
        # PyInstaller extracts all bundled files into _MEIPASS (_internal folder)
        if hasattr(sys, "_MEIPASS"):
            return Path(sys._MEIPASS)

        # fallback (rare case)
        return Path(sys.executable).parent

    # Running as normal python script
    return Path(__file__).parent



def get_yt_dlp_path() -> str:
    """Bundled yt-dlp.exe pehle try karo, warna system ka yt-dlp use karo."""
    base_dir = get_base_dir()
    local_yt = base_dir / "yt-dlp.exe"
    if local_yt.exists():
        return str(local_yt)
    return "yt-dlp"


def build_env_with_ffmpeg() -> dict:
    """Env PATH me bundled ffmpeg folder add karta hai (base_dir/ffmpeg)."""
    env = os.environ.copy()
    base_dir = get_base_dir()
    ffmpeg_dir = base_dir / "ffmpeg"
    if ffmpeg_dir.exists():
        env["PATH"] = str(ffmpeg_dir) + os.pathsep + env.get("PATH", "")
    return env


class YouTubeDownloaderApp:
    def __init__(self):
        self.download_process = None
        self.is_downloading = False
        self.is_paused = False
        self.download_args = None
        self.download_folder = None  # Always None on app start
        self.current_url = None
        self.message_queue = queue.Queue()
        self.worker_thread = None
        self.checked_yt_dlp = False
        self.auto_update_yt_dlp = True  # used by background auto-update flag

        # ✅ Auto-resume flags
        self.auto_resume_enabled = True
        self.auto_resume_pending = False
        self.auto_resume_thread = None
        self.auto_resume_deadline = None  # time.time() + 60 when disconnect

        # Thumbnail / info cache
        self.thumb_image_tk = None
        self.last_video_info = None

        self.saved_resolution = "720p"
        self.load_settings()

        # ---------------- GUI ---------------- #
        self.style = ttk.Style(theme="darkly")
        self.window = self.style.master
        self.window.title(f"📥 YT Video Downloader v{APP_VERSION}")
        self.window.geometry("650x650")

        ttk.Label(self.window, text="🎬 Paste YouTube URL:", font=("Helvetica", 14)).pack(pady=10)
        self.url_entry = ttk.Entry(self.window, width=70, font=("Helvetica", 12))
        self.url_entry.pack(ipady=4)

        frame_dropdown = ttk.Frame(self.window)
        frame_dropdown.pack(pady=10, fill="x")

        ttk.Label(frame_dropdown, text="📺 Select Resolution:", font=("Helvetica", 12)).grid(row=0, column=0, sticky="w")

        resolution_options = ["420p", "720p", "1080p"]
        self.resolution_var = StringVar(value=self.saved_resolution)
        self.resolution_menu = ttk.OptionMenu(
            frame_dropdown, self.resolution_var, self.resolution_var.get(), *resolution_options
        )
        self.resolution_menu.grid(row=0, column=1, sticky="w", padx=10)

        # Info button
        self.btn_info = ttk.Button(
            frame_dropdown,
            text="ℹ Fetch Info",
            bootstyle="secondary",
            width=14,
            command=self.fetch_and_show_info_button,
        )
        self.btn_info.grid(row=0, column=2, padx=10, sticky="e")

        # Thumbnail + info frame
        self.info_frame = ttk.Frame(self.window)
        self.info_frame.pack(pady=10, fill="x")

        self.thumb_label = ttk.Label(self.info_frame)
        self.thumb_label.grid(row=0, column=0, rowspan=3, padx=10, pady=5)

        self.info_title_label = ttk.Label(self.info_frame, text="Title: -", font=("Helvetica", 11), anchor="w")
        self.info_title_label.grid(row=0, column=1, sticky="w", pady=2)

        self.info_channel_label = ttk.Label(self.info_frame, text="Channel: -", font=("Helvetica", 11), anchor="w")
        self.info_channel_label.grid(row=1, column=1, sticky="w", pady=2)

        self.info_duration_label = ttk.Label(self.info_frame, text="Duration: -", font=("Helvetica", 11), anchor="w")
        self.info_duration_label.grid(row=2, column=1, sticky="w", pady=2)

        self.progress_bar = ttk.Progressbar(self.window, length=500, mode='determinate', bootstyle="success")
        self.progress_bar.pack(pady=10)

        self.progress_info_label = ttk.Label(self.window, text="", font=("Helvetica", 11), anchor="center")
        self.progress_info_label.pack()

        btn_frame = ttk.Frame(self.window)
        btn_frame.pack(pady=10)

        self.btn_video = ttk.Button(
            btn_frame, text="▶️ Download Video", bootstyle="success", width=20,
            command=self.start_video_download
        )
        self.btn_video.grid(row=0, column=0, padx=10)

        self.btn_playlist = ttk.Button(
            btn_frame, text="📃 Download Playlist", bootstyle="primary", width=20,
            command=self.start_playlist_download
        )
        self.btn_playlist.grid(row=0, column=1, padx=10)

        self.btn_audio = ttk.Button(
            btn_frame, text="🎧 Audio Only", bootstyle="warning", width=44,
            command=self.start_audio_download
        )
        self.btn_audio.grid(row=1, column=0, columnspan=2, pady=10)

        self.btn_pause = ttk.Button(
            self.window, text="⏸ Pause Download", bootstyle="danger",
            width=44, command=self.pause_download
        )
        self.btn_pause.pack(pady=5)

        self.btn_resume = ttk.Button(
            self.window, text="⏯ Resume Download", bootstyle="info",
            width=44, command=self.resume_download
        )
        self.btn_resume.pack(pady=5)

        # 📂 Open Folder button
        self.btn_open_folder = ttk.Button(
            self.window, text="📂 Open Folder", bootstyle="secondary",
            width=44, command=self.open_download_folder
        )
        self.btn_open_folder.pack(pady=5)

        self.status_label = ttk.Label(self.window, text="", font=("Helvetica", 12))
        self.status_label.pack(pady=5)

        self._set_button_states(False, False)

        self.window.after(100, self.process_queue)
        self.window.protocol("WM_DELETE_WINDOW", self.on_close)

        logger.info("YouTubeDownloaderApp started.")

        # 🔥 Background yt-dlp auto-update (once per 24h)
        if self.auto_update_yt_dlp:
            threading.Thread(target=self.background_auto_update, daemon=True).start()

        # 🔥 Background app version update check (GitHub latest.json)
        threading.Thread(target=self.check_for_app_update, daemon=True).start()

    # ---------------- Background yt-dlp auto-update ---------------- #
    def background_auto_update(self):
        """Silent yt-dlp auto-update in background with 24h cooldown."""
        try:
            if not should_check_update():
                logger.info("YT-DLP auto-update skipped (checked recently).")
                return

            logger.info("Checking YT-DLP update in background...")

            yt_dlp_path = get_yt_dlp_path()
            env = build_env_with_ffmpeg()

            proc = subprocess.run(
                [yt_dlp_path, "-U"],
                capture_output=True,
                text=True,
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )

            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            logger.info("YT-DLP -U output: %s", out or err or "no output")

            mark_update_checked()

        except Exception as e:
            logger.error("YT-DLP auto-update failed silently: %s", e)

    # ---------------- App Self-Update (GitHub latest.json) ---------------- #
    def check_for_app_update(self):
        """Check GitHub latest.json for a newer app version."""
        try:
            latest_url = "https://raw.githubusercontent.com/Devasp18/yt-downloader-updates/main/latest.json"
            logger.info("Checking app update from %s", latest_url)

            with urllib.request.urlopen(latest_url, timeout=10) as resp:
                data = resp.read().decode("utf-8")

            info = json.loads(data)
            latest_version = info.get("version")
            exe_url = info.get("exe_url")

            logger.info("Current app version: %s, Latest version online: %s", APP_VERSION, latest_version)

            if latest_version and exe_url and latest_version != APP_VERSION:
                self.message_queue.put({
                    "type": "app_update_available",
                    "version": latest_version,
                    "url": exe_url
                })

        except Exception as e:
            logger.error("App update check failed: %s", e)

    def apply_app_update(self, url):
        """Download new EXE and replace current one using a small .bat helper."""
        try:
            self.status_label.config(text="⬇ Downloading update...", foreground="blue")
            self.window.update_idletasks()

            new_file = "update_new.exe"
            urllib.request.urlretrieve(url, new_file)

            self.status_label.config(text="⚙ Applying update...", foreground="orange")
            self.window.update_idletasks()

            current_exe = os.path.abspath(sys.argv[0])
            bat_file = "apply_update.bat"

            bat_content = f"""@echo off
timeout 2 >nul
del "{current_exe}"
rename "{new_file}" "{os.path.basename(current_exe)}"
start "" "{current_exe}"
del "%~f0"
"""

            with open(bat_file, "w", encoding="utf-8") as f:
                f.write(bat_content)

            os.startfile(bat_file)
            logger.info("Updater batch started, closing app for update.")
            self.window.destroy()

        except Exception as e:
            messagebox.showerror("Update Failed", str(e))
            logger.error("App update apply failed: %s", e)

    # ---------------- Settings Load/Save ---------------- #
    def load_settings(self):
        try:
            if CONFIG_PATH.exists():
                with CONFIG_PATH.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                self.saved_resolution = data.get("resolution", "720p")
                logger.info("Settings loaded.")
        except Exception as e:
            logger.error("Failed to load settings: %s", e)

    def save_settings(self):
        try:
            data = {
                "resolution": self.resolution_var.get(),
                "folder": None
            }
            with CONFIG_PATH.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.info("Settings saved.")
        except Exception as e:
            logger.error("Failed to save settings: %s", e)

    # ---------------- yt-dlp Check ---------------- #
    def ensure_yt_dlp(self):
        if self.checked_yt_dlp:
            return True

        yt_dlp_path = get_yt_dlp_path()
        env = build_env_with_ffmpeg()

        try:
            result = subprocess.run(
                [yt_dlp_path, "--version"],
                capture_output=True,
                text=True,
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
            logger.info("yt-dlp version: %s", result.stdout.strip() or result.stderr.strip())
            self.checked_yt_dlp = True
            return True

        except FileNotFoundError:
            logger.error("yt-dlp not found.")
            self.message_queue.put({"type": "error", "text": "yt-dlp not found on system."})
            return False
        except Exception as e:
            logger.error("Error checking yt-dlp: %s", e)
            return False

    # ---------------- Video Info + Thumbnail ---------------- #
    def fetch_and_show_info_button(self):
        """Button handler: sirf info fetch kare, download nahi."""
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showwarning("Warning", "Please enter a YouTube URL.")
            return
        if "youtube" not in url and "youtu.be" not in url:
            messagebox.showwarning("Warning", "Invalid YouTube URL.")
            return

        threading.Thread(target=self.fetch_and_show_info, args=(url,), daemon=True).start()

    def fetch_and_show_info(self, url):
        """yt-dlp -J se metadata + thumbnail fetch kare."""
        try:
            if not is_connected():
                logger.warning("fetch_and_show_info: no internet")
                return

            yt_dlp_path = get_yt_dlp_path()
            env = build_env_with_ffmpeg()

            result = subprocess.run(
                [yt_dlp_path, "-J", url],
                capture_output=True,
                text=True,
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )

            if result.returncode != 0:
                logger.error("Failed to fetch info: %s", result.stderr)
                return

            info = json.loads(result.stdout)

            # Playlist ho to pehla video pick karo, warna direct info use karo
            if isinstance(info, dict) and info.get("_type") == "playlist" and info.get("entries"):
                info_video = info["entries"][0] or {}
            else:
                info_video = info or {}

            self.last_video_info = info_video

            title = info_video.get("title", "-")
            uploader = info_video.get("uploader", "-")
            duration = format_duration(info_video.get("duration"))
            thumb_url = info_video.get("thumbnail")

            def update_gui():
                self.info_title_label.config(text=f"Title: {title}")
                self.info_channel_label.config(text=f"Channel: {uploader}")
                self.info_duration_label.config(text=f"Duration: {duration}")

            self.window.after(0, update_gui)

            # Thumbnail (optional, only if Pillow available)
            if PIL_AVAILABLE and thumb_url:
                try:
                    with urllib.request.urlopen(thumb_url, timeout=10) as resp:
                        data = resp.read()
                    from io import BytesIO
                    img = Image.open(BytesIO(data))
                    img.thumbnail((160, 90))
                    self.thumb_image_tk = ImageTk.PhotoImage(img)

                    def update_thumb():
                        self.thumb_label.config(image=self.thumb_image_tk)
                    self.window.after(0, update_thumb)

                except Exception as e:
                    logger.error("Failed to load thumbnail: %s", e)

        except Exception as e:
            logger.error("Error in fetch_and_show_info: %s", e)

    # ---------------- Button State ---------------- #
    def _set_button_states(self, downloading, paused):
        if downloading:
            self.btn_video.config(state=DISABLED)
            self.btn_playlist.config(state=DISABLED)
            self.btn_audio.config(state=DISABLED)
            self.btn_pause.config(state=NORMAL)
            self.btn_resume.config(state=DISABLED if not paused else NORMAL)
        else:
            self.btn_video.config(state=NORMAL)
            self.btn_playlist.config(state=NORMAL)
            self.btn_audio.config(state=NORMAL)
            self.btn_pause.config(state=DISABLED)
            self.btn_resume.config(state=NORMAL if paused else DISABLED)

    # ---------------- Core Download Logic ---------------- #
    def run_yt_dlp(self, is_playlist, audio_only, resume=False):

        # naya run start hote hi auto-resume flag reset
        self.auto_resume_pending = False
        self.auto_resume_deadline = None
        self.is_downloading = True
        self.is_paused = False

        if resume and self.download_args:
            is_playlist, audio_only = self.download_args
        else:
            self.download_args = (is_playlist, audio_only)

        url = self.current_url

        logger.info(
            "Download started: %s (playlist=%s audio=%s resume=%s)",
            url, is_playlist, audio_only, resume
        )

        if not is_connected():
            self.message_queue.put({"type": "error", "text": "No internet connection."})
            self.is_downloading = False
            return

        if not self.ensure_yt_dlp():
            self.is_downloading = False
            return

        folder_base = str(self.download_folder)

        res = self.resolution_var.get()
        if audio_only:
            format_code = "bestaudio"
        else:
            height = {
                "420p": "height<=480",
                "720p": "height<=720",
                "1080p": "height<=1080"
            }.get(res, "height<=720")
            format_code = f"bestvideo[{height}]+bestaudio/best"

        label_type = "Playlist" if is_playlist else ("Audio" if audio_only else "Video")

        self.message_queue.put({"type": "status", "text": f"🔄 {label_type} downloading...", "color": "blue"})
        self.message_queue.put({"type": "progress_reset"})

        if is_playlist:
            filename_pattern = f"{folder_base}/%(playlist_title)s/%(playlist_index)s - %(title).128s.%(ext)s"
        else:
            filename_pattern = f"{folder_base}/%(title).128s.%(ext)s"

        if audio_only:
            filename_pattern = filename_pattern.replace(".%(ext)s", ".mp3")

        yt_dlp_path = get_yt_dlp_path()
        env = build_env_with_ffmpeg()

        # ✅ concurrent fragments + ffmpeg reconnect args
        cmd = [
            yt_dlp_path,
            "--merge-output-format", "mp4",
            "--progress",
            "--newline",
            "-f", format_code,
            "-o", filename_pattern,
            "--concurrent-fragments", "4",
            "--downloader-args",
            "ffmpeg_i:-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            url,
            "-c",
            "--retries", "20",
            "--fragment-retries", "20",
            "--socket-timeout", "30"
        ]

        if audio_only:
            cmd.insert(1, "--extract-audio")
            cmd.insert(2, "--audio-format")
            cmd.insert(3, "mp3")

        max_attempts = 3
        attempt = 0

        try:
            while attempt < max_attempts:
                attempt += 1
                try:
                    self.download_process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        creationflags=CREATE_NO_WINDOW,
                        env=env
                    )

                    for raw_line in iter(self.download_process.stdout.readline, ""):
                        line = raw_line.strip()

                        # ---------------- PAUSE (USER) ---------------- #
                        if self.is_paused:
                            try:
                                self.download_process.send_signal(signal.SIGINT)
                            except Exception:
                                self.download_process.terminate()
                            logger.info("Paused by user")
                            self.message_queue.put({"type": "status", "text": "⏸ Download paused.", "color": "orange"})
                            self.message_queue.put({"type": "paused"})
                            break

                        # ---------------- INTERNET LOSS → AUTO PAUSE + AUTO-RESUME (60s) ---------------- #
                        if not is_connected():
                            self.is_paused = True
                            # 60 sec window ke liye deadline set
                            self.auto_resume_pending = True
                            self.auto_resume_deadline = time.time() + 60.0
                            try:
                                self.download_process.send_signal(signal.SIGINT)
                            except Exception:
                                self.download_process.terminate()
                            logger.warning("Internet error - paused (auto-resume pending, 60s window)")
                            self.message_queue.put({
                                "type": "status",
                                "text": "⏸ Internet disconnected. Auto-resume for 60s...",
                                "color": "orange",
                            })
                            self.message_queue.put({"type": "paused"})
                            # GUI thread ko auto-resume start karne bolo
                            if self.auto_resume_enabled:
                                self.message_queue.put({"type": "auto_resume_scheduled"})
                            break

                        # ---------------- PROGRESS PARSING ---------------- #
                        match = re.search(
                            r'\[download\]\s+([\d.]+)% of\s+([\d.]+)(\w+iB)\s+at\s+([\d.]+\w+/s).*?ETA\s+([\d:]+)',
                            line
                        )
                        if match:
                            percent, total, unit, speed, eta = match.groups()
                            try:
                                downloaded = float(percent) * float(total) / 100
                            except Exception:
                                downloaded = 0

                            self.message_queue.put({
                                "type": "progress",
                                "percent": float(percent),
                                "info": f"📦 {downloaded:.2f}{unit} of {total}{unit}   🔻 {speed}   ⏳ {eta}",
                            })

                    if self.download_process:
                        self.download_process.communicate()

                    rc = self.download_process.returncode if self.download_process else -1

                    # ----- Unified result handling with smart retry ----- #
                    if not self.is_paused and rc in (0, 1):
                        # 0 = success, 1 = warnings (still treat as success)
                        logger.info("Download complete (rc=%s).", rc)
                        self.auto_resume_pending = False
                        self.auto_resume_deadline = None
                        self.message_queue.put({
                            "type": "status",
                            "text": f"✅ {label_type} download complete!",
                            "color": "green",
                        })
                        self.message_queue.put({"type": "complete"})
                        self.save_settings()
                        # 🔔 Notification
                        show_system_notification("Download Complete", f"{label_type} download finished.")
                        break

                    elif self.is_paused and self.auto_resume_pending:
                        # Auto-resume ke case me yaha error popup nahi dena
                        logger.info("Download ended in paused state (auto-resume active).")
                        break

                    elif self.is_paused:
                        # Manual pause, auto-resume nahi
                        logger.info("Download ended in paused state (manual pause).")
                        break

                    else:
                        # Real failure (rc > 1)
                        logger.error("yt-dlp real error (exit code %s) on attempt %d", rc, attempt)
                        if attempt < max_attempts:
                            logger.info("Retrying download (attempt %d of %d)...", attempt + 1, max_attempts)
                            self.message_queue.put({
                                "type": "status",
                                "text": f"⚠ Download error, retrying ({attempt}/{max_attempts})...",
                                "color": "orange",
                            })
                            time.sleep(3)
                            continue
                        else:
                            self.auto_resume_pending = False
                            self.auto_resume_deadline = None
                            self.message_queue.put({"type": "error", "text": "Download failed."})
                            break

                except Exception as e:
                    logger.exception("Download error on attempt %d: %s", attempt, e)
                    if attempt < max_attempts:
                        self.message_queue.put({
                            "type": "status",
                            "text": f"⚠ Error occurred, retrying ({attempt}/{max_attempts})...",
                            "color": "orange",
                        })
                        time.sleep(3)
                        continue
                    else:
                        self.auto_resume_pending = False
                        self.auto_resume_deadline = None
                        self.message_queue.put({"type": "error", "text": str(e)})
                        break

                finally:
                    self.download_process = None

        finally:
            self.is_downloading = False

    # ---------------- Auto-Resume Worker (60s window) ---------------- #
    def start_auto_resume(self):
        """Background thread jo internet aane ka wait karega, phir auto-resume karega (max 60s)."""
        if not self.auto_resume_enabled:
            return

        if not self.auto_resume_pending:
            return

        if self.auto_resume_thread and self.auto_resume_thread.is_alive():
            return

        def _worker():
            logger.info("Auto-resume worker started.")
            while self.auto_resume_pending:
                # 60 second ka window check karo
                if self.auto_resume_deadline is not None and time.time() > self.auto_resume_deadline:
                    logger.info("Auto-resume window expired, switching to manual resume only.")
                    self.auto_resume_pending = False
                    self.auto_resume_deadline = None
                    # GUI ko status update
                    self.message_queue.put({
                        "type": "status",
                        "text": "⏸ Internet disconnected. Auto-resume time over, press Resume when back online.",
                        "color": "orange",
                    })
                    break

                if not is_connected():
                    time.sleep(5)
                    continue

                # internet aa gaya AND still within window → GUI thread ko resume ke liye signal
                self.message_queue.put({"type": "auto_resume_now"})
                logger.info("Internet restored within auto-resume window, requesting auto-resume.")
                break

        self.auto_resume_thread = threading.Thread(target=_worker, daemon=True)
        self.auto_resume_thread.start()

    def _resume_download_internal(self):
        """Common resume logic (button + auto-resume dono yahi use karte)."""
        if self.is_downloading and not self.is_paused:
            # already downloading
            return

        if not self.download_args or not self.current_url:
            return

        if not is_connected():
            self.status_label.config(text="⚠ Internet still disconnected.", foreground="orange")
            return

        self.is_paused = False
        self.auto_resume_pending = False
        self.auto_resume_deadline = None
        self._set_button_states(True, False)

        self.worker_thread = threading.Thread(
            target=self.run_yt_dlp,
            args=(None, None, True),
            daemon=True,
        )
        self.worker_thread.start()

        self.status_label.config(text="🔄 Resuming download...", foreground="blue")

    # ---------------- Queue Processing ---------------- #
    def process_queue(self):
        try:
            while True:
                msg = self.message_queue.get_nowait()
                m = msg.get("type")

                if m == "status":
                    self.status_label.config(text=msg["text"], foreground=msg.get("color", "white"))

                elif m == "progress_reset":
                    self.progress_bar["value"] = 0
                    self.progress_info_label.config(text="")

                elif m == "progress":
                    self.progress_bar["value"] = msg["percent"]
                    self.progress_info_label.config(text=msg["info"], foreground="cyan")

                elif m == "complete":
                    self.progress_bar["value"] = 100
                    self._set_button_states(False, False)

                elif m == "paused":
                    self._set_button_states(False, True)

                elif m == "error":
                    self._set_button_states(False, False)
                    self.progress_bar["value"] = 0
                    self.progress_info_label.config(text="")
                    self.status_label.config(text="❌ Download failed!", foreground="red")
                    messagebox.showerror("Error", msg["text"])

                elif m == "app_update_available":
                    version = msg["version"]
                    url = msg["url"]
                    if messagebox.askyesno(
                        "Update Available",
                        f"A new version {version} is available.\n\nDo you want to download and install it now?",
                    ):
                        self.apply_app_update(url)

                elif m == "auto_resume_scheduled":
                    # background worker ko start karo
                    self.start_auto_resume()

                elif m == "auto_resume_now":
                    # internet aa gaya → internal resume
                    self._resume_download_internal()

        except queue.Empty:
            pass

        self.window.after(100, self.process_queue)

    # ---------------- Download Triggers ---------------- #
    def _prepare_new_download(self):
        if self.is_downloading:
            messagebox.showinfo("Info", "A download is already running.")
            return False

        url = self.url_entry.get().strip()
        if not url:
            messagebox.showwarning("Warning", "Please enter a YouTube URL.")
            return False

        if "youtube" not in url and "youtu.be" not in url:
            messagebox.showwarning("Warning", "Invalid YouTube URL.")
            return False

        self.current_url = url

        # Info preview (best-effort, background)
        threading.Thread(target=self.fetch_and_show_info, args=(url,), daemon=True).start()

        # FIRST TIME PER SESSION → Ask folder only once
        if self.download_folder is None:
            folder = filedialog.askdirectory(title="Select Download Folder")
            if not folder:
                return False
            self.download_folder = Path(folder)

        return True

    def _start_download(self, is_playlist, audio_only):
        if not self._prepare_new_download():
            return

        self.is_paused = False
        self.auto_resume_pending = False
        self.auto_resume_deadline = None
        self._set_button_states(True, False)

        self.worker_thread = threading.Thread(
            target=self.run_yt_dlp,
            args=(is_playlist, audio_only, False),
            daemon=True,
        )
        self.worker_thread.start()

    def start_video_download(self):
        self._start_download(False, False)

    def start_playlist_download(self):
        self._start_download(True, False)

    def start_audio_download(self):
        self._start_download(False, True)

    # ---------------- Pause & Resume (buttons) ---------------- #
    def pause_download(self):
        if self.is_downloading and self.download_process:
            # logical state update
            self.is_paused = True
            self.is_downloading = False
            self.auto_resume_pending = False  # manual pause → no auto-resume
            self.auto_resume_deadline = None
            logger.info("Pause requested")
        else:
            messagebox.showinfo("Info", "No active download.")

    def resume_download(self):
        # button se manual resume → auto_resume_pending false
        self.auto_resume_pending = False
        self.auto_resume_deadline = None
        if self.is_downloading and not self.is_paused:
            messagebox.showinfo("Info", "Already downloading.")
            return

        if not self.download_args or not self.current_url:
            messagebox.showinfo("Info", "Nothing to resume.")
            return

        if not is_connected():
            messagebox.showwarning("Warning", "Internet is disconnected.")
            return

        self._resume_download_internal()

    # ---------------- Open Folder ---------------- #
    def open_download_folder(self):
        """📂 User ke download folder ko open karo."""
        if self.download_folder is None:
            messagebox.showinfo("Info", "No download folder selected yet.")
            return
        try:
            os.startfile(str(self.download_folder))
        except Exception as e:
            messagebox.showerror("Error", f"Cannot open folder:\n{e}")

    # ---------------- Window Close ---------------- #
    def on_close(self):
        # koi bhi auto-resume worker chal raha ho to band karo
        self.auto_resume_pending = False
        self.auto_resume_deadline = None

        if self.is_downloading and self.download_process:
            if messagebox.askyesno("Exit", "A download is running. Exit anyway?"):
                try:
                    self.download_process.terminate()
                except Exception:
                    pass
            else:
                return

        self.save_settings()
        self.window.destroy()


# ---------------- MAIN LOOP ---------------- #
if __name__ == "__main__":
    app = YouTubeDownloaderApp()
    app.window.mainloop()
