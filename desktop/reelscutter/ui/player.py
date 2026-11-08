"""Video preview player: play/pause, scrubbing, volume, frame step, fullscreen, timestamps."""
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSlider, QVBoxLayout, QWidget, QLabel

from .widgets import fmt_dur, label


class FullscreenVideo(QVideoWidget):
    """Video surface; double-click or Esc toggles fullscreen."""
    toggled = Signal()

    def mouseDoubleClickEvent(self, e):
        self.toggled.emit()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and self.isFullScreen():
            self.toggled.emit()
        elif e.key() == Qt.Key_Space:
            self.parent_player.toggle()
        else:
            super().keyPressEvent(e)


class Player(QWidget):
    positionChanged = Signal(float)     # seconds
    durationChanged = Signal(float)

    def __init__(self, compact=False):
        super().__init__()
        self.fps = 30.0
        self.stop_at = None             # seconds; used by "preview selection"
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.audio.setVolume(0.8)
        self.video = FullscreenVideo()
        self.video.parent_player = self
        self.video.setMinimumHeight(160 if compact else 260)
        self.video.setStyleSheet("background:#000;")
        self.player.setVideoOutput(self.video)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(self.video, 1)

        self.scrub = QSlider(Qt.Horizontal)
        self.scrub.setRange(0, 0)
        self.scrub.setToolTip("Drag to scrub through the video")
        lay.addWidget(self.scrub)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_back = QPushButton("◀︎◀︎")
        self.btn_back.setToolTip("Back 5 seconds (⇧←)")
        self.btn_prev = QPushButton("◀︎|")
        self.btn_prev.setToolTip("Previous frame (←)")
        self.btn_play = QPushButton("▶︎  Play")
        self.btn_play.setToolTip("Play / pause (Space)")
        self.btn_play.setMinimumWidth(92)
        self.btn_next = QPushButton("|▶︎")
        self.btn_next.setToolTip("Next frame (→)")
        self.btn_fwd = QPushButton("▶︎▶︎")
        self.btn_fwd.setToolTip("Forward 5 seconds (⇧→)")
        self.time = label("00:00.00 / 00:00.00", "muted")
        self.btn_mute = QPushButton("🔊")
        self.btn_mute.setToolTip("Mute / unmute (M)")
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(80)
        self.vol.setFixedWidth(90)
        self.vol.setToolTip("Volume")
        self.btn_full = QPushButton("⛶")
        self.btn_full.setToolTip("Fullscreen (F). Press Esc or double-click to exit.")
        for b in (self.btn_back, self.btn_prev, self.btn_play, self.btn_next, self.btn_fwd):
            b.setFocusPolicy(Qt.NoFocus)
            row.addWidget(b)
        row.addSpacing(8)
        row.addWidget(self.time)
        row.addStretch()
        for w in (self.btn_mute, self.vol, self.btn_full):
            w.setFocusPolicy(Qt.NoFocus)
            row.addWidget(w)
        lay.addLayout(row)
        if compact:
            for b in (self.btn_back, self.btn_fwd):
                b.hide()

        self._scrubbing = False
        self.btn_play.clicked.connect(self.toggle)
        self.btn_prev.clicked.connect(lambda: self.step(-1))
        self.btn_next.clicked.connect(lambda: self.step(1))
        self.btn_back.clicked.connect(lambda: self.jump(-5))
        self.btn_fwd.clicked.connect(lambda: self.jump(5))
        self.btn_mute.clicked.connect(self.toggle_mute)
        self.vol.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))
        self.btn_full.clicked.connect(self.toggle_fullscreen)
        self.video.toggled.connect(self.toggle_fullscreen)
        self.scrub.sliderPressed.connect(lambda: setattr(self, "_scrubbing", True))
        self.scrub.sliderReleased.connect(self._scrub_done)
        self.scrub.sliderMoved.connect(lambda v: self.player.setPosition(v))
        self.player.positionChanged.connect(self._on_pos)
        self.player.durationChanged.connect(self._on_dur)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.errorOccurred.connect(lambda e, msg: self.time.setText(f"Can't play this file: {msg}"))
        # Show the first frame as soon as a video is opened (Qt shows nothing until playback starts otherwise):
        # play muted until one frame arrives, then pause at the start.
        self._prime = False
        self._prime_muted = False
        self.player.mediaStatusChanged.connect(self._on_status)
        try:
            self.video.videoSink().videoFrameChanged.connect(self._on_frame)
        except Exception:
            pass
        self.frames_shown = 0

    # ---- API
    def load(self, path, fps=None):
        self.stop_at = None
        self.fps = fps or 30.0
        self.player.stop()
        self._prime = bool(path)
        self.player.setSource(QUrl.fromLocalFile(str(path)) if path else QUrl())

    def unload(self):
        self.player.stop()
        self.player.setSource(QUrl())

    def toggle(self):
        if self._prime:
            self._end_prime()
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.stop_at = None if self.stop_at is not None and self.pos() >= self.stop_at - 0.05 else self.stop_at
            self.player.play()

    def pause(self):
        self.player.pause()

    def pos(self):
        return self.player.position() / 1000.0

    def seek(self, t):
        self.player.setPosition(int(max(0.0, t) * 1000))

    def step(self, frames):
        self.player.pause()
        self.seek(self.pos() + frames / max(1.0, self.fps))

    def jump(self, secs):
        self.seek(self.pos() + secs)

    def play_range(self, a, b):
        self.stop_at = b
        self.seek(a)
        self.player.play()

    def toggle_mute(self):
        m = not self.audio.isMuted()
        self.audio.setMuted(m)
        self.btn_mute.setText("🔇" if m else "🔊")

    def toggle_fullscreen(self):
        if self.video.isFullScreen():
            self.video.setFullScreen(False)
        else:
            self.video.setFullScreen(True)
            self.video.setFocus()

    # ---- internals
    def _on_status(self, st):
        if self._prime and st in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia) \
                and self.player.playbackState() != QMediaPlayer.PlayingState:
            self._prime_muted = self.audio.isMuted()
            self.audio.setMuted(True)
            self.player.play()

    def _on_frame(self, frame):
        self.frames_shown += 1
        if self._prime and frame.isValid():
            self._end_prime()
            self.player.pause()
            self.player.setPosition(0)

    def _end_prime(self):
        self._prime = False
        self.audio.setMuted(self._prime_muted)

    def _scrub_done(self):
        self._scrubbing = False
        self.player.setPosition(self.scrub.value())

    def _on_pos(self, ms):
        t = ms / 1000.0
        if self.stop_at is not None and t >= self.stop_at:
            self.player.pause()
            self.stop_at = None
        if not self._scrubbing:
            self.scrub.blockSignals(True)
            self.scrub.setValue(ms)
            self.scrub.blockSignals(False)
        self.time.setText(f"{fmt_dur(t, ms=True)} / {fmt_dur(self.player.duration() / 1000.0, ms=True)}")
        self.positionChanged.emit(t)

    def _on_dur(self, ms):
        self.scrub.setRange(0, max(0, ms))
        self.durationChanged.emit(ms / 1000.0)

    def _on_state(self, st):
        self.btn_play.setText("❚❚  Pause" if st == QMediaPlayer.PlayingState else "▶︎  Play")
