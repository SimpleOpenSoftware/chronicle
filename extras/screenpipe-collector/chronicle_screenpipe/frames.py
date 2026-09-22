"""Read the requested capture, including ScreenPipe's compacted video storage.

Privacy decisions must never use the thumbnail API's nearby-frame substitution.
The SQLite frame identity and stored video offset remain authoritative.
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from collections import OrderedDict
from pathlib import Path
from threading import Lock


class _VideoFrameCache:
    """Reuse decoded bytes only for an exact current video and frame offset."""

    def __init__(self):
        self.frames = OrderedDict()
        self.bytes = 0
        self.limit = 16 * 1024 * 1024
        self.lock = Lock()

    @staticmethod
    def fingerprint(path):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").digest()

    def read(self, path, offset):
        # Serialize identical reads before decoding. This lock is used only by
        # frame retrieval, never by capture, scheduling or collector heartbeat.
        with self.lock:
            checksum = self.fingerprint(path)
            key = (str(path), offset, checksum)
            if key in self.frames:
                self.frames.move_to_end(key)
                return self.frames[key]
            image = _extract_video_frame(path, offset)
            if self.fingerprint(path) != checksum:
                raise RuntimeError("Capture changed while decoding")
            if len(image) <= self.limit:
                self.frames[key] = image
                self.bytes += len(image)
                while self.bytes > self.limit or len(self.frames) > 32:
                    _, old = self.frames.popitem(last=False)
                    self.bytes -= len(old)
            return image


_VIDEO_FRAMES = _VideoFrameCache()


def read_frame(screenpipe_dir: Path, frame_id: int) -> bytes:
    root = screenpipe_dir.resolve()
    connection = sqlite3.connect(
        f"file:{root / 'db.sqlite'}?mode=ro", uri=True, timeout=5
    )
    try:
        row = connection.execute(
            "SELECT f.snapshot_path, f.name, f.offset_index, v.file_path "
            "FROM frames f LEFT JOIN video_chunks v ON v.id=f.video_chunk_id WHERE f.id=?",
            (frame_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise FileNotFoundError("Requested capture is unavailable")

    def local(value):
        if not value:
            return None
        path = Path(value).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Capture path is outside the recorder directory")
        return path if path.is_file() else None

    # These are the recorder's two supported storage representations, not a
    # substitution with a different frame when an image has been compacted.
    snapshot = local(row[0]) or local(row[1])
    if snapshot:
        return snapshot.read_bytes()
    video = local(row[3])
    if video is None or row[2] is None or row[2] < 0:
        raise FileNotFoundError("Requested capture is unavailable")
    return _VIDEO_FRAMES.read(video, int(row[2]))


def _extract_video_frame(video: Path, offset: int) -> bytes:
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-threads",
            "1",
            "-i",
            str(video),
            "-vf",
            f"select='eq(n,{offset})'",
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-compression_level",
            "1",
            "-",
        ],
        capture_output=True,
        timeout=30,
    )
    if result.returncode or not result.stdout:
        raise FileNotFoundError("Requested video frame is unavailable")
    return result.stdout
