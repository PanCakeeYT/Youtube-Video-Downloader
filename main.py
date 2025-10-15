# /// YouTube Downloader GUI (Minimal High-End) ///
# Requirements:
# pip install PySide6 yt-dlp

import sys
import threading
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel, QLineEdit, QPushButton,
    QComboBox, QCheckBox, QProgressBar, QTextEdit
)
from PySide6.QtGui import QPalette, QColor
from PySide6.QtCore import Qt
import yt_dlp


class YTDownloader(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("YouTube Downloader")
        self.setFixedSize(500, 400)
        self.setup_ui()
        self.formats = []

    def setup_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(12)

        # Dark theme
        palette = QPalette()
        palette.setColor(QPalette.Window, QColor("#121212"))
        palette.setColor(QPalette.WindowText, QColor("#EEEEEE"))
        palette.setColor(QPalette.Base, QColor("#1E1E1E"))
        palette.setColor(QPalette.AlternateBase, QColor("#2C2C2C"))
        palette.setColor(QPalette.Text, QColor("#FFFFFF"))
        palette.setColor(QPalette.Button, QColor("#2C2C2C"))
        palette.setColor(QPalette.ButtonText, QColor("#FFFFFF"))
        self.setPalette(palette)

        self.url_label = QLabel("YouTube URL:")
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Paste video link here...")
        self.url_input.setStyleSheet("padding: 8px; border-radius: 6px;")

        self.fetch_btn = QPushButton("Fetch Resolutions")
        self.fetch_btn.clicked.connect(self.fetch_formats)
        self.fetch_btn.setStyleSheet("padding: 8px; border-radius: 6px;")

        self.res_label = QLabel("Select Resolution:")
        self.res_combo = QComboBox()
        self.res_combo.setStyleSheet("padding: 6px; border-radius: 6px; background: #1E1E1E;")

        self.audio_checkbox = QCheckBox("Include Audio")
        self.audio_checkbox.setChecked(True)

        self.download_btn = QPushButton("Download")
        self.download_btn.clicked.connect(self.start_download)
        self.download_btn.setStyleSheet("padding: 10px; border-radius: 6px; background: #444;")

        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.progress.setStyleSheet("""
            QProgressBar {
                border: 1px solid #333;
                border-radius: 5px;
                background: #1E1E1E;
                height: 16px;
            }
            QProgressBar::chunk {
                background-color: #00BFA5;
                border-radius: 5px;
            }
        """)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("background: #1A1A1A; border-radius: 6px; color: #ccc;")

        layout.addWidget(self.url_label)
        layout.addWidget(self.url_input)
        layout.addWidget(self.fetch_btn)
        layout.addWidget(self.res_label)
        layout.addWidget(self.res_combo)
        layout.addWidget(self.audio_checkbox)
        layout.addWidget(self.download_btn)
        layout.addWidget(self.progress)
        layout.addWidget(self.log)

        self.setLayout(layout)

    def fetch_formats(self):
        url = self.url_input.text().strip()
        if not url:
            self.log.append("⚠️ Please enter a URL.")
            return

        self.log.append("🔎 Fetching formats...")
        threading.Thread(target=self._fetch_formats_thread, args=(url,), daemon=True).start()

    def _fetch_formats_thread(self, url):
        ydl_opts = {'quiet': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            self.formats = sorted(
                [f for f in formats if f.get('vcodec') != 'none' and f.get('height')],
                key=lambda x: x['height'], reverse=True
            )

        self.res_combo.clear()
        for f in self.formats:
            label = f"{f['height']}p ({f['ext']})"
            self.res_combo.addItem(label, f)

        self.log.append("✅ Formats loaded.")

    def start_download(self):
        url = self.url_input.text().strip()
        if not url:
            self.log.append("⚠️ Please enter a URL.")
            return
        if not self.formats:
            self.log.append("⚠️ Fetch resolutions first.")
            return

        selected_format = self.res_combo.currentData()
        include_audio = self.audio_checkbox.isChecked()

        threading.Thread(
            target=self._download_thread,
            args=(url, selected_format, include_audio),
            daemon=True
        ).start()

    def _download_thread(self, url, fmt, include_audio):
        self.log.append(f"⬇️ Starting download: {fmt['height']}p {'+ audio' if include_audio else '(no audio)'}")

        def hook(d):
            if d['status'] == 'downloading':
                p = d.get('_percent_str', '0%').replace('%', '')
                try:
                    self.progress.setValue(int(float(p)))
                except:
                    pass
            elif d['status'] == 'finished':
                self.progress.setValue(100)
                self.log.append("✅ Download complete.")

        ydl_opts = {
            'progress_hooks': [hook],
            'outtmpl': '%(title)s.%(ext)s',
        }

        if include_audio:
            ydl_opts['format'] = f"{fmt['format_id']}+bestaudio/best"
        else:
            ydl_opts['format'] = f"{fmt['format_id']}"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = YTDownloader()
    window.show()
    sys.exit(app.exec())
