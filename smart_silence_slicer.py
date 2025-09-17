#!/usr/bin/env python3

import sys
import subprocess
import re
import os
import json
import hashlib
import threading
import queue
import argparse
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom

def _run_ffmpeg_and_get_times(command, regex_pattern, label):
    """
    Run an ffmpeg command, parse its stderr for timestamps, and show progress.
    """
    def reader_thread(pipe, q):
        try:
            for line in iter(pipe.readline, ''):
                q.put(line)
        finally:
            pipe.close()

    times = []
    spinner = ['-', '\\', '|', '/']
    spinner_idx = 0
    
    process = subprocess.Popen(command, stderr=subprocess.PIPE, text=True, universal_newlines=True)
    q = queue.Queue()
    thread = threading.Thread(target=reader_thread, args=(process.stderr, q))
    thread.start()

    while process.poll() is None or not q.empty():
        try:
            line = q.get(timeout=0.1)
            match = re.search(regex_pattern, line)
            if match:
                time_val = float(match.group(1))
                times.append(time_val)
                sys.stdout.write(f"\r{label}: Found {len(times)} ({time_val:.3f}s)          ")
                sys.stdout.flush()
        except queue.Empty:
            if process.poll() is None: # Only spin if process is still running
                sys.stdout.write(f"\r{label}: {spinner[spinner_idx % 4]}          ")
                sys.stdout.flush()
                spinner_idx += 1
    
    thread.join()
    
    sys.stdout.write(f"\r{label}: Found {len(times)} total.                          \n")
    sys.stdout.flush()
    
    return sorted(times)

def generate_file_hash(filepath):
    """Generate a hash for a file."""
    hasher = hashlib.md5()
    with open(filepath, 'rb') as f:
        buf = f.read()
        hasher.update(buf)
    return hasher.hexdigest()

def detect_silences(input_video, onset_threshold="-60dB", offset_threshold="-60dB", duration=0.25):
    """
    Detect silences using separate thresholds for sound offset and onset.
    Returns a list of (offset, onset) tuples for silent sections.
    """
    # Get sound offsets (silence starts) with the sensitive threshold
    offset_command = [
        'ffmpeg', '-i', input_video,
        '-af', f'silencedetect=noise={offset_threshold}:d={duration}',
        '-f', 'null', '-'
    ]
    offset_times = _run_ffmpeg_and_get_times(offset_command, r'silence_start: (\d+\.?\d*)', "Detecting offsets")

    # Get sound onsets (silence ends) with the louder threshold
    onset_command = [
        'ffmpeg', '-i', input_video,
        '-af', f'silencedetect=noise={onset_threshold}:d={duration}',
        '-f', 'null', '-'
    ]
    onset_times = _run_ffmpeg_and_get_times(onset_command, r'silence_end: (\d+\.?\d*)', "Detecting onsets")

    # Pair up the offset and onset times
    silences = []
    offsets_iter = iter(offset_times)
    onsets_iter = iter(onset_times)
    
    current_offset = next(offsets_iter, None)
    current_onset = next(onsets_iter, None)
    
    while current_offset is not None and current_onset is not None:
        # Find an onset time that is after the current offset time
        while current_onset is not None and current_onset <= current_offset:
            current_onset = next(onsets_iter, None)

        if current_onset is None:
            break  # No more valid onset times

        # We have a valid pair
        silences.append((current_offset, current_onset))

        # Find the next offset time that is after the current onset time
        while current_offset is not None and current_offset <= current_onset:
            current_offset = next(offsets_iter, None)
            
    return silences

def get_video_info(input_video):
    """
    Get video duration, frame rate, width, and height using ffprobe.
    """
    command = [
        'ffprobe',
        '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height,r_frame_rate:format=duration',
        '-of', 'json',
        input_video
    ]
    
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    info = data['streams'][0]
    
    duration_val = float(data['format']['duration'])
    frame_rate_str = info['r_frame_rate']
    num, den = map(int, frame_rate_str.split('/'))
    
    return {
        'duration': duration_val,
        'frame_rate_num': num,
        'frame_rate_den': den,
        'width': int(info['width']),
        'height': int(info['height'])
    }

def format_time(seconds):
    """
    Format seconds into HH:MM:SS.ms string for MLT.
    """
    total_millis = int(round(seconds * 1000))
    millis = total_millis % 1000
    total_seconds = total_millis // 1000
    
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    
    return f"{h:02d}:{m:02d}:{s:02d}.{millis:03d}"

def calculate_segments(duration_secs, silences, min_segment_duration=0.1):
    """
    Calculate video segments based on silence points.
    """
    # Create split points from silences
    split_points = {0.0, duration_secs}
    for start, end in silences:
        split_points.add(start)
        split_points.add(end)
    
    sorted_points = sorted(list(split_points))
    
    # Filter out points that create segments shorter than min_segment_duration
    filtered_points = [sorted_points[0]]
    for i in range(1, len(sorted_points)):
        if sorted_points[i] - filtered_points[-1] >= min_segment_duration:
            filtered_points.append(sorted_points[i])
    
    if len(filtered_points) > 1 and duration_secs - filtered_points[-1] < min_segment_duration:
        filtered_points.pop()
        
    if filtered_points[-1] != duration_secs:
        filtered_points.append(duration_secs)

    # Create segments from the filtered points
    segments = []
    for i in range(len(filtered_points) - 1):
        start, end = filtered_points[i], filtered_points[i+1]
        if end > start:
            segments.append((start, end))
    return segments

def create_mlt_file(video_data, mlt_path):
    """
    Create an MLT file with clips from multiple videos.
    """
    if not video_data:
        return

    first_video_info = video_data[0][2]
    total_duration_secs = sum(end - start for _, segments, _ in video_data for start, end in segments)

    root = Element('mlt', {
        'LC_NUMERIC': 'C', 
        'version': '7.4.0', 
        'title': 'Shotcut version 22.01.30',
        'producer': 'main_bin'
    })
    
    SubElement(root, 'profile', {
        'description': 'automatic',
        'width': str(first_video_info['width']),
        'height': str(first_video_info['height']),
        'progressive': '1',
        'sample_aspect_num': '1', 'sample_aspect_den': '1',
        'display_aspect_num': '16', 'display_aspect_den': '9',
        'frame_rate_num': str(first_video_info['frame_rate_num']), 
        'frame_rate_den': str(first_video_info['frame_rate_den']),
        'colorspace': '709'
    })
    
    main_bin = SubElement(root, 'playlist', {'id': 'main_bin'})
    SubElement(main_bin, 'property', {'name': 'xml_retain'}).text = '1'

    black_producer = SubElement(root, 'producer', {'id': 'black', 'in': '00:00:00.000', 'out': format_time(total_duration_secs)})
    SubElement(black_producer, 'property', {'name': 'length'}).text = format_time(total_duration_secs)
    SubElement(black_producer, 'property', {'name': 'eof'}).text = 'pause'
    SubElement(black_producer, 'property', {'name': 'resource'}).text = '0'
    SubElement(black_producer, 'property', {'name': 'aspect_ratio'}).text = '1'
    SubElement(black_producer, 'property', {'name': 'mlt_service'}).text = 'color'
    SubElement(black_producer, 'property', {'name': 'mlt_image_format'}).text = 'rgba'
    SubElement(black_producer, 'property', {'name': 'set.test_audio'}).text = '0'

    background = SubElement(root, 'playlist', {'id': 'background'})
    SubElement(background, 'entry', {'producer': 'black', 'in': '00:00:00.000', 'out': format_time(total_duration_secs)})

    # Create a chain for each video file first
    for chain_idx, (input_video, segments, video_info) in enumerate(video_data):
        video_filename = os.path.basename(input_video)
        video_hash = generate_file_hash(input_video)
        duration_secs = video_info['duration']

        chain = SubElement(root, 'chain', {'id': f'chain{chain_idx}', 'out': format_time(duration_secs)})
        SubElement(chain, 'property', {'name': 'length'}).text = format_time(duration_secs)
        SubElement(chain, 'property', {'name': 'eof'}).text = 'pause'
        SubElement(chain, 'property', {'name': 'resource'}).text = video_filename
        SubElement(chain, 'property', {'name': 'mlt_service'}).text = 'avformat-novalidate'
        SubElement(chain, 'property', {'name': 'seekable'}).text = '1'
        SubElement(chain, 'property', {'name': 'audio_index'}).text = '1'
        SubElement(chain, 'property', {'name': 'video_index'}).text = '0'
        SubElement(chain, 'property', {'name': 'mute_on_pause'}).text = '0'
        SubElement(chain, 'property', {'name': 'shotcut:hash'}).text = video_hash
        SubElement(chain, 'property', {'name': 'ignore_points'}).text = '0'
        SubElement(chain, 'property', {'name': 'shotcut:caption'}).text = video_filename
        SubElement(chain, 'property', {'name': 'xml'}).text = 'was here'

    playlist = SubElement(root, 'playlist', {'id': 'playlist0', 'title': 'V1'})
    SubElement(playlist, 'property', {'name': 'shotcut:video'}).text = '1'
    SubElement(playlist, 'property', {'name': 'shotcut:name'}).text = 'V1'
    
    # Then, create the playlist entries that refer to the chains
    for chain_idx, (input_video, segments, video_info) in enumerate(video_data):
        frame_duration = video_info['frame_rate_den'] / video_info['frame_rate_num']
        for i, (start, end) in enumerate(segments):
            is_last = (i == len(segments) - 1)
            out_time = end if is_last else end - frame_duration
            if out_time < start:
                out_time = start
            SubElement(playlist, 'entry', {'producer': f'chain{chain_idx}', 'in': format_time(start), 'out': format_time(out_time)})

    tractor = SubElement(root, 'tractor', {
        'id': 'tractor0', 
        'title': 'Shotcut version 22.01.30',
        'in': '00:00:00.000', 
        'out': format_time(total_duration_secs)
    })
    SubElement(tractor, 'property', {'name': 'shotcut'}).text = '1'
    SubElement(tractor, 'property', {'name': 'shotcut:projectAudioChannels'}).text = '2'
    SubElement(tractor, 'property', {'name': 'shotcut:projectFolder'}).text = '0'
    SubElement(tractor, 'track', {'producer': 'background'})
    SubElement(tractor, 'track', {'producer': 'playlist0'})

    transition0 = SubElement(tractor, 'transition', {'id': 'transition0'})
    SubElement(transition0, 'property', {'name': 'a_track'}).text = '0'
    SubElement(transition0, 'property', {'name': 'b_track'}).text = '1'
    SubElement(transition0, 'property', {'name': 'mlt_service'}).text = 'mix'
    SubElement(transition0, 'property', {'name': 'always_active'}).text = '1'
    SubElement(transition0, 'property', {'name': 'sum'}).text = '1'

    transition1 = SubElement(tractor, 'transition', {'id': 'transition1'})
    SubElement(transition1, 'property', {'name': 'a_track'}).text = '0'
    SubElement(transition1, 'property', {'name': 'b_track'}).text = '1'
    SubElement(transition1, 'property', {'name': 'version'}).text = '0.9'
    SubElement(transition1, 'property', {'name': 'mlt_service'}).text = 'frei0r.cairoblend'
    SubElement(transition1, 'property', {'name': 'threads'}).text = '0'
    SubElement(transition1, 'property', {'name': 'disable'}).text = '1'
    
    xml_str = tostring(root, 'utf-8')
    pretty_xml_str = minidom.parseString(xml_str).toprettyxml(indent="  ")
    
    with open(mlt_path, 'w') as f:
        f.write(pretty_xml_str)
        
    print(f"Generated MLT file: {mlt_path}")

def main():
    parser = argparse.ArgumentParser(description="Generate an .mlt file for shotcut by slicing up videos at audio thresholds.")
    parser.add_argument('video_files', nargs='+', help="One or more video files to process.")
    parser.add_argument('-o', '--output', help="Output MLT file path. Defaults to the first video's name with .mlt extension.")
    parser.add_argument('--onset-db', type=int, default=-60, help="Threshold for sound onset (silence end) in dB (default: -60).")
    parser.add_argument('--offset-db', type=int, default=-60, help="Threshold for sound offset (silence start) in dB (default: -60).")
    parser.add_argument('--min-duration-ms', type=int, default=100, help="Minimum segment duration in ms (default: 100).")
    
    args = parser.parse_args()

    min_segment_duration = args.min_duration_ms / 1000.0
    
    video_data = []
    for input_video in args.video_files:
        if not os.path.exists(input_video):
            print(f"Error: File not found at {input_video}")
            continue
        
        print(f"Processing {input_video}...")
        silences = detect_silences(input_video, offset_threshold=f"{args.offset_db}dB", onset_threshold=f"{args.onset_db}dB")
        print(f"Found {len(silences)} silence(s).")
        
        print("Getting video info...")
        video_info = get_video_info(input_video)
        
        segments = calculate_segments(video_info['duration'], silences, min_segment_duration)
        video_data.append((input_video, segments, video_info))

    if not video_data:
        print("No valid video files processed.")
        sys.exit(1)

    output_mlt_path = args.output
    if not output_mlt_path:
        output_mlt_path = os.path.splitext(args.video_files[0])[0] + '.mlt'

    print("Creating MLT file...")
    create_mlt_file(video_data, output_mlt_path)

if __name__ == '__main__':
    main()
