import argparse
import json
from pathlib import Path

from data_collection import (
    DEFAULT_INPUT,
    GITHUB_API,
    GitHubClient,
    DatasetWriter,
    collect_pull_request_records,
    read_repositories,
)


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = BASE_DIR / "code_review_datasets"
PULLS_PER_PAGE = 100


def normalize_filename(value):
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value)


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Collect GitHub PR review/comment data for one repository and one PR index range. "
            "PR positions are 1-based in the selected sort order."
        )
    )
    parser.add_argument(
        "--github-token",
        default="",
        help="GitHub token used for API authentication.",
    )
    parser.add_argument(
        "--input",
        default=str(DEFAULT_INPUT),
        help="Input .xlsx/.csv containing repository url or full_name columns.",
    )
    parser.add_argument(
        "--repo-index",
        type=int,
        required=True,
        help="1-based repository index in the input repository file.",
    )
    parser.add_argument(
        "--pr-start",
        type=int,
        required=True,
        help="1-based start PR position in the selected repository.",
    )
    parser.add_argument(
        "--pr-end",
        type=int,
        required=True,
        help="1-based end PR position in the selected repository, inclusive.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Output JSON/JSONL path. If omitted, a part file is created under code_review_datasets/.",
    )
    parser.add_argument(
        "--format",
        choices=["json", "jsonl"],
        default="jsonl",
        help="Output format. Default is JSONL for safer large-batch crawling.",
    )
    parser.add_argument(
        "--request-sleep",
        type=float,
        default=0.2,
        help="Sleep seconds between API requests.",
    )
    parser.add_argument(
        "--pr-sort",
        choices=["created", "updated", "popularity", "long-running"],
        default="updated",
        help="GitHub PR sort order. Default is updated.",
    )
    parser.add_argument(
        "--pr-direction",
        choices=["asc", "desc"],
        default="desc",
        help="GitHub PR sort direction. Default is desc.",
    )
    parser.add_argument(
        "--skip-reviews",
        action="store_true",
        help="Skip top-level pull request reviews.",
    )
    parser.add_argument(
        "--skip-inline-comments",
        action="store_true",
        help="Skip inline pull request review comments.",
    )
    parser.add_argument(
        "--skip-pr-comments",
        action="store_true",
        help="Skip regular PR conversation comments.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from the progress log and append JSONL output. Only supported with --format jsonl.",
    )
    parser.add_argument(
        "--progress-log",
        default="",
        help="Progress JSONL path. Defaults to <output>.progress.jsonl.",
    )
    parser.add_argument(
        "--failed-log",
        default="",
        help="Failed PR JSONL path. Defaults to <output>.failed.jsonl.",
    )
    return parser.parse_args()


def validate_args(args):
    if args.repo_index <= 0:
        raise ValueError("--repo-index must be greater than 0.")
    if args.pr_start <= 0:
        raise ValueError("--pr-start must be greater than 0.")
    if args.pr_end <= 0:
        raise ValueError("--pr-end must be greater than 0.")
    if args.pr_start > args.pr_end:
        raise ValueError("--pr-start cannot be greater than --pr-end.")
    if args.resume and args.format != "jsonl":
        raise ValueError("--resume is only supported with --format jsonl.")


def default_output_path(repo_meta, args):
    repo_name = normalize_filename(repo_meta["repo"].replace("/", "_"))
    suffix = "jsonl" if args.format == "jsonl" else "json"
    filename = (
        f"{repo_name}_repo{args.repo_index:03d}_"
        f"prs{args.pr_start:04d}_{args.pr_end:04d}.{suffix}"
    )
    return DEFAULT_OUTPUT_DIR / filename


def default_sidecar_path(output_path, label):
    return Path(f"{output_path}.{label}.jsonl")


def get_repository_by_index(repos, repo_index):
    if repo_index > len(repos):
        raise IndexError(
            f"--repo-index {repo_index} is out of range. "
            f"The input file contains {len(repos)} repositories."
        )
    return repos[repo_index - 1]


def iter_pull_request_batch(client, repo_meta, args):
    owner, repo = repo_meta["repo"].split("/", 1)
    pulls_url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls"

    start_page = (args.pr_start - 1) // PULLS_PER_PAGE + 1
    end_page = (args.pr_end - 1) // PULLS_PER_PAGE + 1

    for page in range(start_page, end_page + 1):
        pull_requests = client.request_json(
            "GET",
            pulls_url,
            params={
                "state": "all",
                "sort": args.pr_sort,
                "direction": args.pr_direction,
                "per_page": PULLS_PER_PAGE,
                "page": page,
            },
            default=[],
        )
        if not isinstance(pull_requests, list) or not pull_requests:
            break

        for page_offset, pull_request in enumerate(pull_requests, start=1):
            position = (page - 1) * PULLS_PER_PAGE + page_offset
            if position < args.pr_start:
                continue
            if position > args.pr_end:
                return
            yield position, pull_request


class JsonlRecordWriter:
    def __init__(self, output_path, append=False):
        self.output_path = Path(output_path)
        self.append = append
        self.file = None
        self.count = 0

    def __enter__(self):
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if self.append else "w"
        self.file = self.output_path.open(mode, encoding="utf-8", newline="")
        return self

    def write(self, record):
        self.file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.count += 1

    def __exit__(self, exc_type, exc, traceback):
        self.file.close()


def open_dataset_writer(output_path, output_format, append=False):
    if output_format == "jsonl":
        return JsonlRecordWriter(output_path, append=append)
    return DatasetWriter(output_path, output_format=output_format)


def write_jsonl(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def pr_progress_key(repo, pr_number):
    return f"{repo}#{pr_number}"


def load_completed_pr_keys(progress_log):
    completed = set()
    path = Path(progress_log)
    if not path.exists():
        return completed

    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("status") != "success":
                continue
            repo = record.get("repo")
            pr_number = record.get("pr_number")
            if repo and pr_number is not None:
                completed.add(pr_progress_key(repo, pr_number))
    return completed


def collect_one_pull_request(client, repo_meta, pull_request, args):
    return collect_pull_request_records(
        client,
        repo_meta,
        pull_request,
        include_reviews=not args.skip_reviews,
        include_inline_comments=not args.skip_inline_comments,
        include_issue_comments=not args.skip_pr_comments,
    )


def collect_batch(client, repo_meta, args, output_path, progress_log, failed_log):
    completed_keys = load_completed_pr_keys(progress_log) if args.resume else set()
    processed_pr_count = 0
    skipped_pr_count = 0
    failed_pr_count = 0
    total_records = 0

    with open_dataset_writer(output_path, args.format, append=args.resume) as writer:
        for position, pull_request in iter_pull_request_batch(client, repo_meta, args):
            pr_number = pull_request.get("number")
            key = pr_progress_key(repo_meta["repo"], pr_number)
            if key in completed_keys:
                skipped_pr_count += 1
                print(f"[{repo_meta['repo']}] skip completed PR position {position} #{pr_number}")
                continue

            print(
                f"[{repo_meta['repo']}] PR position {position}/{args.pr_end} "
                f"#{pr_number}"
            )
            try:
                records = collect_one_pull_request(client, repo_meta, pull_request, args)
                for record in records:
                    writer.write(record)

                processed_pr_count += 1
                total_records += len(records)
                write_jsonl(
                    progress_log,
                    {
                        "status": "success",
                        "repo": repo_meta["repo"],
                        "repo_index": args.repo_index,
                        "pr_position": position,
                        "pr_number": pr_number,
                        "record_count": len(records),
                    },
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                failed_pr_count += 1
                error_record = {
                    "status": "failed",
                    "repo": repo_meta["repo"],
                    "repo_index": args.repo_index,
                    "pr_position": position,
                    "pr_number": pr_number,
                    "error": repr(exc),
                }
                write_jsonl(progress_log, error_record)
                write_jsonl(failed_log, error_record)
                print(f"Failed PR #{pr_number}: {exc}")

    return {
        "processed_pr_count": processed_pr_count,
        "skipped_pr_count": skipped_pr_count,
        "failed_pr_count": failed_pr_count,
        "total_records": total_records,
    }


def main():
    args = parse_args()
    validate_args(args)

    repos = read_repositories(args.input)
    repo_meta = get_repository_by_index(repos, args.repo_index)

    client = GitHubClient(
        token=args.github_token,
        request_sleep=args.request_sleep,
    )

    output_path = Path(args.output) if args.output else default_output_path(repo_meta, args)
    progress_log = Path(args.progress_log) if args.progress_log else default_sidecar_path(output_path, "progress")
    failed_log = Path(args.failed_log) if args.failed_log else default_sidecar_path(output_path, "failed")

    stats = collect_batch(client, repo_meta, args, output_path, progress_log, failed_log)

    print(f"Repository: {repo_meta['repo']}")
    print(f"Repository index: {args.repo_index}")
    print(f"PR position range: {args.pr_start}-{args.pr_end}")
    print(f"Processed PR count: {stats['processed_pr_count']}")
    print(f"Skipped PR count: {stats['skipped_pr_count']}")
    print(f"Failed PR count: {stats['failed_pr_count']}")
    print(f"Total records: {stats['total_records']}")
    print(f"Output: {output_path.resolve()}")
    print(f"Progress log: {progress_log.resolve()}")
    print(f"Failed log: {failed_log.resolve()}")


if __name__ == "__main__":
    main()
