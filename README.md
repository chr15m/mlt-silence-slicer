A simple Python script to generate an .mlt file for shotcut by slicing up a video at audio thresholds.

Use it like this:
`./smart_silence_slicer.py video-1.mkv video-2.mp4 --output video.mlt`

You can use the `--delete-silence` option to drop all the segments containing only silence.

Run with `--help` to get help.

Then open `video.mlt` in shotcut.

Depends on `ffmpeg` on your path.
