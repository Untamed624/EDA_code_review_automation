import csv
import argparse
import time
from pathlib import Path

try:
    import requests
except ModuleNotFoundError:
    requests = None

# =========================
# Output
# =========================
BASE_DIR = Path(__file__).resolve().parent
TARGET_LANGUAGE = "chisel"  # 修改这里，例如 "VHDL" / "SystemVerilog"
OUTPUT_CSV = BASE_DIR / f"github_{TARGET_LANGUAGE.lower()}_projects.csv"
OUTPUT_XLSX = BASE_DIR / f"github_{TARGET_LANGUAGE.lower()}_projects.xlsx"

HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "metadata-language-crawler",
    "X-GitHub-Api-Version": "2022-11-28",
}

session = requests.Session() if requests else None
if session:
    session.headers.update(HEADERS)


def configure_github_token(github_token):
    if session is None:
        return
    token = (github_token or "").strip()
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    else:
        session.headers.pop("Authorization", None)

# =========================
# Search config
# =========================
MAX_PAGES = 20
MAX_TOTAL_ITEMS = 3000
# MIN_STARS = 1000
# MIN_PR_COUNT = 500
TOP_N_LANGUAGES = 3

SORT = "stars"
ORDER = "desc"


def require_requests():
    if session is None:
        raise RuntimeError("requests is required. Install it with: pip install requests")


def request_json(url, params=None, default=None):
    require_requests()
    try:
        response = session.get(url, params=params, timeout=30)
        if response.status_code == 403:
            print("API rate limit hit or permission denied. Please pass --github-token and retry.")
            return default
        if response.status_code == 422:
            print(f"Invalid or limited search query: {params}")
            return default
        if response.status_code != 200:
            print(f"API Error: {response.status_code} {url}")
            return default
        return response.json()
    except requests.RequestException as exc:
        print(f"Request error: {exc}")
        return default


def search_repositories(language, max_pages):
    url = "https://api.github.com/search/repositories"
    results = []
    for page in range(1, max_pages + 1):
        params = {
            # "q": f"language:{language} stars:>={MIN_STARS}",
            "q": f"language:{language}",
            "sort": SORT,
            "order": ORDER,
            "per_page": 100,
            "page": page,
        }
        data = request_json(url, params=params, default={})
        items = data.get("items", []) if isinstance(data, dict) else []
        if not items:
            break

        print(f"language={language!r}, page={page}, items={len(items)}")
        results.extend(items)

        if len(items) < 100:
            break
        if len(results) >= MAX_TOTAL_ITEMS:
            break
        time.sleep(1.0)

    return results[:MAX_TOTAL_ITEMS]


def get_languages(owner, repo):
    url = f"https://api.github.com/repos/{owner}/{repo}/languages"
    data = request_json(url, default={})
    return data if isinstance(data, dict) else {}


def get_pr_count(owner, repo):
    url = "https://api.github.com/search/issues"
    params = {
        "q": f"repo:{owner}/{repo} is:pr",
        "per_page": 1,
    }
    data = request_json(url, params=params, default={})
    if not isinstance(data, dict):
        return 0
    return int(data.get("total_count", 0) or 0)


def normalize(value):
    if value is None:
        return ""
    return str(value).strip().lower()


def get_top_languages_list(languages_map, top_n=TOP_N_LANGUAGES):
    if not languages_map:
        return []
    sorted_languages = sorted(languages_map.items(), key=lambda entry: entry[1], reverse=True)[:top_n]
    return [language for language, _ in sorted_languages]


def format_top_languages(languages_map, top_n=TOP_N_LANGUAGES):
    if not languages_map:
        return ""
    total = sum(languages_map.values())
    if total <= 0:
        return ""

    sorted_languages = sorted(languages_map.items(), key=lambda entry: entry[1], reverse=True)[:top_n]

    parts = []
    for language, bytes_count in sorted_languages:
        ratio = bytes_count / total * 100
        parts.append(f"{language}({ratio:.1f}%)")
    return ",".join(parts)


def is_primary_language_match(item, target_language):
    return normalize(item.get("language")) == normalize(target_language)


def build_row(item, target_language):
    full_name = item["full_name"]
    owner, repo = full_name.split("/", 1)

    languages_map = get_languages(owner, repo)
    pr_count = get_pr_count(owner, repo)
    top3_languages = format_top_languages(languages_map, top_n=TOP_N_LANGUAGES)
    primary_language_matches_target = is_primary_language_match(item, target_language)

    return {
        "repo_name": item.get("name", ""),
        "full_name": full_name,
        "target_language": target_language,
        "primary_language": item.get("language", ""),
        "primary_language_matches_target": primary_language_matches_target,
        "top3_languages": top3_languages,
        "topics": ",".join(item.get("topics", [])),
        "url": item.get("html_url", ""),
        "stars": item.get("stargazers_count", 0),
        "forks_count": item.get("forks_count", 0),
        "pr_count": pr_count,
    }


def write_csv(rows, output_path=OUTPUT_CSV):
    fieldnames = [
        "repo_name",
        "full_name",
        "target_language",
        "primary_language",
        "primary_language_matches_target",
        "top3_languages",
        "topics",
        "url",
        "stars",
        "forks_count",
        "pr_count",
    ]
    with output_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_optional_xlsx(rows):
    try:
        import pandas as pd
    except ModuleNotFoundError:
        print("pandas is not installed, skipping xlsx export.")
        return
    pd.DataFrame(rows).to_excel(OUTPUT_XLSX, index=False)
    print(f"Output: {OUTPUT_XLSX}")


def fetch_language_projects(target_language=TARGET_LANGUAGE):
    require_requests()

    items = search_repositories(target_language, max_pages=MAX_PAGES)

    repos = {}
    for item in items:
        full_name = item.get("full_name")
        if full_name and full_name not in repos:
            repos[full_name] = item

    rows = []
    for index, item in enumerate(repos.values(), start=1):
        print(f"Fetching metadata {index}/{len(repos)}: {item['full_name']}")

        # if item.get("stargazers_count", 0) < MIN_STARS:
        #     continue
        # if not is_primary_language_match(item, target_language):
        #     continue

        row = build_row(item, target_language)
        # if int(row.get("pr_count", 0) or 0) >= MIN_PR_COUNT:
        #     rows.append(row)
        rows.append(row)

        time.sleep(0.4)

    rows.sort(key=lambda row: int(row.get("stars", 0) or 0), reverse=True)
    return rows


def parse_args():
    parser = argparse.ArgumentParser(
        description="Collect GitHub repositories by target language."
    )
    parser.add_argument(
        "--github-token",
        default="",
        help="GitHub token used for API authentication.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    configure_github_token(args.github_token)
    rows = fetch_language_projects(TARGET_LANGUAGE)
    write_csv(rows)
    write_optional_xlsx(rows)
    print(f"Target language: {TARGET_LANGUAGE}")
    print(f"Filtered project count: {len(rows)}")
    print(f"Total PR count: {sum(int(row.get('pr_count', 0) or 0) for row in rows)}")
    print(f"Output: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
