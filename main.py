import inspect
import os
import time
import sys
import re
from typing import Annotated, Iterable, Literal, Tuple, TypedDict, Union, cast
import pandas
from xlsxwriter.worksheet import Worksheet

from pydantic import BaseModel, ConfigDict, Field
import pymongo
from pymongo.synchronous.collection import Collection
from pymongo.synchronous.database import Database
from pymongo.typings import _Pipeline
from pathlib import Path
from dotenv import load_dotenv
from vimeo import VimeoClient
import argparse
import ffmpeg

HANDLE_LEN = 2.0
SECOND_FRAMES = 24
MINUTE_FRAMES = SECOND_FRAMES * 60
HOUR_FRAMES = MINUTE_FRAMES * 60

DEF_COL_NAME = "frame_data"

VIMEO_GET_VIDS_URL = "https://api.vimeo.com/me/videos"

THUMB_WIDTH = 96
THUMB_HEIGHT = 74

class FramePaths(BaseModel):
    baselight: Path
    xytech: Path


# arbitrary type config needed for pymongo Collection type
class InsertOnly(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    config_type: Literal['insert_only']
    frame_paths: FramePaths
    col: Collection

class InsertAndProcess(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    config_type: Literal['insert_and_process']
    frame_paths: FramePaths
    col: Collection
    video_path: Path
    out_path: Path

class ReadAndProcess(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    config_type: Literal['read_and_process']
    col: Collection
    video_path: Path
    out_path: Path

class Pull(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    config_type: Literal['pull']
    out_path: Path
    vimeo_client: VimeoClient

Config = Annotated[Union[InsertOnly, InsertAndProcess, ReadAndProcess, Pull], Field(discriminator='config_type')]

class VideoData(BaseModel):
    nb_frames: int
    fps: float



type RangeParts = Tuple[int, int]

class FrameData(TypedDict):
    Location: str
    Frames: str

class HandleFrameData(FrameData):
    Handles: RangeParts
    RangeParts: RangeParts




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

def get_vimeo_credentials():
    client_id = os.getenv("VIMEO_CLIENT_ID")
    assert client_id is not None, "missing VIMEO_CLIENT_ID in env"
    client_secret = os.getenv("VIMEO_CLIENT_SECRET")
    assert client_secret is not None, "missing VIMEO_CLIENT_SECRET in env"
    access_token = os.getenv("VIMEO_ACCESS_TOKEN")
    assert access_token is not None, "missing VIMEO_ACCESS_TOKEN in env"

    return (client_id, client_secret, access_token)


def get_config(args: argparse.Namespace) -> Config:
    col_name = args.collection or DEF_COL_NAME
    db = init_mongodb()
    col = db[col_name]

    baselight = args.baselight
    xytech = args.xytech

    if (baselight is not None) and (xytech is not None):
        frame_paths = FramePaths(baselight=Path(baselight), xytech=Path(xytech))
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

            client_id, client_secret, access_token = get_vimeo_credentials()
            vimeo_client = VimeoClient(key=client_id, token=access_token, secret=client_secret)

            return Pull(config_type='pull', out_path=out_path, vimeo_client=vimeo_client)

        assert frame_paths is not None, "if not processing video, specify baselight and xytech file to insert"
        return InsertOnly(config_type='insert_only', frame_paths=frame_paths, col=col)
    else:
        assert out_path_str is not None, "processing video requires output path"
        video_path = Path(video_path_str)
        out_path = Path(out_path_str)
        if frame_paths is None:
            return ReadAndProcess(config_type='read_and_process', col=col, video_path=video_path, out_path=out_path)
        else:
            return InsertAndProcess(config_type='insert_and_process', frame_paths=frame_paths, col=col, video_path=video_path, out_path=out_path)
        



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
            'Location': [],
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
                data['Location'].append(xytech_path)


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


def get_frame_range_split(entry_frames:str) -> list[str]:
    return entry_frames.split('-')

def get_frames_with_handles(range_parts: RangeParts, handle_len: float, fps: float) -> RangeParts:
    handle_frames = handle_len * fps

    start = max(range_parts[0] - handle_frames, 0)
    end = range_parts[1] + handle_frames

    start = int(start)
    end = int(end)

    return (start, end)

def get_range_handles_below_thresh(frame_data: list[FrameData], threshold: int, fps: float) -> list[HandleFrameData]:
    ranges: list[HandleFrameData] = []
    for entry in frame_data:
        entry_frames = entry['Frames']
        range_parts = get_frame_range_split(entry_frames)
        if len(range_parts) == 1:
            if int(range_parts[0]) >= threshold:
                return ranges

            continue

        assert len(range_parts) >= 2, "range_parts invalid length"
        range_parts = (int(range_parts[0]), int(range_parts[1]))

        if (range_parts[0] >= threshold) or (range_parts[0] >= threshold):
            return ranges

        new_frames = get_frames_with_handles(range_parts, HANDLE_LEN, fps)
        ranges.append({
            **entry,
            'Handles': new_frames,
            'RangeParts': range_parts
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


def create_snippets(video_path: Path, handles: Iterable[Tuple[int, int]], fps:float) -> Tuple[list[Path], Path]:
    tmp_folder_path = Path(f'tmp_{time.time()}')
    tmp_folder_path.mkdir(parents=True, exist_ok=True)
    out_paths: list[Path] = []

    for handle in handles:
        start, end = handle
        out_path = tmp_folder_path / f'{video_path.stem}_{start}_{end}{video_path.suffix}'
        create_video_snippet(video_path, out_path, handle, fps)
        out_paths.append(out_path)

    return (out_paths, tmp_folder_path)

def create_thumbnail(input_path: Path) -> Path:
    # enforce png thumbnail
    out_path = input_path.with_suffix('.thumb.png')

    ffmpeg_run(
        ffmpeg
            .input(str(input_path), ss=0)
            .filter('scale', THUMB_WIDTH, THUMB_HEIGHT)
            .output(str(out_path), vframes=1)
            .overwrite_output()
        )

    return out_path


def init_mongodb() -> Database:
    myclient = pymongo.MongoClient("mongodb://localhost:27017")
    db = myclient['local']

    return db

def read_frame_data(col: Collection) -> list[FrameData]:
    return list(col.find({}, { '_id': 0 }))

def range_parts_to_timecode_str(range_parts: RangeParts) -> str:
    return "-".join((frames_to_timecode(range_parts[0]), frames_to_timecode(range_parts[1])))

def prepare_handle_frame_data_export(handle_frame_data: list[HandleFrameData]):
    export_records = list(map(lambda hfd: {
        'Location': hfd['Location'],
        'Frame Range': hfd['Frames'],
        'Timecode Range': range_parts_to_timecode_str(hfd['RangeParts']),
    }, handle_frame_data))

    export_records = cast(list[dict[str, str]], export_records)
    return export_records


def get_col_widths(data: list[dict[str, str]]):
    widths: dict[str, int] = {}

    for entry in data:
        for key, val in entry.items():
            entry_length = len(val)
            cur_max = widths.get(key) or 0
            widths[key] = max(entry_length, cur_max)

    return list(widths.values())



def export_xlsx(handle_frame_data: list[HandleFrameData], thumb_paths: list[Path], out_path: Path):
    export_records = prepare_handle_frame_data_export(handle_frame_data)
    widths = get_col_widths(export_records)

    df = pandas.DataFrame.from_records(export_records)

    # insert text data into xlsx
    writer = pandas.ExcelWriter(out_path, engine='xlsxwriter')
    startrow = 0
    df.to_excel(writer, sheet_name="Sheet1", index=False, startrow=startrow)

    worksheet: Worksheet = writer.sheets["Sheet1"]
    cols = len(df.columns)

    # add images
    worksheet.set_column(cols, cols, THUMB_WIDTH)
    worksheet.write_string(startrow, cols, "Thumbnail")
    for i, thumb_path in enumerate(thumb_paths):
        worksheet.set_row(startrow + i + 1, THUMB_HEIGHT)
        worksheet.embed_image(startrow + i + 1, cols, str(thumb_path))


    # size columns
    for i, width in enumerate(widths):
        worksheet.set_column(i, i, width)


    # save and close xlsx file
    writer.close()
    print(f'Exported xlsx file to {out_path}')

def get_video_data(video_path: Path) -> VideoData:
    video_data = ffmpeg.probe(str(video_path))
    vstream = get_video_stream(video_data)
    nb_frames = get_total_frames(vstream)
    fps = get_fps(vstream)
    return VideoData(nb_frames=nb_frames, fps=fps)

def process_collection(video_path: Path, col: Collection, out_path: Path):
    frame_data = read_frame_data(col)
    process(video_path, frame_data, out_path)

def process(video_path: Path, frame_data: list[FrameData], out_path: Path):
    video_data = get_video_data(video_path)

    # TODO: could change frame_data in place
    handle_frame_data = get_range_handles_below_thresh(frame_data, video_data.nb_frames, video_data.fps)
    handles = (entry['Handles'] for entry in handle_frame_data)

    snippet_paths, tmp_folder_path = create_snippets(video_path, handles, video_data.fps)

    # create thumbs and export xls
    thumb_paths = [create_thumbnail(path) for path in snippet_paths]
    export_xlsx(handle_frame_data, thumb_paths, out_path)
    # export_frame_data: list[ExportFrameData] = combine_handle_frame_data_thumbnail(handle_frame_data, thumb_paths)
    
    

    




def pull_action(c: Pull):
    res = c.vimeo_client.get(VIMEO_GET_VIDS_URL)
    json_data = res.json()
    videos = json_data['data']
    videos = map(lambda v: {
                'Title': v['name'],
                'URI': v['uri'],
                'Link': v['link'],
                'Status': v['status'],
            }, videos)

    df = pandas.DataFrame.from_records(videos)

    df.to_csv(c.out_path)
def insert_only_action(c: InsertOnly):
    insert_frame_files(c.frame_paths.baselight, c.frame_paths.xytech, c.col)

def insert_and_process_action(c: InsertAndProcess):
    insert_frame_files(c.frame_paths.baselight, c.frame_paths.xytech, c.col)
    process_collection(c.video_path, c.col, c.out_path)


def read_and_process_action(c: ReadAndProcess):
    process_collection(c.video_path, c.col, c.out_path)


def main():
    try:
        load_dotenv()
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
