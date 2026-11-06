"""音乐播放器模块。

职责：
1. 扫描 music/ 目录，收集支持的音频文件
2. 封装 QMediaPlayer：播放/暂停/上下一首/随机/循环
3. 对外发信号，供桌宠气泡显示歌名

只依赖 QtMultimedia，不碰 UI 布局。
"""

import os
import random
import re

# ── 多媒体后端选择（必须在 import QtMultimedia 之前）─────────────
# Qt 6.7 默认挑 ffmpeg 后端，但本机 PyQt6 的 ffmpegmediaplugin.dll 缺依赖
# 动态库（avcodec 等）加载失败；Qt 不会自动回退到别的后端，于是
# QMediaPlayer 直接是「Not available」——点播放毫无反应、也不报错。
# 实测 Windows Media Foundation 后端（windowsmediaplugin.dll）加载正常、
# mp3 能正常出声，所以默认指定它。想试 ffmpeg 就自己设 QT_MEDIA_BACKEND=ffmpeg。
os.environ.setdefault("QT_MEDIA_BACKEND", "windows")

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer

import paths
import trace

AUDIO_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma", ".opus")

# 打包后音乐目录在 exe 旁边（用户要往里放歌），而不是包内只读区
# seed_from_resource=True：首次运行时把包内的示例歌和「放音乐到这里.txt」复制出来，
# 用户点「打开 music 目录」就能看到说明，不会面对一个空文件夹
MUSIC_DIR = paths.ensure_external("music", seed_from_resource=True)


def scan_music(folder: str = MUSIC_DIR):
    """递归扫描目录下的音频文件，返回绝对路径列表（按文件名排序）。"""
    if not os.path.isdir(folder):
        return []
    found = []
    for root, _dirs, files in os.walk(folder):
        for name in files:
            if name.lower().endswith(AUDIO_EXTS):
                found.append(os.path.join(root, name))
    found.sort(key=lambda p: os.path.basename(p).lower())
    return found


def pretty_name(path: str) -> str:
    """把文件名变成适合显示的歌名：去扩展名、去开头的序号。

    只剥离开头连续的「数字 + 分隔符」，不会误伤名字中间的内容。
        01_test_tone.mp3    -> test_tone
        01. 夏日限定.mp3     -> 夏日限定
        01 - 夏日 - 限定.mp3 -> 夏日 - 限定
    """
    name = os.path.splitext(os.path.basename(path))[0].strip()
    # 匹配开头的序号段：可选分隔符 + 数字 + 分隔符，最多剥两次（如 01-02.曲名）
    cleaned = re.sub(r"^(\d+\s*[-._、]\s*)+", "", name).strip()
    # 纯数字文件名（如 01.mp3）就别剥成空了
    return cleaned or name


class MusicPlayer(QObject):
    """音乐播放器。所有异常通过 error 信号抛出，不阻塞 UI。"""

    track_changed = pyqtSignal(str)      # 当前歌名
    position_changed = pyqtSignal(int, int)  # 当前位置ms, 总时长ms
    state_changed = pyqtSignal(bool)     # True 表示正在播放
    error = pyqtSignal(str)

    def __init__(self, folder: str = MUSIC_DIR, parent=None):
        super().__init__(parent)
        self.folder = folder
        self.playlist = scan_music(folder)
        self.index = -1
        self.shuffle = False
        self.loop_one = False

        self.audio = QAudioOutput()
        self.audio.setVolume(0.65)

        self.player = QMediaPlayer()
        self.player.setAudioOutput(self.audio)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(
            lambda d: self.position_changed.emit(self.player.position(), d)
        )
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.errorOccurred.connect(self._on_error)

        # 后端健康检查：没起来的时候 QMediaPlayer 一出生就带 ResourceError，
        # 而那一刻 errorOccurred 还没连上，用户只会觉得「点了没反应」。
        self.backend_ok = self.player.error() == QMediaPlayer.Error.NoError
        trace.log("music backend: " + ("就绪" if self.backend_ok
                                       else "不可用（%s）" % self.player.errorString()))

    # ---------- 列表 ----------
    def refresh(self, folder: str | None = None):
        """重新扫描目录。返回曲目数。

        重扫时按文件路径记住正在播的那首，避免丢歌进目录后 index 漂移
        导致「下一首」跳错。
        """
        if folder:
            self.folder = folder
        cur = self.playlist[self.index] if 0 <= self.index < len(self.playlist) else None
        self.playlist = scan_music(self.folder)
        if cur is not None and cur in self.playlist:
            self.index = self.playlist.index(cur)
        elif self.index >= len(self.playlist):
            self.index = -1
        return len(self.playlist)

    def track_names(self):
        return [pretty_name(p) for p in self.playlist]

    # ---------- 播放控制 ----------
    def play_index(self, i: int):
        if not self.playlist:
            self.error.emit("music/ 里还没有音乐文件哦")
            return
        if not self.backend_ok:
            self.error.emit("音频后端没起来，放不了歌（见 _trace.log）")
            return
        self.index = i % len(self.playlist)
        name = pretty_name(self.playlist[self.index])
        trace.log(f"music play: {name}")
        self.player.setSource(QUrl.fromLocalFile(self.playlist[self.index]))
        self.player.play()
        self.track_changed.emit(name)
        # 1.2 秒还没进入播放态 = 解码失败/格式不支持，用气泡说清楚，别静默
        QTimer.singleShot(1200, lambda idx=self.index: self._verify_playing(idx))

    def _verify_playing(self, idx: int):
        if idx != self.index:
            return
        if self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self.error.emit("放不出声：%s" % (self.player.errorString()
                                              or "这个格式可能不支持"))

    def play_pause(self):
        if not self.playlist:
            self.error.emit("music/ 里还没有音乐文件哦")
            return
        state = self.player.playbackState()
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.track_changed.emit("暂停：" + self._current_name())
        else:
            if self.index < 0:
                self.play_index(0)
            else:
                self.player.play()
                self.track_changed.emit("继续播放")

    def next(self):
        if not self.playlist:
            return
        if self.shuffle:
            self.play_index(random.randrange(len(self.playlist)))
        else:
            self.play_index(self.index + 1)

    def prev(self):
        if not self.playlist:
            return
        self.play_index(self.index - 1)

    def stop(self):
        self.player.stop()

    def set_volume(self, v: float):
        self.audio.setVolume(max(0.0, min(1.0, v)))

    @property
    def is_playing(self) -> bool:
        return (
            self.player.playbackState()
            == QMediaPlayer.PlaybackState.PlayingState
        )

    def _current_name(self):
        if 0 <= self.index < len(self.playlist):
            return pretty_name(self.playlist[self.index])
        return ""

    # ---------- Qt 回调 ----------
    def _on_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self.loop_one and self.index >= 0:
                self.player.setPosition(0)
                self.player.play()
            else:
                self.next()

    def _on_position(self, pos):
        self.position_changed.emit(pos, self.player.duration())

    def _on_state(self, state):
        self.state_changed.emit(
            state == QMediaPlayer.PlaybackState.PlayingState
        )

    def _on_error(self, _err, msg):
        if msg:
            self.error.emit(f"播放失败：{msg}")


if __name__ == "__main__":
    print("音乐目录：", MUSIC_DIR)
    files = scan_music(MUSIC_DIR)
    print(f"找到 {len(files)} 首：")
    for f in files:
        print("   ", pretty_name(f), "->", f)
