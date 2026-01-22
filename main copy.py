import sys
import os
import tempfile
import subprocess
import json
import logging
import shutil
import re
from PySide6.QtWidgets import (QApplication, QMainWindow, QLabel, 
                               QVBoxLayout, QHBoxLayout, QWidget, 
                               QPushButton, QFileDialog, QSlider, QFrame,
                               QListWidget, QListWidgetItem, QLineEdit,
                               QGroupBox, QProgressBar, QMessageBox, 
                               QTimeEdit, QDoubleSpinBox)
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtCore import Qt, QUrl, QTime, QProcess, QObject, QEvent, QTimer, QRect
from PySide6.QtGui import QKeyEvent, QPainter, QColor, QPen

LOG_FILE = os.path.expanduser("~/last_app_run.log")
logging.basicConfig(
    filename=LOG_FILE,
    filemode='w',
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

try:
    import pygame
except ImportError:
    logging.error("Audio engine 'pygame' missing.")

class FrameStepTimeEdit(QTimeEdit):
    def stepBy(self, steps):
        if self.currentSectionIndex() == 3:
            offset = steps * 42 
            new_time = self.time().addMSecs(offset)
            self.setTime(new_time)
        else:
            super().stepBy(steps)

class ChapterSlider(QSlider):
    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.markers = [] 

    def set_markers(self, markers):
        self.markers = markers
        self.update() 

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.markers or self.maximum() <= 0:
            return
        painter = QPainter(self)
        pen = QPen(QColor(0, 191, 255)) 
        pen.setWidth(2)
        painter.setPen(pen)
        for ms in self.markers:
            x = self.style().sliderPositionFromValue(0, self.maximum(), ms, self.width())
            painter.drawLine(x, 0, x, self.height())
        painter.end()

class GlobalHotkeyFilter(QObject):
    def __init__(self, parent, callback):
        super().__init__(parent)
        self.callback = callback

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            focused_widget = QApplication.focusWidget()
            if isinstance(focused_widget, QLineEdit):
                return False
            return self.callback(event)
        return False

class ChapterStudio(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Chapter Marker Studio")
        self.resize(1400, 950)
        
        logging.info("Application Initializing...")
        
        pygame.mixer.pre_init(frequency=48000, size=-16, channels=2, buffer=2048)
        pygame.init()

        self.temp_wav = None
        self.active_video_path = None
        self.video_duration_ms = 0
        self.mkchap_path = os.path.expanduser("~/mkchap/bin/Debug/net8.0/mkchap")
        
        self.hotkey_filter = GlobalHotkeyFilter(self, self.handle_global_hotkeys)
        qApp.installEventFilter(self.hotkey_filter)

        self.scan_process = QProcess(self)
        self.save_process = QProcess(self)
        
        self.scan_process.readyReadStandardOutput.connect(self.handle_scan_output)
        self.scan_process.readyReadStandardError.connect(self.handle_scan_output)
        self.scan_process.finished.connect(self.on_scan_process_finished)
        self.save_process.readyReadStandardError.connect(self.handle_ffmpeg_progress)
        self.save_process.finished.connect(self.on_ffmpeg_finished)

        self.init_ui()

        self.media_player = QMediaPlayer()
        self.media_player.setVideoOutput(self.video_widget)
        self.media_player.playbackStateChanged.connect(self.update_play_button_text)
        self.media_player.positionChanged.connect(self.update_position)
        self.media_player.durationChanged.connect(self.update_duration)
        self.is_scrubbing = False
        self.scan_stdout_buffer = ""

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        master_layout = QHBoxLayout(central_widget)

        left_column = QVBoxLayout()
        self.video_widget = QVideoWidget()
        self.video_widget.setStyleSheet("background-color: black; border: 1px solid #444;")
        left_column.addWidget(self.video_widget, stretch=5)

        player_suite = QFrame()
        player_suite.setStyleSheet("background-color: #1a1a1a; border-radius: 4px;")
        suite_layout = QVBoxLayout(player_suite)
        
        self.slider = ChapterSlider(Qt.Orientation.Horizontal)
        self.slider.sliderPressed.connect(self.on_slider_pressed)
        self.slider.sliderReleased.connect(self.on_slider_released)
        self.slider.sliderMoved.connect(self.set_position)
        suite_layout.addWidget(self.slider)

        ctrl_row = QHBoxLayout()
        btn_style = "QPushButton { background-color: #333; color: white; border: 1px solid #555; padding: 8px 20px; border-radius: 4px; font-weight: bold; } QPushButton:hover { background-color: #444; } "
        
        self.btn_play = QPushButton("Play")
        self.btn_play.setStyleSheet(btn_style)
        self.btn_play.clicked.connect(self.toggle_playback)
        ctrl_row.addWidget(self.btn_play)

        self.btn_stop = QPushButton("Unload")
        self.btn_stop.setStyleSheet(btn_style)
        self.btn_stop.clicked.connect(self.stop_playback)
        ctrl_row.addWidget(self.btn_stop)

        self.time_label = QLabel("00:00:00.000")
        self.time_label.setStyleSheet("font-family: monospace; font-size: 24px; color: #00FF00; padding: 0 20px;")
        ctrl_row.addWidget(self.time_label)
        ctrl_row.addStretch() 
        
        self.btn_add_marker = QPushButton("+ Add Marker (M)")
        self.btn_add_marker.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold; padding: 8px 15px;")
        self.btn_add_marker.clicked.connect(self.add_manual_marker)
        ctrl_row.addWidget(self.btn_add_marker)

        suite_layout.addLayout(ctrl_row)
        left_column.addWidget(player_suite)
        master_layout.addLayout(left_column, stretch=3)

        right_column = QVBoxLayout()
        
        scan_group = QGroupBox("Video Analysis Settings")
        scan_layout = QVBoxLayout()
        settings_row = QHBoxLayout()
        
        v_thresh = QVBoxLayout()
        v_thresh.addWidget(QLabel("Black Threshold (0.01 - 1.0):"))
        self.spin_thresh = QDoubleSpinBox()
        self.spin_thresh.setRange(0.01, 1.0)
        self.spin_thresh.setValue(0.05)
        v_thresh.addWidget(self.spin_thresh)
        settings_row.addLayout(v_thresh)
        
        v_dur = QVBoxLayout()
        v_dur.addWidget(QLabel("Min Duration (s) (0.1 - 10.0):"))
        self.spin_dur = QDoubleSpinBox()
        self.spin_dur.setRange(0.1, 10.0)
        self.spin_dur.setValue(0.5)
        v_dur.addWidget(self.spin_dur)
        settings_row.addLayout(v_dur)
        
        scan_layout.addLayout(settings_row)
        self.btn_run_scan = QPushButton("SCAN VIDEO FILE FOR BLACK FRAMES")
        self.btn_run_scan.setStyleSheet("background-color: #3d5afe; color: black; height: 40px;")
        self.btn_run_scan.clicked.connect(self.confirm_scan)
        scan_layout.addWidget(self.btn_run_scan)
        
        self.scan_progress = QProgressBar()
        self.scan_progress.setVisible(False)
        self.scan_label = QLabel("")
        scan_layout.addWidget(self.scan_progress)
        scan_layout.addWidget(self.scan_label)
        scan_group.setLayout(scan_layout)
        right_column.addWidget(scan_group)

        self.chapter_list = QListWidget()
        self.chapter_list.itemDoubleClicked.connect(self.seek_to_chapter)
        self.chapter_list.itemClicked.connect(self.load_chapter_details)
        right_column.addWidget(self.chapter_list)

        self.btn_read_existing = QPushButton("READ EXISTING CHAPTERS")
        self.btn_read_existing.clicked.connect(self.read_existing_chapters)
        right_column.addWidget(self.btn_read_existing)

        self.edit_group = QGroupBox("Edit Chapter Marker Placement")
        edit_layout = QVBoxLayout()
        self.edit_name = QLineEdit()
        self.edit_name.textChanged.connect(self.update_chapter_data)

        nudge_layout = QHBoxLayout()
        self.btn_nudge_back = QPushButton("< -1 Frame")
        self.btn_nudge_fwd = QPushButton("+1 Frame >")
        self.btn_nudge_back.clicked.connect(lambda: self.nudge_frame(-42))
        self.btn_nudge_fwd.clicked.connect(lambda: self.nudge_frame(42))
        nudge_layout.addWidget(self.btn_nudge_back)
        nudge_layout.addWidget(self.btn_nudge_fwd)

        edit_layout.addWidget(QLabel("Chapter Name:"))
        edit_layout.addWidget(self.edit_name)
        edit_layout.addWidget(QLabel("Adjust Timestamp:"))
        edit_layout.addLayout(nudge_layout)
        
        self.btn_rem_marker = QPushButton("Delete Selected Marker (R)")
        self.btn_rem_marker.setStyleSheet("background-color: #ff0000; color: black; bold;")
        self.btn_rem_marker.clicked.connect(self.remove_selected_marker)
        edit_layout.addWidget(self.btn_rem_marker)
        self.edit_group.setLayout(edit_layout)
        right_column.addWidget(self.edit_group)

        self.btn_save_video = QPushButton("WRITE CHAPTERS TO VIDEO FILE")
        self.btn_save_video.setStyleSheet("background-color: #ef6c00; color: black; height: 50px;")
        self.btn_save_video.clicked.connect(self.confirm_write)
        right_column.addWidget(self.btn_save_video)
        
        self.save_progress = QProgressBar()
        self.save_progress.setVisible(False)
        right_column.addWidget(self.save_progress)

        self.btn_load_video = QPushButton("Load Video File...")
        self.btn_load_video.clicked.connect(self.open_file_dialog)
        right_column.addWidget(self.btn_load_video)

        master_layout.addLayout(right_column, stretch=1)

    def handle_global_hotkeys(self, event):
        if event.key() == Qt.Key_Space:
            self.toggle_playback()
            return True
        elif event.key() == Qt.Key_M:
            self.add_manual_marker()
            return True
        elif event.key() == Qt.Key_R:
            self.remove_selected_marker()
            return True
        elif event.key() == Qt.Key_Left:
            self.step_frame(-42)
            return True
        elif event.key() == Qt.Key_Right:
            self.step_frame(42)
            return True
        return False

    def step_frame(self, offset_ms):
        if not self.active_video_path: return
        new_pos = max(0, self.media_player.position() + offset_ms)
        self.media_player.setPosition(new_pos)
        if self.temp_wav: 
            pygame.mixer.music.set_pos(new_pos / 1000.0)

    def open_file_dialog(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Select Video", "", "Video (*.mp4 *.mkv *.ts *.mov)")
        if file_path:
            self.active_video_path = file_path
            self.btn_load_video.setText("Extracting Audio...")
            QApplication.processEvents()

            if self.temp_wav and os.path.exists(self.temp_wav):
                try: os.remove(self.temp_wav)
                except: pass

            self.temp_wav = os.path.join(tempfile.gettempdir(), f"studio_{os.getpid()}.wav")
            subprocess.run(['ffmpeg', '-y', '-i', file_path, '-vn', '-acodec', 'pcm_s16le', '-ar', '48000', '-ac', '2', self.temp_wav], 
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            pygame.mixer.music.load(self.temp_wav)
            self.media_player.setSource(QUrl.fromLocalFile(file_path))
            self.chapter_list.clear()
            self.add_chapter_to_ui(0, "Start")
            self.btn_load_video.setText(f"File: {os.path.basename(file_path)}")
            self.media_player.play()
            pygame.mixer.music.play()

    def confirm_scan(self):
        if not self.active_video_path: return
        if QMessageBox.question(self, "Scan", "Analyze video for black frames?") == QMessageBox.StandardButton.Yes:
            self.run_mkchap_scan()

    def run_mkchap_scan(self):
        logging.info(f"SCAN START: {self.active_video_path}")
        self.btn_run_scan.setEnabled(False)
        self.scan_progress.setVisible(True)
        self.scan_progress.setRange(0, 0)
        self.btn_run_scan.setText("Scanning...")
        self.btn_run_scan.setEnabled(False)
        self.scan_stdout_buffer = ""
        
        args = [
            self.active_video_path,
            "-b", str(self.spin_thresh.value()),
            "-s", str(int(self.spin_dur.value())) 
        ]
        logging.debug(f"Executing: {self.mkchap_path} {' '.join(args)}")
        self.scan_process.start(self.mkchap_path, args)

    def handle_scan_output(self):
        out = self.scan_process.readAllStandardOutput().data().decode()
        err = self.scan_process.readAllStandardError().data().decode()
        self.scan_stdout_buffer += out
        if err: logging.debug(f"Process Error/Help Stream: {err}")

    def on_scan_process_finished(self, exit_code):
        logging.debug(f"Scan Finished. Exit Code: {exit_code}")
        self.scan_progress.setVisible(False)
        self.btn_run_scan.setEnabled(True)
        self.btn_run_scan.setText("SCAN VIDEO FILE FOR BLACK FRAMES")
        if exit_code == 0:
            self.chapter_list.clear()
            self.add_chapter_to_ui(0, "Start")
            try:
                json_start = self.scan_stdout_buffer.find('{')
                if json_start != -1:
                    data = json.loads(self.scan_stdout_buffer[json_start:])
                    for sec in data.get("BlackSections", []):
                        if sec.get("State", "Ok") == "Ok":
                            ms = self.parse_iso_to_ms(sec.get("Start", "00:00:00.000"))
                            if ms > 0: self.add_chapter_to_ui(ms, "")
                    self.resequence_names()
                else:
                    logging.error("No JSON found in scan output.")
            except Exception as e:
                logging.error(f"Scan JSON Error: {e}")

    def parse_iso_to_ms(self, iso_str):
        try:
            parts = iso_str.split('.')
            hms = parts[0].split(':')
            ms_part = parts[1][:3] if len(parts) > 1 else "0"
            total_ms = (int(hms[0]) * 3600 + int(hms[1]) * 60 + int(hms[2])) * 1000
            total_ms += int(ms_part.ljust(3, '0'))
            return total_ms
        except: return 0

    def confirm_write(self):
        if not self.active_video_path: return
        if QMessageBox.question(self, "Write", "Apply Chapter Markers to Video File? (This will OVERWRITE existing markers)") == QMessageBox.StandardButton.Yes:
            self.save_chapters_via_ffmpeg()

    def handle_ffmpeg_progress(self):
        data = self.save_process.readAllStandardError().data().decode()
        match = re.search(r"time=(\d+):(\d+):(\d+.\d+)", data)
        if match and self.video_duration_ms > 0:
            h, m, s = map(float, match.groups())
            elapsed_ms = (h * 3600 + m * 60 + s) * 1000
            percent = min(100, int((elapsed_ms / self.video_duration_ms) * 100))
            self.save_progress.setValue(percent)

    def save_chapters_via_ffmpeg(self):
        self.media_player.pause()
        pygame.mixer.music.pause()
        self.save_progress.setVisible(True)
        self.video_duration_ms = self.media_player.duration()
        self.btn_save_video.setText("Writing...")
        self.btn_save_video.setEnabled(False)
        meta_path = os.path.join(tempfile.gettempdir(), f"metadata_{os.getpid()}.txt")
        chapters = []
        for i in range(self.chapter_list.count()):
            item = self.chapter_list.item(i)
            chapters.append({'start': item.data(Qt.ItemDataRole.UserRole), 'title': item.data(Qt.ItemDataRole.DisplayRole + 1)})
        chapters.sort(key=lambda x: x['start'])

        with open(meta_path, "w") as f:
            f.write(";FFMETADATA1\n")
            for i in range(len(chapters)):
                start = int(chapters[i]['start'])
                end = int(chapters[i+1]['start']) if i+1 < len(chapters) else self.video_duration_ms
                if end <= start: end = start + 1
                f.write(f"\n[CHAPTER]\nTIMEBASE=1/1000\nSTART={start}\nEND={end}\ntitle={chapters[i]['title']}\n")

        ext = os.path.splitext(self.active_video_path)[1]
        self.temp_out = os.path.join(tempfile.gettempdir(), f"out_{os.getpid()}{ext}")
        args = ['-y', '-i', self.active_video_path, '-i', meta_path, '-map_metadata', '1', '-map_chapters', '1', '-codec', 'copy', self.temp_out]
        self.save_process.start('ffmpeg', args)

    def on_ffmpeg_finished(self, exit_code):
        self.save_progress.setVisible(False)
        self.btn_save_video.setEnabled(True)
        self.btn_save_video.setText("WRITE CHAPTERS TO VIDEO FILE")
        if exit_code == 0:
            try:
                shutil.move(self.temp_out, self.active_video_path)
                QMessageBox.information(self, "Success", "Chapters saved successfully!")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to move file: {e}")
        else:
            QMessageBox.critical(self, "Error", "FFmpeg failed to write chapters.")

    def add_chapter_to_ui(self, ms, label):
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, ms)
        item.setData(Qt.ItemDataRole.DisplayRole + 1, label)
        self.chapter_list.addItem(item)
        self.resequence_names()

    def resequence_names(self):
        self.chapter_list.blockSignals(True)
        self.chapter_list.sortItems()
        chapter_idx = 1
        marker_positions = []
        for i in range(self.chapter_list.count()):
            item = self.chapter_list.item(i)
            ms = item.data(Qt.ItemDataRole.UserRole)
            marker_positions.append(ms)
            existing_name = item.data(Qt.ItemDataRole.DisplayRole + 1)
            
            if existing_name and not existing_name.startswith("Chapter ") and existing_name != "Start":
                name = existing_name
            else:
                name = "Start" if ms <= 100 else f"Chapter {chapter_idx}"
            
            if ms > 100: chapter_idx += 1
            t_str = QTime(0, 0).addMSecs(ms).toString("HH:mm:ss.zzz")
            item.setText(f"[{t_str}] {name}")
            item.setData(Qt.ItemDataRole.DisplayRole + 1, name)
        self.slider.set_markers(marker_positions)
        self.chapter_list.blockSignals(False)

    def load_chapter_details(self, item):
        ms = item.data(Qt.ItemDataRole.UserRole)
        display_text = item.text()
        try:
            name_part = display_text.split("] ", 1)[1]
        except IndexError:
            name_part = ""

        self.edit_name.blockSignals(True)
        self.edit_name.setText(name_part)
        self.edit_name.blockSignals(False)
        self.media_player.setPosition(ms)

    def handle_time_edit(self, new_time):
        item = self.chapter_list.currentItem()
        if not item: return
        new_ms = QTime(0,0).msecsTo(new_time)
        item.setData(Qt.ItemDataRole.UserRole, new_ms)
        self.media_player.setPosition(new_ms)
        if self.temp_wav: pygame.mixer.music.set_pos(new_ms / 1000.0)
        self.update_chapter_metadata()

    def update_chapter_data(self):
        item = self.chapter_list.currentItem()
        if not item: return

        new_name = self.edit_name.text()
        ms = item.data(Qt.ItemDataRole.UserRole)
        t_str = QTime(0, 0).addMSecs(ms).toString("HH:mm:ss.zzz")

        item.setText(f"[{t_str}] {new_name}")
        self.resequence_names()

    def add_manual_marker(self):
        if self.active_video_path: self.add_chapter_to_ui(self.media_player.position(), "")
        self.resequence_names()

    def remove_selected_marker(self):
        row = self.chapter_list.currentRow()
        if row >= 0:
            reply = QMessageBox.question(self, 'Confirm Deletion',
                                        "Are you sure you want to remove the selected chapter marker from the list?",
                                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                self.chapter_list.takeItem(row)
                self.resequence_names()

    def seek_to_chapter(self, item):
        ms = item.data(Qt.ItemDataRole.UserRole)
        self.media_player.setPosition(ms)
        if self.temp_wav: pygame.mixer.music.set_pos(ms / 1000.0)

    def toggle_playback(self):
        if not self.active_video_path: return
        if self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.media_player.pause()
            pygame.mixer.music.pause()
            self.btn_play.setText("Play")
        else:
            self.media_player.play()
            pygame.mixer.music.unpause()
            self.btn_play.setText("Pause")

    def stop_playback(self):
        if not self.active_video_path: return

        reply = QMessageBox.question(self, 'Confirm Stop',
                                    "Are you sure you want to unload the video? All changes will be lost.",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)

        if reply == QMessageBox.Yes:
            self.media_player.stop()
            if self.temp_wav: pygame.mixer.music.stop()
            self.active_video_path = None
            self.chapter_list.clear()
            self.btn_load_video.setText("Load Video File...")
            self.resequence_names()
            self.edit_name.clear()
            self.edit_time.setTime(QTime(0, 0))
            self.btn_play.setText("Play")
            self.time_label.setText("00:00:00.000")
            self.slider.setValue(0)
        if self.temp_wav and os.path.exists(self.temp_wav):
            try: os.remove(self.temp_wav)
            except: pass

    def nudge_frame(self, ms_offset):
        item = self.chapter_list.currentItem()
        if not item: return

        current_ms = item.data(Qt.ItemDataRole.UserRole)
        new_ms = max(0, current_ms + ms_offset)

        item.setData(Qt.ItemDataRole.UserRole, new_ms)

        name = item.data(Qt.ItemDataRole.DisplayRole + 1)
        t_str = QTime(0, 0).addMSecs(new_ms).toString("HH:mm:ss.zzz")
        item.setText(f"[{t_str}] {name}")

        self.media_player.setPosition(new_ms)
        self.resequence_names()

    def update_position(self, pos):
        if not self.is_scrubbing: self.slider.setValue(pos)
        self.time_label.setText(QTime(0, 0).addMSecs(pos).toString("HH:mm:ss.zzz"))

    def update_duration(self, dur): self.slider.setRange(0, dur)
    def on_slider_pressed(self): self.is_scrubbing = True
    def on_slider_released(self):
        self.is_scrubbing = False
        self.media_player.setPosition(self.slider.value())
        if self.temp_wav: pygame.mixer.music.set_pos(self.slider.value() / 1000.0)
    def set_position(self, pos):
        if self.is_scrubbing:
            self.media_player.setPosition(pos)
            if self.temp_wav: pygame.mixer.music.set_pos(pos / 1000.0)

    def update_play_button_text(self, state):
        if state == QMediaPlayer.PlayingState:
            self.btn_play.setText("Pause")
        else:
            self.btn_play.setText("Play")

    def read_existing_chapters(self):
        if not self.active_video_path: return

        if self.chapter_list.count() > 0:
            reply = QMessageBox.question(self, 'Confirm Overwrite',
                                        "This will clear your current list. Continue?",
                                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.No: return
            self.chapter_list.clear()

        try:
            cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_chapters", self.active_video_path]
            result = subprocess.run(cmd, capture_output=True, text=True)
            data = json.loads(result.stdout)
            chapters = data.get("chapters", [])

            if not chapters:
                QMessageBox.information(self, "Info", "No existing chapters found.")
                return

            for ch in chapters:
                start_ms = int(float(ch["start_time"]) * 1000)
                title = ch.get("tags", {}).get("title", f"Chapter {chapters.index(ch)+1}")

                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, start_ms)
                item.setData(Qt.ItemDataRole.DisplayRole + 1, title)
                self.chapter_list.addItem(item)

            self.resequence_names()

            QMessageBox.information(self, "Success", f"Loaded {len(chapters)} chapters.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to read chapters: {str(e)}")

    def closeEvent(self, event):
        reply = QMessageBox.question(self, 'Confirm Exit',
                                    "Are you sure you want to exit?",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)

        if reply == QMessageBox.Yes:
            event.accept()
        else:
            event.ignore()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = ChapterStudio()
    window.show()
    sys.exit(app.exec())
