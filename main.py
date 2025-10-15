# very_simple_downloader.py
# REQUIREMENTS: pip install PySide6 yt-dlp requests
# Make sure ffmpeg is available in your system's PATH.

import sys
import os
import threading
import time

import requests
import yt_dlp

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QProgressBar, QTextEdit, QFileDialog, QMessageBox
)
from PySide6.QtGui import QPixmap
from PySide6.QtCore import Qt, Signal, QObject

# --- Worker Signals ---
# Used to communicate from the download thread to the main UI thread
class WorkerSignals(QObject):
    progress = Signal(dict)
    status = Signal(str)
    finished = Signal(bool)
    log = Signal(str)
    info_fetched = Signal(dict)

# --- Download Worker Thread ---
class DownloadWorker(threading.Thread):
    def __init__(self, url, selected_format, download_dir, signals):
        super().__init__(daemon=True)
        self.url = url
        self.selected_format = selected_format
        self.download_dir = download_dir
        self.signals = signals
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True
        self.signals.log.emit("Stop request sent.")

    def run(self):
        self.signals.status.emit("Starting download...")
        
        output_template = os.path.join(self.download_dir, "%(title)s.%(ext)s")

        ydl_opts = {
            'outtmpl': output_template,
            'progress_hooks': [self._hook],
            'quiet': True,
            'no_warnings': True,
            'format': f"{self.selected_format}+bestaudio/best",
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([self.url])
            
            if not self._stop_requested:
                self.signals.finished.emit(True)
            else:
                self.signals.status.emit("Stopped by user")
                self.signals.finished.emit(False)
        except Exception as e:
            # Check if it's a user-initiated stop
            if "Stopped by user" in str(e):
                self.signals.status.emit("Stopped")
            else:
                self.signals.log.emit(f"Error: {str(e)}")
                self.signals.status.emit("Error")
            self.signals.finished.emit(False)

    def _hook(self, d):
        if self._stop_requested:
            raise yt_dlp.utils.DownloadError("Stopped by user")
        
        if d['status'] == 'downloading':
            self.signals.progress.emit(d)
        elif d['status'] == 'finished':
            self.signals.status.emit("Merging files...")

# --- Main Application Window ---
class DownloaderApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Simple Video Downloader")
        self.resize(700, 550)
        
        # --- App State ---
        self.download_dir = os.getcwd()
        self.current_video_info = None
        self.active_worker = None

        # --- Signals ---
        self.signals = WorkerSignals()
        self.signals.info_fetched.connect(self.on_info_fetched)
        self.signals.progress.connect(self.on_progress)
        self.signals.status.connect(self.on_status)
        self.signals.finished.connect(self.on_finished)
        self.signals.log.connect(self.append_log)
        
        self.setup_ui()
        self.reset_ui_state()

    def setup_ui(self):
        """Create and lay out all the widgets."""
        main_layout = QVBoxLayout(self)

        # --- URL Input ---
        url_layout = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Paste a video URL here and press Enter")
        self.url_input.returnPressed.connect(self.fetch_video_info)
        self.fetch_button = QPushButton("Fetch Info")
        self.fetch_button.clicked.connect(self.fetch_video_info)
        url_layout.addWidget(QLabel("URL:"))
        url_layout.addWidget(self.url_input)
        url_layout.addWidget(self.fetch_button)
        main_layout.addLayout(url_layout)

        # --- Video Info Display ---
        self.title_label = QLabel("Video title will appear here")
        self.title_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        self.thumbnail_label = QLabel("Thumbnail")
        self.thumbnail_label.setFixedSize(320, 180)
        self.thumbnail_label.setAlignment(Qt.AlignCenter)
        self.thumbnail_label.setStyleSheet("background-color: #E0E0E0; border: 1px solid #C0C0C0;")

        info_layout = QHBoxLayout()
        info_layout.addWidget(self.thumbnail_label)
        
        download_options_layout = QVBoxLayout()
        download_options_layout.addWidget(self.title_label)
        
        # --- Format Selector ---
        self.format_combo = QComboBox()
        download_options_layout.addWidget(QLabel("Choose Quality:"))
        download_options_layout.addWidget(self.format_combo)
        
        self.download_button = QPushButton("Download")
        self.download_button.clicked.connect(self.start_download)
        download_options_layout.addWidget(self.download_button)
        download_options_layout.addStretch()
        
        info_layout.addLayout(download_options_layout, stretch=1)
        main_layout.addLayout(info_layout)

        # --- Progress and Status ---
        self.progress_bar = QProgressBar()
        self.status_label = QLabel("Status: Idle")
        main_layout.addWidget(self.progress_bar)
        main_layout.addWidget(self.status_label)

        # --- Log and Directory Controls ---
        bottom_layout = QHBoxLayout()
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFixedHeight(100)
        
        dir_layout = QVBoxLayout()
        self.dir_label = QLabel(f"Saving to: {self.download_dir}")
        self.dir_label.setWordWrap(True)
        change_dir_button = QPushButton("Change Directory")
        change_dir_button.clicked.connect(self.change_directory)
        dir_layout.addWidget(self.dir_label)
        dir_layout.addWidget(change_dir_button)
        
        bottom_layout.addWidget(self.log_box, stretch=1)
        bottom_layout.addLayout(dir_layout)
        main_layout.addLayout(bottom_layout)

    def reset_ui_state(self):
        """Reset the UI to its initial state, ready for a new URL."""
        self.url_input.setEnabled(True)
        self.fetch_button.setEnabled(True)
        self.download_button.setEnabled(False)
        self.title_label.setText("Video title will appear here")
        self.thumbnail_label.setText("Thumbnail")
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0%")
        self.status_label.setText("Status: Idle")
        self.format_combo.clear()
        self.current_video_info = None

    # --- Core Methods ---
    def fetch_video_info(self):
        """Starts a thread to fetch video information."""
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "Input Error", "Please enter a URL.")
            return

        self.status_label.setText("Status: Fetching video information...")
        self.url_input.setEnabled(False)
        self.fetch_button.setEnabled(False)
        
        # Run the network request in a separate thread
        threading.Thread(target=self._fetch_info_thread, args=(url,), daemon=True).start()

    def _fetch_info_thread(self, url):
        """The actual fetching logic that runs in the background."""
        try:
            with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl:
                info = ydl.extract_info(url, download=False)
            self.signals.info_fetched.emit(info)
        except Exception as e:
            self.signals.log.emit(f"Could not fetch info: {str(e)}")
            self.signals.status.emit("Error fetching info. Check URL and try again.")
            self.signals.info_fetched.emit(None) # Signal failure

    def start_download(self):
        """Starts the download worker thread."""
        if not self.current_video_info:
            QMessageBox.critical(self, "Error", "No video info available to start download.")
            return
            
        selected_format_id = self.format_combo.currentData()
        if not selected_format_id:
            QMessageBox.warning(self, "Input Error", "Please select a quality.")
            return

        self.download_button.setText("Stop")
        self.download_button.clicked.disconnect()
        self.download_button.clicked.connect(self.stop_download)
        
        self.fetch_button.setEnabled(False)
        self.format_combo.setEnabled(False)

        self.active_worker = DownloadWorker(
            url=self.current_video_info['webpage_url'],
            selected_format=selected_format_id,
            download_dir=self.download_dir,
            signals=self.signals
        )
        self.active_worker.start()

    def stop_download(self):
        """Stops the currently active download worker."""
        if self.active_worker:
            self.active_worker.stop()
            self.download_button.setEnabled(False) # Prevent multiple clicks
            self.status_label.setText("Status: Stopping...")

    def change_directory(self):
        """Opens a dialog to select a new download directory."""
        directory = QFileDialog.getExistingDirectory(self, "Select Download Directory", self.download_dir)
        if directory:
            self.download_dir = directory
            self.dir_label.setText(f"Saving to: {self.download_dir}")
            self.append_log(f"Save directory set to: {directory}")

    # --- Signal Handlers (slots) ---
    def on_info_fetched(self, info):
        """Handles the result from the info fetching thread."""
        if info is None: # Handle failure
            self.reset_ui_state()
            self.url_input.clear()
            return

        self.current_video_info = info
        self.title_label.setText(info.get('title', 'N/A'))
        self.format_combo.clear()
        
        # Filter for video formats and populate the dropdown
        video_formats = [f for f in info.get('formats', []) if f.get('vcodec') != 'none' and f.get('height')]
        video_formats.sort(key=lambda f: f.get('height', 0), reverse=True)

        for f in video_formats:
            label = f"{f.get('height')}p ({f.get('ext')})"
            self.format_combo.addItem(label, userData=f['format_id'])
        
        self.download_button.setEnabled(True)
        self.status_label.setText("Status: Ready to download.")
        
        # Load thumbnail in a separate thread
        thumb_url = info.get('thumbnail')
        if thumb_url:
            threading.Thread(target=self._load_thumbnail, args=(thumb_url,), daemon=True).start()

    def _load_thumbnail(self, url):
        """Loads image from URL and sets it on the label."""
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            pixmap = QPixmap()
            pixmap.loadFromData(response.content)
            scaled_pixmap = pixmap.scaled(self.thumbnail_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.thumbnail_label.setPixmap(scaled_pixmap)
        except Exception as e:
            self.signals.log.emit(f"Failed to load thumbnail: {e}")
            self.thumbnail_label.setText("No Thumbnail")

    def on_progress(self, progress_dict):
        """Updates the progress bar."""
        try:
            percent_str = progress_dict.get('_percent_str', '0%').strip('%')
            percent = int(float(percent_str))
            speed = progress_dict.get('_speed_str', 'N/A')
            self.progress_bar.setValue(percent)
            self.progress_bar.setFormat(f"{percent}% ({speed})")
            self.status_label.setText("Status: Downloading...")
        except (ValueError, TypeError):
            pass

    def on_status(self, text):
        """Updates the status label."""
        self.status_label.setText(f"Status: {text}")

    def on_finished(self, success):
        """Called when the download worker thread finishes."""
        self.status_label.setText("Status: Download Complete!" if success else "Status: Download Failed or Stopped.")
        self.progress_bar.setValue(100)
        self.progress_bar.setFormat("Done" if success else "Failed")
        
        self.active_worker = None
        
        # Reset the download button
        self.download_button.setText("Download")
        try:
            self.download_button.clicked.disconnect()
        except RuntimeError:
            pass # Already disconnected
        self.download_button.clicked.connect(self.start_download)
        self.download_button.setEnabled(False) # User must fetch a new video
        
        # Re-enable UI for next video
        self.fetch_button.setEnabled(True)
        self.url_input.setEnabled(True)
        self.url_input.clear()
        self.format_combo.setEnabled(True)

        if success:
            QMessageBox.information(self, "Success", "Video downloaded successfully!")

    def append_log(self, text):
        """Adds a timestamped message to the log box."""
        ts = time.strftime("%H:%M:%S")
        self.log_box.append(f"[{ts}] {text}")

def main():
    app = QApplication(sys.argv)
    win = DownloaderApp()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
