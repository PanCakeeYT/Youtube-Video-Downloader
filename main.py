# advanced_yt_downloader.py
# Requirements: pip install PySide6 yt-dlp requests
# Make sure ffmpeg is available in PATH for merges / conversions.

import sys
import os
import json
import threading
import time
import math
from queue import Queue
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

import requests
import yt_dlp

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QCheckBox, QProgressBar, QTextEdit, QFileDialog, QTableWidget,
    QTableWidgetItem, QAbstractItemView, QHeaderView, QMessageBox, QSpinBox,
    QDialog, QFormLayout, QDialogButtonBox, QFrame
)
from PySide6.QtGui import QPalette, QColor, QPixmap, QDragEnterEvent, QDropEvent
from PySide6.QtCore import Qt, Signal, QObject

SETTINGS_FILE = "ydl_gui_settings.json"

# --------- Data structures ----------
@dataclass
class DownloadJob:
    url: str
    title: Optional[str] = None
    thumbnail: Optional[str] = None
    formats: List[Dict[str, Any]] = field(default_factory=list)
    selected_format_id: Optional[str] = None
    include_audio: bool = True
    audio_only: bool = False
    subtitles: bool = False
    outtmpl: str = "%(title)s.%(ext)s"
    status: str = "Queued"
    progress: int = 0
    speed: Optional[str] = None
    eta: Optional[str] = None
    size: Optional[str] = None
    index_in_table: int = -1
    resumed: bool = False  # used to mark resumed downloads


# ---------- Signals object ----------
class WorkerSignals(QObject):
    progress = Signal(int, int, str, str)  # index, percent, speed, eta
    status = Signal(int, str)  # index, status_text
    finished = Signal(int, bool)  # index, success
    log = Signal(str)


# ---------- Worker (threaded) ----------
class DownloadWorker(threading.Thread):
    def __init__(self, job: DownloadJob, index: int, settings: dict, signals: WorkerSignals):
        super().__init__(daemon=True)
        self.job = job
        self.index = index
        self.settings = settings
        self.signals = signals
        self._stop_requested = False

    def stop(self):
        # best-effort stop: set flag; yt-dlp runs in same thread, so raising won't stop it immediately
        self._stop_requested = True
        self.signals.log.emit(f"Requested stop for job {self.index} ({self.job.title})")

    def run(self):
        self.signals.status.emit(self.index, "Starting")
        ydl_opts = {
            'outtmpl': os.path.join(self.settings.get('download_dir', '.'), self.job.outtmpl),
            'progress_hooks': [self._hook],
            'quiet': True,
            'no_warnings': True,
            'noprogress': True,  # we use our hook
            'continuedl': True,  # allow resuming
            'retries': 3,
            'cookiefile': self.settings.get('cookiesfile') or None,
        }

        # format logic
        fmt = self.job.selected_format_id
        if self.job.audio_only:
            # download best audio and convert if requested format
            audio_pref = self.settings.get('audio_format', 'mp3')
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': audio_pref,
                    'preferredquality': '192',
                }] if audio_pref else [],
            })
        else:
            if fmt:
                if self.job.include_audio:
                    ydl_opts['format'] = f"{fmt}+bestaudio/best"
                else:
                    # video only
                    ydl_opts['format'] = fmt
            else:
                # fallback to best
                ydl_opts['format'] = 'bestvideo+bestaudio/best' if self.job.include_audio else 'bestvideo'

        # subtitles
        if self.job.subtitles:
            ydl_opts.update({
                'writesubtitles': True,
                'writeautomaticsub': True,
                'subtitleslangs': self.settings.get('sub_langs', ['en']),
                'subtitlesformat': 'srt',
            })

        # proxy / user-agent
        if self.settings.get('proxy'):
            ydl_opts['proxy'] = self.settings['proxy']
        if self.settings.get('user_agent'):
            ydl_opts['user_agent'] = self.settings['user_agent']

        # postprocessors for format conversion if requested by settings
        # e.g., convert mp4->mp4 not needed. For audio we already used FFmpegExtractAudio.

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # support playlists: yt-dlp will download all entries if given playlist URL.
                # But our app adds playlist entries individually when adding to queue, so we still allow single URL.
                # Run extraction+download
                self.signals.log.emit(f"Worker {self.index}: downloading {self.job.url}")
                ydl.download([self.job.url])
            if not self._stop_requested:
                self.signals.progress.emit(self.index, 100, "0 B/s", "0s")
                self.signals.status.emit(self.index, "Complete")
                self.signals.finished.emit(self.index, True)
            else:
                self.signals.status.emit(self.index, "Stopped")
                self.signals.finished.emit(self.index, False)
        except Exception as e:
            self.signals.log.emit(f"Worker {self.index} error: {repr(e)}")
            self.signals.status.emit(self.index, f"Error: {str(e)}")
            self.signals.finished.emit(self.index, False)

    def _hook(self, d):
        if self._stop_requested:
            # attempt to abort by raising an exception yt-dlp will propagate
            raise yt_dlp.utils.DownloadError("Stopped by user")
        status = d.get('status')
        if status == 'downloading':
            # percent
            p = d.get('_percent_str', '0.0%').strip().replace('%', '')
            try:
                perc = int(float(p))
            except:
                perc = 0
            speed = d.get('_speed_str', '0 B/s')
            eta = d.get('_eta_str', '??s')
            self.signals.progress.emit(self.index, perc, speed, eta)
            self.signals.status.emit(self.index, "Downloading")
        elif status == 'finished':
            self.signals.status.emit(self.index, "Merging / finalizing")
        elif status == 'error':
            self.signals.status.emit(self.index, "Error")


# -------------- Settings dialog ----------------
class SettingsDialog(QDialog):
    def __init__(self, parent, settings):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.settings = settings
        layout = QFormLayout()
        self.download_dir_btn = QPushButton(self.settings.get('download_dir', os.getcwd()))
        self.download_dir_btn.clicked.connect(self.choose_dir)
        layout.addRow("Download Directory", self.download_dir_btn)

        self.concurrent_spin = QSpinBox()
        self.concurrent_spin.setRange(1, 8)
        self.concurrent_spin.setValue(self.settings.get('concurrent', 2))
        layout.addRow("Concurrent downloads", self.concurrent_spin)

        self.audio_combo = QComboBox()
        self.audio_combo.addItems(['mp3', 'aac', 'wav', 'm4a', 'none'])
        cur_audio = self.settings.get('audio_format', 'mp3')
        idx = self.audio_combo.findText(cur_audio) if cur_audio in ['mp3', 'aac', 'wav', 'm4a', 'none'] else 0
        self.audio_combo.setCurrentIndex(idx if idx>=0 else 0)
        layout.addRow("Default audio format", self.audio_combo)

        self.proxy_input = QLineEdit(self.settings.get('proxy',''))
        layout.addRow("Proxy (http://...)", self.proxy_input)

        self.user_agent_input = QLineEdit(self.settings.get('user_agent',''))
        layout.addRow("User-Agent", self.user_agent_input)

        self.cookies_input = QLineEdit(self.settings.get('cookiesfile',''))
        b = QPushButton("Browse")
        b.clicked.connect(self.browse_cookies)
        h = QHBoxLayout()
        h.addWidget(self.cookies_input); h.addWidget(b)
        layout.addRow("Cookies file", h)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        self.setLayout(layout)

    def choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select download directory", self.download_dir_btn.text())
        if d:
            self.download_dir_btn.setText(d)

    def browse_cookies(self):
        f, _ = QFileDialog.getOpenFileName(self, "Select cookies file", "", "Cookies Files (*.txt *.cookies);;All Files (*)")
        if f:
            self.cookies_input.setText(f)

    def accept(self):
        self.settings['download_dir'] = self.download_dir_btn.text()
        self.settings['concurrent'] = int(self.concurrent_spin.value())
        sel_audio = self.audio_combo.currentText()
        self.settings['audio_format'] = sel_audio if sel_audio!='none' else ''
        self.settings['proxy'] = self.proxy_input.text().strip() or ''
        self.settings['user_agent'] = self.user_agent_input.text().strip() or ''
        self.settings['cookiesfile'] = self.cookies_input.text().strip() or ''
        super().accept()


# -------------- Main Window ----------------
class AdvancedDownloader(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Advanced YouTube Downloader (PySide6) — Far Too Advanced")
        self.setFixedSize(980, 640)
        self.load_settings()
        self.jobs: List[DownloadJob] = []
        self.queue = Queue()
        self.active_workers: Dict[int, DownloadWorker] = {}
        self.signals = WorkerSignals()
        self.signals.progress.connect(self.on_progress)
        self.signals.status.connect(self.on_status)
        self.signals.finished.connect(self.on_finished)
        self.signals.log.connect(self.append_log)
        self.setup_ui()
        self.check_workers_timer = self.start_check_timer()

    def load_settings(self):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    self.settings = json.load(f)
            except:
                self.settings = {}
        else:
            self.settings = {}
        # defaults
        if 'download_dir' not in self.settings:
            self.settings['download_dir'] = os.getcwd()
        if 'concurrent' not in self.settings:
            self.settings['concurrent'] = 2
        if 'audio_format' not in self.settings:
            self.settings['audio_format'] = 'mp3'

    def save_settings(self):
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.settings, f, indent=2)

    def setup_ui(self):
        # Dark palette
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor("#0F0F10"))
        palette.setColor(QPalette.WindowText, QColor("#EAEAEA"))
        palette.setColor(QPalette.Base, QColor("#121212"))
        palette.setColor(QPalette.AlternateBase, QColor("#1A1A1A"))
        palette.setColor(QPalette.Text, QColor("#DDDDDD"))
        palette.setColor(QPalette.Button, QColor("#1F1F1F"))
        palette.setColor(QPalette.ButtonText, QColor("#FFFFFF"))
        self.setPalette(palette)

        main = QVBoxLayout()
        topbar = QHBoxLayout()

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Paste YouTube / Playlist URL, or drag & drop here...")
        self.url_input.returnPressed.connect(self.handle_add)
        add_btn = QPushButton("Add to Queue")
        add_btn.clicked.connect(self.handle_add)
        fetch_btn = QPushButton("Fetch & Preview")
        fetch_btn.clicked.connect(self.handle_preview)

        settings_btn = QPushButton("Settings")
        settings_btn.clicked.connect(self.open_settings)
        start_btn = QPushButton("Start Queue")
        start_btn.clicked.connect(self.start_queue)
        stop_all_btn = QPushButton("Stop All")
        stop_all_btn.clicked.connect(self.stop_all)

        topbar.addWidget(self.url_input)
        topbar.addWidget(add_btn)
        topbar.addWidget(fetch_btn)
        topbar.addWidget(settings_btn)
        topbar.addWidget(start_btn)
        topbar.addWidget(stop_all_btn)

        main.addLayout(topbar)

        # Table for queue
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Title", "Res/Format", "Audio", "Progress", "Speed/ETA", "Status", "Actions"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1,7):
            self.table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.cellClicked.connect(self.on_table_click)

        main.addWidget(self.table, stretch=6)

        # bottom: thumbnail + log + controls
        bottom = QHBoxLayout()

        preview_frame = QFrame()
        preview_layout = QVBoxLayout()
        preview_frame.setLayout(preview_layout)
        preview_label = QLabel("Thumbnail Preview")
        preview_label.setAlignment(Qt.AlignCenter)
        self.thumbnail_label = QLabel()
        self.thumbnail_label.setFixedSize(320, 180)
        self.thumbnail_label.setStyleSheet("background: #141414; border: 1px solid #232323;")
        self.thumbnail_label.setAlignment(Qt.AlignCenter)
        preview_layout.addWidget(preview_label)
        preview_layout.addWidget(self.thumbnail_label)
        bottom.addWidget(preview_frame)

        log_frame = QFrame()
        log_layout = QVBoxLayout()
        log_frame.setLayout(log_layout)
        log_label = QLabel("Log")
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setFixedHeight(180)
        log_layout.addWidget(log_label)
        log_layout.addWidget(self.log)
        bottom.addWidget(log_frame, stretch=1)

        right_controls = QVBoxLayout()
        # Quick options
        opts_label = QLabel("Quick Options")
        self.audio_only_cb = QCheckBox("Default: Audio-only")
        self.include_audio_cb = QCheckBox("Default: Include audio in videos")
        self.include_audio_cb.setChecked(True)
        choose_dir_btn = QPushButton(f"Save Dir: {self.settings.get('download_dir')}")
        choose_dir_btn.clicked.connect(self.choose_dir)
        clear_done_btn = QPushButton("Clear Completed")
        clear_done_btn.clicked.connect(self.clear_completed)
        self.concurrent_spin = QSpinBox()
        self.concurrent_spin.setRange(1, 8)
        self.concurrent_spin.setValue(self.settings.get('concurrent', 2))
        right_controls.addWidget(opts_label)
        right_controls.addWidget(self.audio_only_cb)
        right_controls.addWidget(self.include_audio_cb)
        right_controls.addWidget(choose_dir_btn)
        right_controls.addWidget(QLabel("Concurrent downloads"))
        right_controls.addWidget(self.concurrent_spin)
        right_controls.addWidget(clear_done_btn)
        right_controls.addStretch()
        bottom.addLayout(right_controls)

        main.addLayout(bottom)

        self.setLayout(main)

        # drag and drop
        self.setAcceptDrops(True)

        # small internal references
        self.choose_dir_btn = choose_dir_btn

    # Drag/drop handlers
    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls() or e.mimeData().hasText():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent):
        if e.mimeData().hasUrls():
            urls = e.mimeData().urls()
            if urls:
                text = '\n'.join([u.toString() for u in urls])
                self.url_input.setText(text)
        elif e.mimeData().hasText():
            self.url_input.setText(e.mimeData().text())

    # ---------- UI actions ----------
    def handle_add(self):
        raw = self.url_input.text().strip()
        if not raw:
            self.append_log("⚠️ Paste a URL or drop one in.")
            return
        # can be multiple lines
        for line in raw.splitlines():
            url = line.strip()
            if url:
                self.add_url_to_queue(url, default_audio_only=self.audio_only_cb.isChecked(),
                                      default_include_audio=self.include_audio_cb.isChecked())
        self.url_input.clear()

    def handle_preview(self):
        url = self.url_input.text().strip()
        if not url:
            self.append_log("⚠️ Paste a URL for preview.")
            return
        self.append_log("🔎 Fetching info...")
        threading.Thread(target=self._fetch_info_thread, args=(url, True), daemon=True).start()

    def add_url_to_queue(self, url: str, default_audio_only=False, default_include_audio=True):
        threading.Thread(target=self._fetch_info_thread, args=(url, False, default_audio_only, default_include_audio), daemon=True).start()

    def _fetch_info_thread(self, url: str, preview_only=False, default_audio_only=False, default_include_audio=True):
        opts = {'quiet': True, 'no_warnings': True}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            self.append_log(f"Error fetching info: {e}")
            return

        # playlist handling
        entries = []
        if 'entries' in info and info.get('entries'):
            # playlist
            for e in info['entries']:
                if not e:
                    continue
                entries.append(e)
            self.append_log(f"Playlist detected: {info.get('title','(playlist)')} — {len(entries)} items")
        else:
            entries.append(info)

        if preview_only:
            # just show top entry details
            top = entries[0]
            title = top.get('title') or top.get('id')
            thumb = top.get('thumbnail')
            fmt_list = top.get('formats', [])
            # extract unique video formats with height
            video_formats = sorted(
                [f for f in fmt_list if f.get('vcodec') != 'none' and f.get('height')],
                key=lambda x: x.get('height', 0), reverse=True
            )
            self.append_log(f"Preview: {title} | {len(video_formats)} video formats found")
            if thumb:
                self._load_thumbnail_from_url(thumb)
            return

        # add each entry to queue
        for entry in entries:
            job = DownloadJob(url=entry.get('webpage_url') or entry.get('url') or url)
            job.title = entry.get('title') or entry.get('id')
            job.thumbnail = entry.get('thumbnail')
            # collect formats: present human-friendly label
            fmts = entry.get('formats', [])
            video_formats = sorted([f for f in fmts if f.get('vcodec') != 'none' and f.get('height')],
                                   key=lambda x: (x.get('height') or 0, x.get('ext') or ''), reverse=True)
            job.formats = video_formats
            # choose default best
            if video_formats:
                job.selected_format_id = video_formats[0].get('format_id')
            job.include_audio = default_include_audio
            job.audio_only = default_audio_only
            job.subtitles = False
            job.outtmpl = "%(title)s.%(ext)s"
            self.jobs.append(job)
            self.add_job_to_table(job)
            self.append_log(f"Queued: {job.title}")
        self.save_settings()

    def add_job_to_table(self, job: DownloadJob):
        row = self.table.rowCount()
        self.table.insertRow(row)
        job.index_in_table = row

        title_item = QTableWidgetItem(job.title or "Unknown")
        fmt_item = QTableWidgetItem(self._fmt_label(job))
        audio_item = QTableWidgetItem("Audio-only" if job.audio_only else ("Audio" if job.include_audio else "No audio"))
        prog_item = QTableWidgetItem("0%")
        speed_item = QTableWidgetItem("-")
        status_item = QTableWidgetItem(job.status)
        actions_item = QTableWidgetItem("Pause / Remove")

        self.table.setItem(row, 0, title_item)
        self.table.setItem(row, 1, fmt_item)
        self.table.setItem(row, 2, audio_item)
        self.table.setItem(row, 3, prog_item)
        self.table.setItem(row, 4, speed_item)
        self.table.setItem(row, 5, status_item)
        self.table.setItem(row, 6, actions_item)

    def _fmt_label(self, job: DownloadJob):
        if job.audio_only:
            return "Audio (auto)"
        if job.selected_format_id and job.formats:
            fm = next((f for f in job.formats if f.get('format_id') == job.selected_format_id), None)
            if fm:
                return f"{fm.get('height','?')}p · {fm.get('ext','')}"
        return "Auto"

    def _load_thumbnail_from_url(self, url: str):
        try:
            r = requests.get(url, timeout=8)
            r.raise_for_status()
            img = QPixmap()
            img.loadFromData(r.content)
            scaled = img.scaled(self.thumbnail_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.thumbnail_label.setPixmap(scaled)
        except Exception as e:
            self.append_log(f"Failed to load thumbnail: {e}")

    def open_settings(self):
        dlg = SettingsDialog(self, dict(self.settings))
        if dlg.exec():
            # settings dialog changed self.settings in its accept
            self.settings.update(dlg.settings)
            self.choose_dir_btn.setText(f"Save Dir: {self.settings.get('download_dir')}")
            self.concurrent_spin.setValue(self.settings.get('concurrent', 2))
            self.save_settings()
            self.append_log("Settings saved")

    def choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select download directory", self.settings.get('download_dir', os.getcwd()))
        if d:
            self.settings['download_dir'] = d
            self.choose_dir_btn.setText(f"Save Dir: {d}")
            self.save_settings()

    def clear_completed(self):
        # remove rows where status == Complete
        rows_to_remove = []
        for row in range(self.table.rowCount()):
            status = self.table.item(row,5).text()
            if status.lower().startswith("complete") or status.lower().startswith("error") or status.lower().startswith("stopped"):
                rows_to_remove.append(row)
        for r in sorted(rows_to_remove, reverse=True):
            self.table.removeRow(r)
            # remove job from jobs list if index matches
            self.jobs = [j for j in self.jobs if j.index_in_table != r]
        self.append_log("Cleared completed/errored items from UI.")

    def start_queue(self):
        # copy current UI states into jobs and enqueue
        self.settings['concurrent'] = int(self.concurrent_spin.value())
        self.save_settings()

        # enqueue any jobs that are queued or stopped (but not currently active)
        for i, job in enumerate(self.jobs):
            if job.status in ("Queued", "Stopped", "Error"):
                job.status = "Queued"
                self.queue.put((i, job))
                self.append_log(f"Enqueued job {i}: {job.title}")

        # spin up workers up to concurrency
        self._fill_workers()

    def _fill_workers(self):
        while len(self.active_workers) < self.settings.get('concurrent', 2) and not self.queue.empty():
            idx, job = self.queue.get()
            # ensure index matches table index mapping
            worker = DownloadWorker(job=job, index=idx, settings=self.settings, signals=self.signals)
            self.active_workers[idx] = worker
            job.status = "Starting"
            self.table.setItem(job.index_in_table, 5, QTableWidgetItem(job.status))
            worker.start()
            self.append_log(f"Worker started for {job.title} (idx={idx})")

    def stop_all(self):
        self.append_log("Stopping all active downloads (best-effort)...")
        for idx, worker in list(self.active_workers.items()):
            worker.stop()

    # ---------- Worker signal handlers ----------
    def on_progress(self, index: int, percent: int, speed: str, eta: str):
        # find job by index
        job = self.jobs[index] if 0 <= index < len(self.jobs) else None
        if not job:
            return
        job.progress = percent
        job.speed = speed
        job.eta = eta
        # update table
        if job.index_in_table >= 0 and job.index_in_table < self.table.rowCount():
            self.table.setItem(job.index_in_table, 3, QTableWidgetItem(f"{percent}%"))
            self.table.setItem(job.index_in_table, 4, QTableWidgetItem(f"{speed} · {eta}"))

    def on_status(self, index: int, status_text: str):
        job = self.jobs[index] if 0 <= index < len(self.jobs) else None
        if not job:
            return
        job.status = status_text
        if job.index_in_table >=0 and job.index_in_table < self.table.rowCount():
            self.table.setItem(job.index_in_table, 5, QTableWidgetItem(status_text))

    def on_finished(self, index: int, success: bool):
        # cleanup worker and try to start next queued
        if index in self.active_workers:
            del self.active_workers[index]
        job = self.jobs[index] if 0 <= index < len(self.jobs) else None
        if job:
            job.status = "Complete" if success else "Failed/Stopped"
            if job.index_in_table >=0 and job.index_in_table < self.table.rowCount():
                self.table.setItem(job.index_in_table, 5, QTableWidgetItem(job.status))
        self.append_log(f"Job {index} finished: {success}")
        # try to fill more workers
        self._fill_workers()

    # ---------- helpers ----------
    def append_log(self, text: str):
        ts = time.strftime("%H:%M:%S")
        self.log.append(f"[{ts}] {text}")

    def on_table_click(self, row, col):
        # show thumbnail of clicked row's job
        try:
            job = next((j for j in self.jobs if j.index_in_table == row), None)
            if job and job.thumbnail:
                self._load_thumbnail_from_url(job.thumbnail)
        except Exception as e:
            self.append_log(f"Table click error: {e}")

    def start_check_timer(self):
        # simple timer using thread to periodically ensure worker fill
        def loop():
            while True:
                time.sleep(1.0)
                # ensure master supports more workers if queue not empty
                if not self.queue.empty():
                    self._fill_workers()
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        return t


def main():
    app = QApplication(sys.argv)
    win = AdvancedDownloader()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
