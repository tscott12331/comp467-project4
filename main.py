import json
import time
import sys
import re
from typing import Annotated, Any, Iterable, Literal, Optional, Tuple, TypedDict, Union
import pandas

from pydantic import Field
from pydantic.config import ConfigDict
import pymongo
from pymongo.synchronous.collection import Collection
from pymongo.synchronous.database import Database
from pymongo.typings import _Pipeline
from pydantic.dataclasses import dataclass
from pathlib import Path
from vimeo import VimeoClient
import argparse
import ffmpeg

HANDLE_LEN = 2.0
SECOND_FRAMES = 24
MINUTE_FRAMES = SECOND_FRAMES * 60
HOUR_FRAMES = MINUTE_FRAMES * 60

DEF_COL_NAME = "frame_data"

@dataclass
class FramePaths:
    baselight: Path
    xytech: Path


# arbitrary type config needed for pymongo Collection type
@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class InsertOnly:
    config_type: Literal['insert_only']
    frame_paths: FramePaths
    col: Collection

@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class InsertAndProcess:
    config_type: Literal['insert_and_process']
    frame_paths: FramePaths
    col: Collection
    video_path: Path
    out_path: Path

@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class ReadAndProcess:
    config_type: Literal['read_and_process']
    col: Collection
    video_path: Path
    out_path: Path

@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class Pull:
    config_type: Literal['pull']
    out_path: Path

Config = Annotated[Union[InsertOnly, InsertAndProcess, ReadAndProcess, Pull], Field(discriminator='config_type')]

@dataclass
class VideoData:
    nb_frames: int
    fps: float



class FrameData(TypedDict):
    Path: str
    Frames: str

class HandleFrameData(FrameData):
    Handles: Tuple[int, int]




def conf_argparse() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument('-b', '--baselight', help="path to baselight file")
    parser.add_argument('-x', '--xytech', help="path to xytech file")

    parser.add_argument('-c', '--collection', help="mongodb collection to store/retrieve data", default="frame_data")
    
    parser.add_argument('-o', '--out', help="output path for processed frames xls or pulled vimeo data csv")
    
    group = parser.add_mutually_exclusive_group()
    group.add_argument('-p', '--process', help="path to video to process")
    group.add_argument('-P', '--pull', help="pull vimeo video data", action="store_true")

    return parser.parse_args()

def get_config(args: argparse.Namespace) -> Config:
    col_name = args.collection or DEF_COL_NAME
    db = init_mongodb()
    col = db[col_name]

    baselight = args.baselight
    xytech = args.xytech

    if (baselight is not None) and (xytech is not None):
        frame_paths = FramePaths(Path(baselight), Path(xytech))
    elif (baselight is None) and (xytech is None):
        frame_paths = None
    else:
        raise Exception("need to specify both baselight and xytech file or neither")

    video_path_str = args.process
    out_path_str = args.out
    pull = args.pull

    if video_path_str is None:
        if pull:
            assert out_path_str is not None, "pulling vimeo video data requires output path"
            out_path = Path(out_path_str)
            return Pull('pull', out_path)

        assert frame_paths is not None, "if not processing video, specify baselight and xytech file to insert"
        return InsertOnly('insert_only', frame_paths, col)
    else:
        assert out_path_str is not None, "processing video requires output path"
        video_path = Path(video_path_str)
        out_path = Path(out_path_str)
        if frame_paths is None:
            return ReadAndProcess('read_and_process', col, video_path, out_path)
        else:
            return InsertAndProcess('insert_and_process', frame_paths, col, video_path, out_path)
        



def insert_frame_data(col: Collection, df: pandas.DataFrame) -> Collection:
    col.delete_many({})
    col.insert_many(df.to_dict(orient='records'))

    return col

def insert_frame_files(baselight_path: Path, xytech_path: Path, col: Collection):
    df = get_frame_data(str(baselight_path), str(xytech_path))
    insert_frame_data(col, df)

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

def get_frames_with_handles(range_parts: list[str], handle_len: float, fps: float) -> Tuple[int, int]:
    handle_frames = handle_len * fps
    start = max(int(range_parts[0]) - handle_frames, 0)
    end = int(range_parts[1]) + handle_frames
    start = int(start)
    end = int(end)

    return (start, end)

def get_range_handles_below_thresh(frame_data: list[FrameData], threshold: int, fps: float) -> list[HandleFrameData]:
    ranges: list[HandleFrameData] = []
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
            **entry,
            'Handles': new_frames
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

def ffmpeg_run(sequence):
    try:
        sequence.run(capture_stdout=True, capture_stderr=True)
    except ffmpeg.Error as e:
        raise Exception(f"ffmpeg error: {e.stderr.decode()}")

def create_video_snippet(video_path: Path, out_path: Path, handles: Tuple[int, int], fps: float):
    start, end = handles
    start_sec = start / fps
    end_sec = end / fps
    input = ffmpeg.input(str(video_path))
    trimmed_video = (
        input.video
            .filter('trim', start_frame=start, end_frame=end)
            .setpts('PTS-STARTPTS')
        )

    trimmed_audio = (
        input.audio
            .filter('atrim', start=start_sec, end=end_sec)
            .filter('asetpts', 'PTS-STARTPTS')
        )

    sequence = (
        ffmpeg
            .output(trimmed_video, trimmed_audio, str(out_path), fps_mode='passthrough')
            .overwrite_output()
        )

    ffmpeg_run(sequence)
    print(f'Created snippet from frames {start} to {end} at {out_path}')


def create_snippets(video_path: Path, handles: Iterable[Tuple[int, int]], fps:float):
    tmp_folder_path = Path(f'tmp_{time.time()}')
    tmp_folder_path.mkdir(parents=True, exist_ok=True)

    for handle in handles:
        start, end = handle
        out_path = tmp_folder_path / f'{video_path.stem}_{start}_{end}{video_path.suffix}'
        create_video_snippet(video_path, out_path, handle, fps)


def init_mongodb() -> Database:
    myclient = pymongo.MongoClient("mongodb://localhost:27017")
    db = myclient['local']

    return db

def read_frame_data(col: Collection) -> list[FrameData]:
    return list(col.find({}, { '_id': 0 }))


def get_video_data(video_path: Path) -> VideoData:
    video_data = ffmpeg.probe(str(video_path))
    vstream = get_video_stream(video_data)
    nb_frames = get_total_frames(vstream)
    fps = get_fps(vstream)
    return VideoData(nb_frames, fps)

def process_collection(video_path: Path, col: Collection):
    frame_data = read_frame_data(col)
    process(video_path, frame_data)

def process(video_path: Path, frame_data: list[FrameData]):
    video_data = get_video_data(video_path)

    # TODO: could change frame_data in place
    handle_frame_data = get_range_handles_below_thresh(frame_data, video_data.nb_frames, video_data.fps)

    handles = (entry['Handles'] for entry in handle_frame_data)
    create_snippets(video_path, handles, video_data.fps)

def pull_action(c: Pull):
    raise NotImplementedError()

def insert_only_action(c: InsertOnly):
    insert_frame_files(c.frame_paths.baselight, c.frame_paths.xytech, c.col)

def insert_and_process_action(c: InsertAndProcess):
    insert_frame_files(c.frame_paths.baselight, c.frame_paths.xytech, c.col)
    process_collection(c.video_path, c.col)


def read_and_process_action(c: ReadAndProcess):
    process_collection(c.video_path, c.col)


def main():
    try:
        args = conf_argparse()
        config = get_config(args)

        match config:
            case Pull() as c:
                pull_action(c)
            case InsertOnly() as c:
                insert_only_action(c)
            case InsertAndProcess() as c:
                insert_and_process_action(c)
            case ReadAndProcess() as c:
                read_and_process_action(c)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)



if __name__ == "__main__":
    main()
