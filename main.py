import re
import pandas

import pymongo
from pymongo.synchronous.collection import Collection
from pymongo.synchronous.database import Database
from pymongo.typings import _Pipeline
from pathlib import Path

def import_csv(csv_path: Path, dump_name:str, db: Database) -> Collection:
    data_frame = pandas.read_csv(csv_path)
    data_frame = data_frame.fillna('')
    data = data_frame.to_dict(orient='records')
    col = db[dump_name]
    col.insert_many(data)
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

def main():
    # baselight then xytech
    df = get_frame_data("Baselight_export_spring2026.txt", "Xytech_spring2026.txt")
    print(df)

if __name__ == "__main__":
    main()
