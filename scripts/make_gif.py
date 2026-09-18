"""Assemble the filter frames into one animated GIF.

Pillow rather than ffmpeg: it is already a dependency of the spreadsheet
reader, and a GIF is the one animated format a README renders inline on GitHub
without a click.

Frames are downscaled before quantising. A 3000-pixel-wide screenshot at 2x
device scale makes a beautiful, unusable 40 MB GIF; the point of this file is
that someone scrolling a README sees the filters move.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

#: Wide enough to read a table header, narrow enough to stay a few hundred KB.
WIDTH = 1100

#: Long enough to register a change, short enough that six frames are not a
#: wait. The last frame holds, so the loop does not appear to stutter.
FRAME_MS = 1400
HOLD_MS = 2600


def build(frames: list[Path], target: Path, width: int = WIDTH) -> Path:
    images = []
    for path in frames:
        image = Image.open(path).convert("RGB")
        if image.width > width:
            height = round(image.height * width / image.width)
            image = image.resize((width, height), Image.LANCZOS)
        # Adaptive palette: the page is mostly flat colour, and the default
        # web palette turns its greys into banding.
        images.append(image.quantize(colors=128, method=Image.MEDIANCUT))

    durations = [FRAME_MS] * len(images)
    durations[-1] = HOLD_MS
    images[0].save(
        target,
        save_all=True,
        append_images=images[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--frames", type=Path, default=Path("docs/img/_frames"))
    parser.add_argument("--out", type=Path, default=Path("docs/img/filters.gif"))
    parser.add_argument("--width", type=int, default=WIDTH)
    args = parser.parse_args()

    frames = sorted(args.frames.glob("*.png"))
    if not frames:
        print(f"no frames in {args.frames}")
        return 1
    target = build(frames, args.out, args.width)
    size = target.stat().st_size
    print(f"{len(frames)} frames -> {target} ({size / 1e6:.2f} MB)")
    if size > 6_000_000:
        print("  warning: over 6 MB; GitHub will render it but slowly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
