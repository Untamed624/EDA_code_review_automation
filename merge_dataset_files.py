import argparse
import json
import re
from pathlib import Path

from json_to_jsonl import JsonArrayStream


FILENAME_RANGE_PATTERNS = (
    re.compile(r"repo(?P<repo_index>\d+)_pr(?P<start>\d+)_(?P<end>\d+)", re.IGNORECASE),
    re.compile(r"_repo(?P<repo_index>\d+)_prs(?P<start>\d+)_(?P<end>\d+)", re.IGNORECASE),
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Merge multiple JSON/JSONL dataset files in the given order."
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Input files in merge order. Supports .json array files and .jsonl files.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Merged output file path.",
    )
    parser.add_argument(
        "--format",
        choices=["jsonl", "json"],
        default="jsonl",
        help="Output format. Default is jsonl.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    parser.add_argument(
        "--dedupe",
        action="store_true",
        help="Skip duplicate records. Useful when PR ranges overlap after interrupted runs.",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Only print each input file's inferred PR-position range; do not merge.",
    )
    parser.add_argument(
        "--sort-files-by-pr-position",
        action="store_true",
        help=(
            "Sort input files by inferred repo index and PR-position start before merging. "
            "This is useful when a later run fills a broken middle segment."
        ),
    )
    return parser.parse_args()


def iter_jsonl_records(input_path):
    with input_path.open("r", encoding="utf-8-sig") as file:
        for line_number, line in enumerate(file, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {input_path}:{line_number}: {exc}") from exc


def iter_records(input_path):
    input_path = Path(input_path)
    suffix = input_path.suffix.lower()

    if suffix == ".jsonl":
        yield from iter_jsonl_records(input_path)
    elif suffix == ".json":
        with JsonArrayStream(input_path) as stream:
            yield from stream.records()
    else:
        raise ValueError(f"Unsupported input file type: {input_path}")


def parse_filename_range(input_path):
    name = Path(input_path).name
    for pattern in FILENAME_RANGE_PATTERNS:
        match = pattern.search(name)
        if match:
            return {
                "repo_index": int(match.group("repo_index")),
                "start": int(match.group("start")),
                "end": int(match.group("end")),
            }
    return {}


def default_progress_log_path(input_path):
    input_path = Path(input_path)
    direct_path = Path(f"{input_path}.progress.jsonl")
    if direct_path.exists():
        return direct_path

    filename_range = parse_filename_range(input_path)
    repo_index = filename_range.get("repo_index")
    start = filename_range.get("start")
    if repo_index is None or start is None:
        return direct_path

    candidates = sorted(
        input_path.parent.glob(f"repo{repo_index:03d}_pr{start:05d}_*.progress.jsonl")
    )
    if candidates:
        return candidates[0]

    candidates = sorted(
        input_path.parent.glob(f"*repo{repo_index:03d}_prs{start:04d}_*.progress.jsonl")
    )
    if candidates:
        return candidates[0]

    return direct_path


def read_progress_summary(input_path):
    progress_path = default_progress_log_path(input_path)
    if not progress_path.exists():
        return {}

    statuses = {}
    positions = []
    repo_index = None
    repo = ""
    pr_numbers = set()
    zero_record_prs = 0

    with progress_path.open("r", encoding="utf-8-sig") as file:
        for line_number, line in enumerate(file, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid progress JSONL at {progress_path}:{line_number}: {exc}") from exc

            status = record.get("status", "")
            statuses[status] = statuses.get(status, 0) + 1
            if repo_index is None and record.get("repo_index") is not None:
                repo_index = record.get("repo_index")
            if not repo and record.get("repo"):
                repo = record.get("repo")
            if record.get("pr_position") is not None:
                positions.append(int(record["pr_position"]))
            if record.get("pr_number") is not None:
                pr_numbers.add(record["pr_number"])
            if record.get("record_count") == 0:
                zero_record_prs += 1

    summary = {
        "progress_path": progress_path,
        "progress_entries": sum(statuses.values()),
        "progress_statuses": statuses,
        "zero_record_prs": zero_record_prs,
        "progress_pr_count": len(pr_numbers),
    }
    if repo:
        summary["repo"] = repo
    if repo_index is not None:
        summary["repo_index"] = repo_index
    if positions:
        summary["progress_start"] = min(positions)
        summary["progress_end"] = max(positions)
    return summary


def inspect_file(input_path):
    input_path = Path(input_path)
    filename_range = parse_filename_range(input_path)
    progress = read_progress_summary(input_path)

    repos = set()
    pr_numbers = []
    seen_pr_numbers = set()
    record_count = 0
    first_pr_number = None
    last_pr_number = None

    for record in iter_records(input_path):
        record_count += 1
        repo = record.get("repo")
        if repo:
            repos.add(repo)
        pr_number = record.get("pr_number")
        if pr_number is not None:
            if first_pr_number is None:
                first_pr_number = pr_number
            last_pr_number = pr_number
            if pr_number not in seen_pr_numbers:
                seen_pr_numbers.add(pr_number)
                pr_numbers.append(pr_number)

    start = progress.get("progress_start", filename_range.get("start"))
    end = progress.get("progress_end", filename_range.get("end"))
    repo_index = progress.get("repo_index", filename_range.get("repo_index"))

    return {
        "path": input_path,
        "repo_index": repo_index,
        "position_start": start,
        "position_end": end,
        "filename_start": filename_range.get("start"),
        "filename_end": filename_range.get("end"),
        "progress_path": progress.get("progress_path"),
        "progress_entries": progress.get("progress_entries", 0),
        "progress_statuses": progress.get("progress_statuses", {}),
        "zero_record_prs": progress.get("zero_record_prs", 0),
        "repos": sorted(repos),
        "record_count": record_count,
        "unique_pr_count": len(seen_pr_numbers),
        "first_pr_number": first_pr_number,
        "last_pr_number": last_pr_number,
    }


def print_inspection(summaries):
    for summary in summaries:
        print(f"File: {summary['path']}")
        print(f"  repo_index: {summary['repo_index']}")
        print(f"  inferred PR-position range: {summary['position_start']} - {summary['position_end']}")
        print(f"  filename PR-position range: {summary['filename_start']} - {summary['filename_end']}")
        if summary["progress_path"]:
            print(f"  progress log: {summary['progress_path']}")
            print(f"  progress entries: {summary['progress_entries']} {summary['progress_statuses']}")
            print(f"  zero-record PRs: {summary['zero_record_prs']}")
        else:
            print("  progress log: not found")
        print(f"  repos: {', '.join(summary['repos']) if summary['repos'] else '(unknown)'}")
        print(f"  records: {summary['record_count']}")
        print(f"  unique PR numbers in records: {summary['unique_pr_count']}")
        print(f"  first/last PR number in file: {summary['first_pr_number']} - {summary['last_pr_number']}")


def sort_paths_by_pr_position(input_paths):
    summaries = [inspect_file(input_path) for input_path in input_paths]

    def sort_key(item):
        repo_index = item["repo_index"] if item["repo_index"] is not None else 10**12
        start = item["position_start"] if item["position_start"] is not None else 10**12
        return repo_index, start, str(item["path"])

    sorted_summaries = sorted(summaries, key=sort_key)
    print("Merge order after sorting by PR position:")
    for summary in sorted_summaries:
        print(
            f"  {summary['path']} "
            f"(repo_index={summary['repo_index']}, "
            f"positions={summary['position_start']}-{summary['position_end']})"
        )
    return [summary["path"] for summary in sorted_summaries]


def record_key(record):
    repo = record.get("repo", "")
    pr_number = record.get("pr_number", "")
    comment_type = record.get("comment_type", "")
    review_id = record.get("review_id")
    comment_id = record.get("comment_id")
    comment_url = record.get("comment_url", "")

    if comment_id is not None:
        stable_id = f"comment:{comment_id}"
    elif review_id is not None:
        stable_id = f"review:{review_id}"
    elif comment_url:
        stable_id = f"url:{comment_url}"
    else:
        stable_id = json.dumps(record, ensure_ascii=False, sort_keys=True)

    return repo, pr_number, comment_type, stable_id


class OutputWriter:
    def __init__(self, output_path, output_format):
        self.output_path = Path(output_path)
        self.output_format = output_format
        self.file = None
        self.count = 0

    def __enter__(self):
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.output_path.open("w", encoding="utf-8", newline="")
        if self.output_format == "json":
            self.file.write("[\n")
        return self

    def write(self, record):
        if self.output_format == "jsonl":
            self.file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            self.file.write("\n")
        else:
            if self.count:
                self.file.write(",\n")
            self.file.write(json.dumps(record, ensure_ascii=False, indent=2))
        self.count += 1

    def __exit__(self, exc_type, exc, traceback):
        if self.output_format == "json":
            self.file.write("\n]\n")
        self.file.close()


def merge_files(input_paths, output_path, output_format, overwrite=False, dedupe=False):
    output_path = Path(output_path)
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"Output already exists: {output_path}")

    seen = set()
    skipped_duplicates = 0
    per_file_counts = []

    with OutputWriter(output_path, output_format) as writer:
        for input_path in input_paths:
            input_path = Path(input_path)
            if not input_path.exists():
                raise FileNotFoundError(f"Input file not found: {input_path}")

            input_count = 0
            for record in iter_records(input_path):
                if dedupe:
                    key = record_key(record)
                    if key in seen:
                        skipped_duplicates += 1
                        continue
                    seen.add(key)

                writer.write(record)
                input_count += 1

            per_file_counts.append((input_path, input_count))
            print(f"Merged {input_path} ({input_count} records kept)")

    return writer.count, skipped_duplicates, per_file_counts


def main():
    args = parse_args()
    input_paths = [Path(path) for path in args.inputs]

    if args.inspect:
        print_inspection([inspect_file(path) for path in input_paths])
        return

    if not args.output:
        raise ValueError("--output is required unless --inspect is used.")

    if args.sort_files_by_pr_position:
        input_paths = sort_paths_by_pr_position(input_paths)

    total_count, skipped_duplicates, _ = merge_files(
        input_paths,
        args.output,
        args.format,
        overwrite=args.overwrite,
        dedupe=args.dedupe,
    )
    print(f"Output: {Path(args.output).resolve()}")
    print(f"Total records written: {total_count}")
    if args.dedupe:
        print(f"Skipped duplicates: {skipped_duplicates}")


if __name__ == "__main__":
    main()
