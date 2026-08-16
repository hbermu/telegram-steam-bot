"""ffmpeg wrappers.

Knows about frames, widths and byte budgets. Knows nothing about Telegram or
about where captures live.

There is no ImageMagick on SteamOS, so every image operation goes through
ffmpeg, which ships with the OS.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import config


class EncodeError(RuntimeError):
    pass


def _font() -> str | None:
    """A font file for drawtext. SteamOS ships Noto Sans and nothing else."""
    try:
        proc = subprocess.run(
            ["fc-match", "-f", "%{file}", "sans"], capture_output=True, text=True
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except FileNotFoundError:
        pass
    fallback = Path("/usr/share/fonts/noto/NotoSans-Regular.ttf")
    return str(fallback) if fallback.exists() else None


def _escape_drawtext(text: str) -> str:
    for char in ("\\", ":", "'", "%"):
        text = text.replace(char, "\\" + char)
    return text


@dataclass
class Clip:
    path: Path
    width: int
    frames: int
    every: int
    size: int
    duration: float = 0.0
    height: int = 0


# The GIF builder predates the video one and callers still speak of a Gif.
Gif = Clip


def _run(args: list[str]) -> None:
    # nice, so a request cannot stutter a game running on the same console.
    proc = subprocess.run(
        ["nice", "-n", "19", *args], capture_output=True, text=True
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise EncodeError(" / ".join(tail) or f"ffmpeg exited {proc.returncode}")


def probe_size(path: Path) -> tuple[int, int]:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path),
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise EncodeError(f"ffprobe failed on {path.name}")
    # ffprobe rejects -of csv=p=0:s=' ' — keep the default comma and split here.
    width, height = proc.stdout.strip().split(",")[:2]
    return int(width), int(height)


def _stage(
    frames: list[Path], work: Path, labels: list[str] | None = None
) -> tuple[int, int]:
    """Lay the frames out as 00001.png … so ffmpeg never sees a real filename.

    Farm names carry apostrophes and non-ASCII, and captures live in directories
    named after them; sequencing them into a scratch directory sidesteps every
    quoting and concat-escaping problem at once.

    Returns the canvas the frames were normalised onto.
    """
    sizes = [probe_size(f) for f in frames]
    max_w = max(s[0] for s in sizes) + max(s[0] for s in sizes) % 2
    max_h = max(s[1] for s in sizes) + max(s[1] for s in sizes) % 2
    uniform = len(set(sizes)) == 1
    font = _font() if labels else None

    for index, frame in enumerate(frames, start=1):
        target = work / f"{index:05d}.png"
        if uniform and not labels:
            target.symlink_to(frame)
            continue
        # Changing ZoomLevel mid-playthrough leaves mixed sizes, which would
        # abort the encode; pad every frame onto the largest canvas instead.
        # Per-frame text cannot be done in one pass either, so both cases cost
        # one ffmpeg invocation per frame.
        chain = (
            f"scale={max_w}:{max_h}:force_original_aspect_ratio=decrease,"
            f"pad={max_w}:{max_h}:(ow-iw)/2:(oh-ih)/2"
        )
        if labels and font:
            size = max(16, max_h // 28)
            chain += (
                f",drawtext=text='{_escape_drawtext(labels[index - 1])}'"
                f":fontfile={font}:fontcolor=white:fontsize={size}"
                f":borderw={max(2, size // 12)}:bordercolor=black@0.8"
                f":x={size}:y={size}"
            )
        _run([
            "ffmpeg", "-loglevel", "error", "-y", "-i", str(frame),
            "-vf", chain, str(target),
        ])
    return max_w, max_h


def build_gif(
    frames: list[Path],
    out: Path,
    fps: int = config.GIF_FPS,
    budget: int = config.UPLOAD_BUDGET,
    widths: list[int] | None = None,
    every: int | None = None,
    labels: list[str] | None = None,
) -> Gif:
    """Encode a real GIF that fits inside ``budget``.

    Long playthroughs need both a width cap and frame decimation: a full in-game
    year at native resolution runs to roughly 90 MB, well past what the Bot API
    accepts. Encode, measure, and step the width down if it overflows.
    """
    if not frames:
        raise EncodeError("no frames")

    if every is None:
        every = max(1, math.ceil(len(frames) / config.GIF_MAX_FRAMES))
    selected = frames[::every]
    picked_labels = labels[::every] if labels else None

    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=config.WORK_DIR, prefix="gif-"))
    try:
        canvas_w, _ = _stage(selected, work, picked_labels)
        pattern = str(work / "%05d.png")
        palette = str(work / "palette.png")
        last_size = 0

        for width in widths or config.GIF_WIDTHS:
            # Never upscale: a narrow capture stays its own size.
            target = min(width, canvas_w)
            target -= target % 2
            scale = f"scale={target}:-2:flags=lanczos"
            _run([
                "ffmpeg", "-loglevel", "error", "-y", "-framerate", str(fps),
                "-i", pattern, "-vf", f"{scale},palettegen=stats_mode=diff",
                palette,
            ])
            _run([
                "ffmpeg", "-loglevel", "error", "-y", "-framerate", str(fps),
                "-i", pattern, "-i", palette, "-lavfi",
                f"{scale}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3",
                "-loop", "0", str(out),
            ])
            last_size = out.stat().st_size
            if last_size <= budget:
                return Gif(out, target, len(selected), every, last_size)

        raise EncodeError(
            f"even at {min(widths or config.GIF_WIDTHS)}px it exceeds the budget "
            f"({last_size / 1e6:.0f} MB for {len(selected)} frames)"
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _x264(pattern: str, out: Path, fps: int, scale: str, rate: list[str]) -> None:
    _run([
        "ffmpeg", "-loglevel", "error", "-y", "-framerate", str(fps),
        "-i", pattern, "-vf", scale, "-c:v", "libx264", *rate,
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out),
    ])


def _video_widths(width: int | None, canvas_w: int, budget: int | None) -> list[int]:
    if width:
        return [width]
    if budget is None:
        # The CLI writes to disk, where nothing is competing for megabytes.
        return [canvas_w]
    # Native first, and not only for quality: rescaling pixel art costs bits, so
    # the untouched canvas has measured *smaller* than a downscale of itself.
    return [canvas_w] + [w for w in config.VIDEO_WIDTHS if w < canvas_w]


def build_mp4(
    frames: list[Path],
    out: Path,
    fps: int = config.GIF_FPS,
    width: int | None = None,
    every: int = 1,
    labels: list[str] | None = None,
    budget: int | None = None,
) -> Clip:
    """H.264 from the same frames, squeezed under ``budget`` when given.

    This is what long spans go out as. Consecutive farm days differ by a few
    tilled squares, and inter-frame prediction charges almost nothing for that,
    so a whole playthrough keeps every single day where a GIF of the same span
    would have to drop two days in three.

    ``budget`` of None means "encode at quality and do not care", which is what
    the CLI wants when writing to disk.
    """
    if not frames:
        raise EncodeError("no frames")
    selected = frames[::every]
    picked_labels = labels[::every] if labels else None

    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=config.WORK_DIR, prefix="mp4-"))
    try:
        canvas_w, _ = _stage(selected, work, picked_labels)
        pattern = str(work / "%05d.png")
        duration = len(selected) / float(fps)
        size = 0

        for candidate in _video_widths(width, canvas_w, budget):
            target = min(candidate, canvas_w)
            target -= target % 2
            scale = f"scale={target}:-2:flags=lanczos"

            _x264(pattern, out, fps, scale, ["-crf", str(config.VIDEO_CRF)])
            size = out.stat().st_size
            if budget is None or size <= budget:
                return _clip(out, selected, every, duration)

            # CRF targets quality and ignores file size by design. Once it has
            # overshot, asking for the bitrate the budget actually allows is
            # exact, where guessing a higher CRF is another shot in the dark.
            bitrate = int(budget * 8 * 0.90 / duration)
            _x264(pattern, out, fps, scale, [
                "-b:v", str(bitrate),
                "-maxrate", str(int(bitrate * 1.3)),
                "-bufsize", str(bitrate * 2),
            ])
            size = out.stat().st_size
            if size <= budget:
                return _clip(out, selected, every, duration)

        raise EncodeError(
            f"even at {min(_video_widths(width, canvas_w, budget))}px the video "
            f"exceeds the budget ({size / 1e6:.0f} MB for {len(selected)} frames)"
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _clip(out: Path, selected: list[Path], every: int, duration: float) -> Clip:
    width, height = probe_size(out)
    return Clip(
        out, width, len(selected), every, out.stat().st_size, duration, height
    )
