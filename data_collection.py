import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
except ModuleNotFoundError:
    requests = None


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = BASE_DIR / "final_repository.xlsx"
DEFAULT_OUTPUT = BASE_DIR / "github_review_comment_dataset.json"

GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = "https://api.github.com/graphql"

def normalize_text(value):
    if value is None:
        return ""
    return str(value)


def get_nested_login(payload, key="user"):
    user = payload.get(key) or {}
    return user.get("login", "") if isinstance(user, dict) else ""


def parse_repo_from_url(url):
    parsed = urlparse(str(url).strip())
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return None

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None

    owner = parts[0]
    repo = re.sub(r"\.git$", "", parts[1])
    return f"{owner}/{repo}"


def parse_diff_hunk(diff_hunk):
    old_lines = []
    new_lines = []
    diff_lines = normalize_text(diff_hunk).splitlines()

    for line in diff_lines:
        if not line or line.startswith("@@"):
            continue
        if line.startswith("\\ No newline"):
            continue
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("-"):
            old_lines.append(line[1:])
        elif line.startswith("+"):
            new_lines.append(line[1:])
        elif line.startswith(" "):
            old_lines.append(line[1:])
            new_lines.append(line[1:])

    return "\n".join(old_lines), "\n".join(new_lines)


def read_repositories(input_path):
    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    if input_path.suffix.lower() in {".xlsx", ".xls"}:
        try:
            import pandas as pd
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "pandas/openpyxl is required to read Excel input. "
                "Install them or pass a CSV file instead."
            ) from exc
        frame = pd.read_excel(input_path)
        rows = frame.to_dict(orient="records")
    elif input_path.suffix.lower() == ".csv":
        import csv

        with input_path.open("r", encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
    else:
        raise ValueError("Input file must be .xlsx, .xls, or .csv")

    repos = []
    seen = set()
    for row in rows:
        repo = normalize_text(row.get("full_name", "")).strip()
        if "/" not in repo:
            repo = parse_repo_from_url(row.get("url", ""))
        if not repo or repo in seen:
            continue
        seen.add(repo)
        repos.append(
            {
                "repo": repo,
                "repo_url": normalize_text(row.get("url", "")).strip()
                or f"https://github.com/{repo}",
                "source_row": row,
            }
        )
    return repos


class GitHubClient:
    def __init__(self, token="", timeout=30, request_sleep=0.2, max_retries=3):
        if requests is None:
            raise RuntimeError("requests is required. Install it with: pip install requests")

        self.timeout = timeout
        self.request_sleep = request_sleep
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "User-Agent": "github-review-comment-dataset-crawler",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def request_json(self, method, url, params=None, json_body=None, default=None):
        for attempt in range(1, self.max_retries + 1):
            if self.request_sleep > 0:
                time.sleep(self.request_sleep)

            response = self.session.request(
                method,
                url,
                params=params,
                json=json_body,
                timeout=self.timeout,
            )

            if response.status_code in {500, 502, 503, 504} and attempt < self.max_retries:
                time.sleep(attempt * 2)
                continue

            if response.status_code == 403:
                remaining = response.headers.get("X-RateLimit-Remaining")
                reset = response.headers.get("X-RateLimit-Reset")
                message = "GitHub API returned 403"
                if remaining == "0" and reset:
                    message += f"; rate limit resets at unix time {reset}"
                print(message, file=sys.stderr)
                return default

            if response.status_code == 404:
                return default

            if response.status_code >= 400:
                print(
                    f"GitHub API error {response.status_code}: {url}",
                    file=sys.stderr,
                )
                return default

            if not response.content:
                return default
            return response.json()

        return default

    def get_paginated(self, url, params=None, max_items=0):
        params = dict(params or {})
        params["per_page"] = 100
        results = []
        page = 1

        while True:
            params["page"] = page
            data = self.request_json("GET", url, params=params, default=[])
            if not isinstance(data, list) or not data:
                break

            results.extend(data)
            if max_items and len(results) >= max_items:
                return results[:max_items]
            if len(data) < 100:
                break
            page += 1

        return results

    def graphql(self, query, variables):
        if "Authorization" not in self.session.headers:
            return None
        data = self.request_json(
            "POST",
            GITHUB_GRAPHQL,
            json_body={"query": query, "variables": variables},
            default=None,
        )
        if not isinstance(data, dict) or data.get("errors"):
            return None
        return data.get("data")


class DatasetWriter:
    def __init__(self, output_path, output_format="json"):
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
            self.file.write(json.dumps(record, ensure_ascii=False) + "\n")
        else:
            if self.count:
                self.file.write(",\n")
            self.file.write(json.dumps(record, ensure_ascii=False, indent=2))
        self.count += 1

    def __exit__(self, exc_type, exc, traceback):
        if self.output_format == "json":
            self.file.write("\n]\n")
        self.file.close()


def fetch_review_thread_resolution_map(client, owner, repo, pr_number):
    query = """
    query($owner: String!, $repo: String!, $number: Int!, $after: String) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          reviewThreads(first: 100, after: $after) {
            pageInfo {
              hasNextPage
              endCursor
            }
            nodes {
              isResolved
              comments(first: 100) {
                nodes {
                  databaseId
                }
              }
            }
          }
        }
      }
    }
    """

    resolved_by_comment_id = {}
    after = None
    while True:
        data = client.graphql(
            query,
            {
                "owner": owner,
                "repo": repo,
                "number": int(pr_number),
                "after": after,
            },
        )
        if not data:
            return resolved_by_comment_id

        repository_data = data.get("repository") or {}
        pull_request = repository_data.get("pullRequest") or {}
        threads = pull_request.get("reviewThreads") or {}
        for thread in threads.get("nodes", []) or []:
            is_resolved = thread.get("isResolved")
            comments = thread.get("comments", {}).get("nodes", []) or []
            for comment in comments:
                database_id = comment.get("databaseId")
                if database_id is not None:
                    resolved_by_comment_id[int(database_id)] = is_resolved

        page_info = threads.get("pageInfo", {}) if threads else {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")

    return resolved_by_comment_id


def make_base_record(repo_meta, pull_request):
    user = pull_request.get("user") or {}
    return {
        "repo": repo_meta["repo"],
        "repo_url": repo_meta["repo_url"],
        "pr_id": pull_request.get("id"),
        "pr_number": pull_request.get("number"),
        "pr_url": pull_request.get("html_url", ""),
        "pr_title": pull_request.get("title", ""),
        "pr_description": pull_request.get("body", "") or "",
        "pr_state": pull_request.get("state", ""),
        "pr_created_at": pull_request.get("created_at", ""),
        "pr_updated_at": pull_request.get("updated_at", ""),
        "pr_closed_at": pull_request.get("closed_at", ""),
        "pr_merged_at": pull_request.get("merged_at", ""),
        "author": user.get("login", ""),
        "base_ref": (pull_request.get("base") or {}).get("ref", ""),
        "base_sha": (pull_request.get("base") or {}).get("sha", ""),
        "head_ref": (pull_request.get("head") or {}).get("ref", ""),
        "head_sha": (pull_request.get("head") or {}).get("sha", ""),
    }


def make_review_record(repo_meta, pull_request, review):
    record = make_base_record(repo_meta, pull_request)
    record.update(
        {
            "comment_type": "review",
            "review_id": review.get("id"),
            "comment_id": None,
            "review_state": review.get("state", ""),
            "review_comment": review.get("body", "") or "",
            "review_time": review.get("submitted_at", ""),
            "reviewer": get_nested_login(review),
            "file_path": "",
            "commit_id": review.get("commit_id", ""),
            "comment_line": None,
            "comment_side": "",
            "start_line": None,
            "old_code": "",
            "new_code": "",
            "diff": "",
            "is_resolved": None,
            "comment_url": review.get("html_url", ""),
        }
    )
    return record


def make_inline_comment_record(repo_meta, pull_request, comment, resolved_by_comment_id):
    diff_hunk = comment.get("diff_hunk", "") or ""
    old_code, new_code = parse_diff_hunk(diff_hunk)
    comment_id = comment.get("id")

    record = make_base_record(repo_meta, pull_request)
    record.update(
        {
            "comment_type": "inline_comment",
            "review_id": comment.get("pull_request_review_id"),
            "comment_id": comment_id,
            "review_state": "",
            "review_comment": comment.get("body", "") or "",
            "review_time": comment.get("created_at", ""),
            "reviewer": get_nested_login(comment),
            "file_path": comment.get("path", ""),
            "commit_id": comment.get("commit_id", ""),
            "comment_line": comment.get("line") or comment.get("original_line"),
            "comment_side": comment.get("side") or comment.get("original_side", ""),
            "start_line": comment.get("start_line") or comment.get("original_start_line"),
            "old_code": old_code,
            "new_code": new_code,
            "diff": diff_hunk,
            "is_resolved": resolved_by_comment_id.get(int(comment_id))
            if comment_id is not None
            else None,
            "comment_url": comment.get("html_url", ""),
        }
    )
    return record


def make_issue_comment_record(repo_meta, pull_request, comment):
    record = make_base_record(repo_meta, pull_request)
    record.update(
        {
            "comment_type": "pr_comment",
            "review_id": None,
            "comment_id": comment.get("id"),
            "review_state": "",
            "review_comment": comment.get("body", "") or "",
            "review_time": comment.get("created_at", ""),
            "reviewer": get_nested_login(comment),
            "file_path": "",
            "commit_id": "",
            "comment_line": None,
            "comment_side": "",
            "start_line": None,
            "old_code": "",
            "new_code": "",
            "diff": "",
            "is_resolved": None,
            "comment_url": comment.get("html_url", ""),
        }
    )
    return record


def collect_pull_request_records(
    client,
    repo_meta,
    pull_request,
    include_reviews=True,
    include_inline_comments=True,
    include_issue_comments=True,
):
    owner, repo = repo_meta["repo"].split("/", 1)
    pr_number = pull_request["number"]
    records = []

    if include_reviews:
        reviews_url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
        reviews = client.get_paginated(reviews_url)
        for review in reviews:
            if review.get("body") or review.get("state"):
                records.append(make_review_record(repo_meta, pull_request, review))

    if include_inline_comments:
        resolved_by_comment_id = fetch_review_thread_resolution_map(
            client,
            owner,
            repo,
            pr_number,
        )
        comments_url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/comments"
        inline_comments = client.get_paginated(comments_url)
        for comment in inline_comments:
            records.append(
                make_inline_comment_record(
                    repo_meta,
                    pull_request,
                    comment,
                    resolved_by_comment_id,
                )
            )

    if include_issue_comments:
        issue_comments_url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        issue_comments = client.get_paginated(issue_comments_url)
        for comment in issue_comments:
            records.append(make_issue_comment_record(repo_meta, pull_request, comment))

    return records


def collect_repository(
    client,
    repo_meta,
    writer,
    max_prs_per_repo=0,
    include_reviews=True,
    include_inline_comments=True,
    include_issue_comments=True,
):
    owner, repo = repo_meta["repo"].split("/", 1)
    pulls_url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls"
    pull_requests = client.get_paginated(
        pulls_url,
        params={
            "state": "all",
            "sort": "updated",
            "direction": "desc",
        },
        max_items=max_prs_per_repo,
    )

    repo_record_count = 0
    for index, pull_request in enumerate(pull_requests, start=1):
        print(
            f"[{repo_meta['repo']}] PR {index}/{len(pull_requests)} "
            f"#{pull_request.get('number')}"
        )
        records = collect_pull_request_records(
            client,
            repo_meta,
            pull_request,
            include_reviews=include_reviews,
            include_inline_comments=include_inline_comments,
            include_issue_comments=include_issue_comments,
        )
        for record in records:
            writer.write(record)
        repo_record_count += len(records)

    return repo_record_count


def parse_args():
    parser = argparse.ArgumentParser(
        description="Collect GitHub PR review, inline comment, and PR comment dataset."
    )
    parser.add_argument(
        "--input",
        default=str(DEFAULT_INPUT),
        help="Input .xlsx/.csv containing repository url or full_name columns.",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="Output dataset path.",
    )
    parser.add_argument(
        "--format",
        choices=["json", "jsonl"],
        default="json",
        help="Output format. Default is JSON array.",
    )
    parser.add_argument(
        "--github-token",
        default="",
        help="GitHub token used for API authentication.",
    )
    parser.add_argument(
        "--max-repos",
        type=int,
        default=0,
        help="Maximum repositories to process. 0 means all.",
    )
    parser.add_argument(
        "--max-prs-per-repo",
        type=int,
        default=0,
        help="Maximum PRs per repository. 0 means all.",
    )
    parser.add_argument(
        "--request-sleep",
        type=float,
        default=0.2,
        help="Sleep seconds between API requests.",
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
    return parser.parse_args()


def main():
    args = parse_args()
    repos = read_repositories(args.input)
    if args.max_repos:
        repos = repos[: args.max_repos]

    client = GitHubClient(
        token=args.github_token,
        request_sleep=args.request_sleep,
    )

    total_records = 0
    with DatasetWriter(args.output, output_format=args.format) as writer:
        for repo_index, repo_meta in enumerate(repos, start=1):
            print(f"Processing repo {repo_index}/{len(repos)}: {repo_meta['repo']}")
            try:
                repo_count = collect_repository(
                    client,
                    repo_meta,
                    writer,
                    max_prs_per_repo=args.max_prs_per_repo,
                    include_reviews=not args.skip_reviews,
                    include_inline_comments=not args.skip_inline_comments,
                    include_issue_comments=not args.skip_pr_comments,
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"Failed repo {repo_meta['repo']}: {exc}", file=sys.stderr)
                continue
            total_records += repo_count
            print(f"Collected {repo_count} records from {repo_meta['repo']}")

    print(f"Total records: {total_records}")
    print(f"Output: {Path(args.output).resolve()}")


if __name__ == "__main__":
    main()
