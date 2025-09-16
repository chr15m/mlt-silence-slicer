#!/usr/bin/env python3

import sys
import subprocess
import re
import os
import json
import hashlib
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom

def generate_file_hash(filepath):
    """Generate a hash for a file."""
    hasher = hashlib.md5()
    with open(filepath, 'rb') as f:
        buf = f.read()
        hasher.update(buf)
    return hasher.hexdigest()

def detect_silences(input_video, onset_threshold="-40dB", offset_threshold="-50dB", duration=0.25):
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
    offset_result = subprocess.run(offset_command, capture_output=True, text=True, check=False)
    offset_times = [float(t) for t in re.findall(r'silence_start: (\d+\.?\d*)', offset_result.stderr)]

    # Get sound onsets (silence ends) with the louder threshold
    onset_command = [
        'ffmpeg', '-i', input_video,
        '-af', f'silencedetect=noise={onset_threshold}:d={duration}',
        '-f', 'null', '-'
    ]
    onset_result = subprocess.run(onset_command, capture_output=True, text=True, check=False)
    onset_times = [float(t) for t in re.findall(r'silence_end: (\d+\.?\d*)', onset_result.stderr)]

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

def create_mlt_file(input_video, silences, video_info, min_segment_duration=0.1):
    """
    Create an MLT file with clips split at silence boundaries.
    """
    video_filename = os.path.basename(input_video)
    mlt_path = os.path.splitext(input_video)[0] + '.mlt'
    
    duration_secs = video_info['duration']
    
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

    root = Element('mlt', {
        'LC_NUMERIC': 'C', 
        'version': '7.4.0', 
        'title': 'Shotcut version 22.01.30',
        'producer': 'main_bin'
    })
    
    SubElement(root, 'profile', {
        'description': 'automatic',
        'width': str(video_info['width']),
        'height': str(video_info['height']),
        'progressive': '1',
        'sample_aspect_num': '1', 'sample_aspect_den': '1',
        'display_aspect_num': '16', 'display_aspect_den': '9',
        'frame_rate_num': str(video_info['frame_rate_num']), 
        'frame_rate_den': str(video_info['frame_rate_den']),
        'colorspace': '709'
    })
    
    main_bin = SubElement(root, 'playlist', {'id': 'main_bin'})
    SubElement(main_bin, 'property', {'name': 'xml_retain'}).text = '1'

    black_producer = SubElement(root, 'producer', {'id': 'black', 'in': '00:00:00.000', 'out': format_time(duration_secs)})
    SubElement(black_producer, 'property', {'name': 'length'}).text = format_time(duration_secs)
    SubElement(black_producer, 'property', {'name': 'eof'}).text = 'pause'
    SubElement(black_producer, 'property', {'name': 'resource'}).text = '0'
    SubElement(black_producer, 'property', {'name': 'aspect_ratio'}).text = '1'
    SubElement(black_producer, 'property', {'name': 'mlt_service'}).text = 'color'
    SubElement(black_producer, 'property', {'name': 'mlt_image_format'}).text = 'rgba'
    SubElement(black_producer, 'property', {'name': 'set.test_audio'}).text = '0'

    background = SubElement(root, 'playlist', {'id': 'background'})
    SubElement(background, 'entry', {'producer': 'black', 'in': '00:00:00.000', 'out': format_time(duration_secs)})

    video_hash = generate_file_hash(input_video)

    for i, (start, end) in enumerate(segments):
        chain = SubElement(root, 'chain', {'id': f'chain{i}', 'out': format_time(duration_secs)})
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
    
    frame_duration = video_info['frame_rate_den'] / video_info['frame_rate_num']
    for i, (start, end) in enumerate(segments):
        is_last = (i == len(segments) - 1)
        out_time = end if is_last else end - frame_duration
        if out_time < start:
            out_time = start
        SubElement(playlist, 'entry', {'producer': f'chain{i}', 'in': format_time(start), 'out': format_time(out_time)})

    tractor = SubElement(root, 'tractor', {
        'id': 'tractor0', 
        'title': 'Shotcut version 22.01.30',
        'in': '00:00:00.000', 
        'out': format_time(duration_secs)
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
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <video_file> [onset_db] [offset_db] [min_duration_ms]")
        print("  onset_db:  Threshold for sound onset (silence end) (e.g., -40, default: -40).")
        print("  offset_db: Threshold for sound offset (silence start) (e.g., -50, default: -50).")
        print("  min_duration_ms: Minimum segment duration in ms (e.g., 100, default: 100).")
        sys.exit(1)
        
    input_video = sys.argv[1]
    onset_threshold = sys.argv[2] if len(sys.argv) > 2 else "-40"
    offset_threshold = sys.argv[3] if len(sys.argv) > 3 else "-50"
    min_duration_ms = int(sys.argv[4]) if len(sys.argv) > 4 else 100
    min_segment_duration = min_duration_ms / 1000.0
    
    if not os.path.exists(input_video):
        print(f"Error: File not found at {input_video}")
        sys.exit(1)
        
    print("Detecting silences...")
    silences = detect_silences(input_video, offset_threshold=f"{offset_threshold}dB", onset_threshold=f"{onset_threshold}dB")
    print(f"Found {len(silences)} silence(s).")
    
    print("Getting video info...")
    video_info = get_video_info(input_video)
    
    print("Creating MLT file...")
    create_mlt_file(input_video, silences, video_info, min_segment_duration=min_segment_duration)

if __name__ == '__main__':
    main()
