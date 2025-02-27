#!/usr/bin/env python3

import sys
import os
import subprocess
import re
import xml.etree.ElementTree as ET
import xml.dom.minidom as minidom
import hashlib

def generate_file_hash(filepath):
    """Generate MD5 hash of file according to Shotcut rules"""
    file_size = os.path.getsize(filepath)
    with open(filepath, 'rb') as f:
        if file_size <= 2 * 1024 * 1024:  # If file is <= 2MB
            return hashlib.md5(f.read()).hexdigest()
        else:
            # Read first MB
            first_mb = f.read(1024 * 1024)
            # Seek to last MB
            f.seek(-1024 * 1024, 2)
            last_mb = f.read()
            return hashlib.md5(first_mb + last_mb).hexdigest()

def usage():
    print("Usage: python smart-silence-slicer.py input_video.mp4 [silence_threshold] [silence_duration] [min_gap]")
    print("Example: python smart-silence-slicer.py myvideo.mp4 -30dB 0.25 0.2")
    print("  silence_threshold: Default -30dB")
    print("  silence_duration: Default 0.25 seconds")
    print("  min_gap: Minimum gap between slice points in seconds (Default 0.2)")
    sys.exit(1)

def detect_silences(input_video, threshold="-30dB", duration=0.25):
    """Detect silence periods using ffmpeg"""
    cmd = [
        "ffmpeg", "-i", input_video, 
        "-af", f"silencedetect=noise={threshold}:d={duration}", 
        "-f", "null", "-"
    ]
    
    result = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
    output = result.stderr
    
    # Extract silence start and end times
    silence_starts = re.findall(r'silence_start: (\d+\.\d+)', output)
    silence_ends = re.findall(r'silence_end: (\d+\.\d+)', output)
    
    # Convert to floats
    silence_starts = [float(t) for t in silence_starts]
    silence_ends = [float(t) for t in silence_ends]
    
    return list(zip(silence_starts, silence_ends))

def get_video_info(input_video):
    """Get video duration and frame rate"""
    # Get duration
    cmd = [
        "ffprobe", "-v", "error", 
        "-show_entries", "format=duration", 
        "-of", "default=noprint_wrappers=1:nokey=1", 
        input_video
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    duration = float(result.stdout.strip())
    
    # Get frame rate
    cmd = [
        "ffprobe", "-v", "error", 
        "-select_streams", "v:0", 
        "-show_entries", "stream=r_frame_rate", 
        "-of", "default=noprint_wrappers=1:nokey=1", 
        input_video
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    frame_rate_str = result.stdout.strip()
    
    # Parse frame rate (could be in format like "30000/1001")
    if '/' in frame_rate_str:
        num, den = map(int, frame_rate_str.split('/'))
        frame_rate = num / den
    else:
        frame_rate = float(frame_rate_str)
    
    return duration, frame_rate

def filter_slice_points(silences, min_gap=0.2):
    """Filter out slice points that are too close together"""
    if not silences:
        return []
    
    filtered_points = []
    last_end = 0
    
    for start, end in silences:
        # Only keep silences that are far enough from the last silence
        if start - last_end >= min_gap:
            filtered_points.append((start, end))
            last_end = end
    
    return filtered_points

# Format time as HH:MM:SS.mmm
def format_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    seconds = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"

def create_mlt_file(input_video, silences, duration, frame_rate):
    """Create an MLT file for Shotcut with slice points"""
    
    # Create the root element with standalone="no"
    root = ET.Element("mlt", {
        "LC_NUMERIC": "C",
        "version": "7.4.0",
        "title": "Shotcut version 22.01.30",
        "producer": "main_bin",
        "standalone": "no"
    })
    
    # Add profile first
    profile = ET.SubElement(root, "profile", {
        "description": "automatic",
        "width": "1920",
        "height": "1080",
        "progressive": "1",
        "sample_aspect_num": "1",
        "sample_aspect_den": "1",
        "display_aspect_num": "16",
        "display_aspect_den": "9",
        "frame_rate_num": str(int(frame_rate)),
        "frame_rate_den": "1",
        "colorspace": "709"
    })
    
    # Add main_bin playlist
    main_bin = ET.SubElement(root, "playlist", {"id": "main_bin"})
    ET.SubElement(main_bin, "property", {"name": "xml_retain"}).text = "1"

    # Add black background producer
    black = ET.SubElement(root, "producer", {
        "id": "black",
        "in": "00:00:00.000",
        "out": format_time(duration)
    })
    ET.SubElement(black, "property", {"name": "length"}).text = format_time(duration + 0.017)
    ET.SubElement(black, "property", {"name": "eof"}).text = "pause"
    ET.SubElement(black, "property", {"name": "resource"}).text = "0"
    ET.SubElement(black, "property", {"name": "aspect_ratio"}).text = "1"
    ET.SubElement(black, "property", {"name": "mlt_service"}).text = "color"
    ET.SubElement(black, "property", {"name": "mlt_image_format"}).text = "rgba"
    ET.SubElement(black, "property", {"name": "set.test_audio"}).text = "0"
    
    # Add background playlist
    background = ET.SubElement(root, "playlist", {"id": "background"})
    ET.SubElement(background, "entry", {
        "producer": "black",
        "in": "00:00:00.000",
        "out": format_time(duration)
    })
    
    # Create chains for each segment
    chains = []
    video_basename = os.path.basename(input_video)
    
    # Process silence points to create clips
    segments = []
    current_pos = 0
    
    print("\nDebug: Silence periods:")
    for silence in silences:
        start, end = silence
        # Ensure start is before end
        if start > end:
            start, end = end, start
        print(f"  {format_time(start)} -> {format_time(end)}")
        
        # Add segment before silence if there is content
        if start > current_pos:
            segments.append((current_pos, start))
        
        # Update position to end of silence
        current_pos = end
    
    # Add final segment if needed
    if current_pos < duration:
        segments.append((current_pos, duration))
    
    print("\nDebug: Generated segments:")
    for start, end in segments:
        print(f"  {format_time(start)} -> {format_time(end)}")
    
    # Calculate video hash once
    video_hash = generate_file_hash(input_video)

    # Create chains for each segment first
    for i, (start, end) in enumerate(segments):
        chain_id = f"chain{i}"
        chain = ET.SubElement(root, "chain", {
            "id": chain_id,
            "out": format_time(duration)
        })
        ET.SubElement(chain, "property", {"name": "length"}).text = format_time(duration)
        ET.SubElement(chain, "property", {"name": "eof"}).text = "pause"
        ET.SubElement(chain, "property", {"name": "resource"}).text = video_basename
        ET.SubElement(chain, "property", {"name": "mlt_service"}).text = "avformat-novalidate"
        ET.SubElement(chain, "property", {"name": "seekable"}).text = "1"
        ET.SubElement(chain, "property", {"name": "audio_index"}).text = "1"
        ET.SubElement(chain, "property", {"name": "video_index"}).text = "0"
        ET.SubElement(chain, "property", {"name": "mute_on_pause"}).text = "0"
        ET.SubElement(chain, "property", {"name": "shotcut:hash"}).text = video_hash
        ET.SubElement(chain, "property", {"name": "ignore_points"}).text = "0"
        ET.SubElement(chain, "property", {"name": "shotcut:caption"}).text = video_basename
        ET.SubElement(chain, "property", {"name": "xml"}).text = "was here"

    # Now create playlist and add entries
    playlist = ET.SubElement(root, "playlist", {
        "id": "playlist0",
        "title": "V1"
    })
    ET.SubElement(playlist, "property", {"name": "shotcut:video"}).text = "1"
    ET.SubElement(playlist, "property", {"name": "shotcut:name"}).text = "V1"

    # Add entries to playlist
    frame_time = 1.0 / frame_rate
    for i, (start, end) in enumerate(segments):
        # If this is the second segment, adjust its start time to be one frame after previous end
        if i > 0:
            start = segments[i-1][1] + frame_time
        entry = ET.SubElement(playlist, "entry", {
            "producer": f"chain{i}",
            "in": format_time(start),
            "out": format_time(end)
        })

    # Add tractor
    tractor = ET.SubElement(root, "tractor", {
        "id": "tractor0",
        "title": "Shotcut version 22.01.30",
        "in": "00:00:00.000",
        "out": format_time(duration)
    })
    ET.SubElement(tractor, "property", {"name": "shotcut"}).text = "1"
    ET.SubElement(tractor, "property", {"name": "shotcut:projectAudioChannels"}).text = "2"
    ET.SubElement(tractor, "property", {"name": "shotcut:projectFolder"}).text = "0"
    
    ET.SubElement(tractor, "track", {"producer": "background"})
    ET.SubElement(tractor, "track", {"producer": "playlist0"})
    
    # Add transitions
    transition0 = ET.SubElement(tractor, "transition", {"id": "transition0"})
    ET.SubElement(transition0, "property", {"name": "a_track"}).text = "0"
    ET.SubElement(transition0, "property", {"name": "b_track"}).text = "1"
    ET.SubElement(transition0, "property", {"name": "mlt_service"}).text = "mix"
    ET.SubElement(transition0, "property", {"name": "always_active"}).text = "1"
    ET.SubElement(transition0, "property", {"name": "sum"}).text = "1"
    
    transition1 = ET.SubElement(tractor, "transition", {"id": "transition1"})
    ET.SubElement(transition1, "property", {"name": "a_track"}).text = "0"
    ET.SubElement(transition1, "property", {"name": "b_track"}).text = "1"
    ET.SubElement(transition1, "property", {"name": "version"}).text = "0.9"
    ET.SubElement(transition1, "property", {"name": "mlt_service"}).text = "frei0r.cairoblend"
    ET.SubElement(transition1, "property", {"name": "threads"}).text = "0"
    ET.SubElement(transition1, "property", {"name": "disable"}).text = "1"
    
    # Convert to pretty XML with standalone attribute in XML declaration
    xml_str = ET.tostring(root, encoding='unicode')
    doc = minidom.parseString(xml_str)
    doc.standalone = False
    pretty_xml = doc.toprettyxml(indent="  ")
    # Remove standalone from mlt tag if present
    pretty_xml = pretty_xml.replace(' standalone="no"', '', 1)
    
    # Write to file
    output_file = os.path.splitext(input_video)[0] + ".mlt"
    with open(output_file, "w") as f:
        f.write(pretty_xml)
    
    return output_file

def ensure_local_video_copy(input_video):
    """Return basename of video file"""
    return os.path.basename(input_video)

def main():
    # Parse arguments
    if len(sys.argv) < 2:
        usage()
    
    input_video = sys.argv[1]
    threshold = sys.argv[2] if len(sys.argv) > 2 else "-30dB"
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else 0.25
    min_gap = float(sys.argv[4]) if len(sys.argv) > 4 else 0.2
    
    # Check if input file exists
    if not os.path.isfile(input_video):
        print(f"Error: Input file '{input_video}' not found")
        sys.exit(1)
    
    # Ensure a local copy/link of the video exists
    ensure_local_video_copy(input_video)
    
    print(f"Processing {input_video}...")
    print(f"Silence threshold: {threshold}")
    print(f"Minimum silence duration: {duration} seconds")
    print(f"Minimum gap between slices: {min_gap} seconds")
    
    # Get video info
    video_duration, frame_rate = get_video_info(input_video)
    print(f"Video duration: {video_duration:.2f} seconds")
    print(f"Frame rate: {frame_rate:.3f} fps")
    
    # Detect silences
    print("Detecting silences...")
    silences = detect_silences(input_video, threshold, duration)
    print(f"Found {len(silences)} silence periods")
    
    # Filter slice points
    filtered_silences = filter_slice_points(silences, min_gap)
    print(f"After filtering: {len(filtered_silences)} slice points")
    
    # Create MLT file
    output_file = create_mlt_file(input_video, filtered_silences, video_duration, frame_rate)
    print(f"MLT file created: {output_file}")
    print("You can now import this MLT file into Shotcut")
    print("Note: The MLT file references the video file by its basename only.")
    print("      Make sure the video file is in the same directory as the MLT file.")

if __name__ == "__main__":
    main()
