import re
from typing import Tuple
import pandas

import pymongo
from pymongo.synchronous.collection import Collection
from pymongo.synchronous.database import Database
from pymongo.typings import _Pipeline
from pathlib import Path
import argparse
import ffmpeg

HANDLE_LEN = 2.0
SECOND_FRAMES = 24
MINUTE_FRAMES = SECOND_FRAMES * 60
HOUR_FRAMES = MINUTE_FRAMES * 60


def conf_argparse() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument('-b', '--baselight', help="path to baselight file", required=True)
    parser.add_argument('-x', '--xytech', help="path to xytech file", required=True)
    
    parser.add_argument('-p', '--process', help="path to video to process", required=True)

    return parser.parse_args()

def insert_frame_data(df: pandas.DataFrame, db: Database, col_name: str) -> Collection:
    col = db[col_name]
    
    # # to persist data in developmennt
    if col.find_one():
        return col

    col.insert_many(df.to_dict(orient='records'))

    return col

# match path after prefix for xytech file paths
xytech_path_re = r'(/hpsans\d{2}/production/)([A-Za-z0-9/\-_]+)'
baselight_prefix = '/baselightfilesystem1/'

# matches the path after baselight file prefix AND the frame numbers into groups
baselight_path_re = rf'((?<={baselight_prefix})[A-Za-z0-9/\-_]+)((\s(\d+))+)'

def frames_to_ranges(frames:list[str]) -> list[str]:
        num_frames = len(frames)
        in_range = False
        anchor = frames[0]

        out = []
        for i, num in enumerate(frames):
            if in_range is False:
                # start new range
                anchor = num
                in_range = True
            
            if (i == num_frames - 1 or int(frames[i + 1]) - int(num) > 1):
                # last frame or next frame is too far to add to range
                # end range
                in_range = False
                if anchor != num:
                    # actually add range text if the current number isnt the anchor
                    out.append(f'{anchor}-{num}')
                else:
                    out.append(anchor)

        return out

def get_unique_paths(file_name:str) -> dict[str, str]:
    unique_paths: dict[str,str] = {}
    with open(file_name) as file:
        for line in file.readlines():
            path_match = re.search(xytech_path_re, line)
            if path_match is None:
                continue

            prefix = path_match.group(1)
            suffix = path_match.group(2)
            unique_paths[suffix] = prefix

    return unique_paths

def get_frame_data(frame_file_path:str, relevant_paths_file_path:str):
    unique_paths = get_unique_paths(relevant_paths_file_path)

    data = {
            'Path': [],
            'Frames': [],
    }

    with open(frame_file_path) as frame_file:
        for export_line in frame_file.readlines():
            path_match = re.search(baselight_path_re, export_line)
            if path_match is None:
                continue
            
            
            path_suffix = path_match.group(1)
            path_prefix = unique_paths.get(path_suffix)
            if path_prefix is None:
                continue
        

            frames = re.split(r'\s+', path_match.group(2).strip())
            xytech_path = f"{path_prefix}{path_suffix}"
            ranges = frames_to_ranges(frames)
            data['Frames'].extend(ranges)
            for _ in ranges:
                data['Path'].append(xytech_path)


    return pandas.DataFrame.from_dict(data, orient="columns")

def get_video_stream(video_data: dict) -> dict:
    vstream = next((stream for stream in video_data['streams'] if stream['codec_type'] == 'video'), None)
    if vstream is None:
        raise Exception("metadata is not valid or is not from a video source")

    return vstream

def get_total_frames(vstream: dict) -> int:
    nb_frames = vstream.get('nb_frames')
    if nb_frames is None:
        raise Exception("given stream is missing frame data or is not a video stream")

    return int(nb_frames)

def get_fps(vstream: dict) -> float:
    avg_frame_rate = vstream.get('avg_frame_rate')
    if avg_frame_rate is None:
        raise Exception("given stream is missing fps data or is not a video stream")
    
    parts = avg_frame_rate.split('/')

    return float(parts[0]) / float(parts[1])


def get_range_parts(entry_frames:str) -> list[str]:
    return entry_frames.split('-')

def get_frames_with_handles(range_parts: list[str], handle_len: float, fps: float) -> Tuple[float, float]:
    handle_frames = handle_len * fps
    start = max(float(range_parts[0]) - handle_frames, 0)
    end = float(range_parts[1]) + handle_frames

    return (start, end)

# needs fps info
def get_range_handles_below(frame_data: list[dict[str, str]], threshold: int, fps: float) -> list[dict[str, Tuple[float, float]]]:
    ranges = []
    for entry in frame_data:
        entry_frames = entry['Frames']
        range_parts = get_range_parts(entry_frames)
        if len(range_parts) == 1:
            if int(range_parts[0]) >= threshold:
                return ranges

            continue

        if (int(range_parts[0]) >= threshold) or (int(range_parts[0]) >= threshold):
            return ranges

        new_frames = get_frames_with_handles(range_parts, HANDLE_LEN, fps)
        ranges.append({
            'Path': entry['Path'],
            'Frames': new_frames
            })

    return ranges

# unsure if this timecode would work with ffmpeg
# pretty sure he wants this for output tho
def frames_to_timecode(frames: int):
    hours = frames // HOUR_FRAMES
    frames = frames % HOUR_FRAMES

    minutes = frames // (24 * 60)
    frames = frames % MINUTE_FRAMES
    
    seconds = frames // SECOND_FRAMES
    frames = frames % SECOND_FRAMES

    return(f'{str(hours).zfill(2)}:{str(minutes).zfill(2)}:{str(seconds).zfill(2)}:{str(frames).zfill(2)}')

            


def init_mongodb() -> Database:
    myclient = pymongo.MongoClient("mongodb://localhost:27017")
    db = myclient['local']

    return db

def main():
    args = conf_argparse()

    baselight = args.baselight
    xytech = args.xytech

    df = get_frame_data(baselight, xytech)
    db = init_mongodb()
    frame_data_col = insert_frame_data(df, db, "frame-data")
    frame_data = list(frame_data_col.find({}, { '_id': 0 }))

    video_data = ffmpeg.probe(args.process)
    vstream = get_video_stream(video_data)
    nb_frames = get_total_frames(vstream)
    fps = get_fps(vstream)

    ranges = get_range_handles_below(frame_data, nb_frames, fps)



if __name__ == "__main__":
    main()
