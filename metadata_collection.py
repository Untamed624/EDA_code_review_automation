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
OUTPUT_CSV = BASE_DIR / "github_hardware_language_projects.csv"
OUTPUT_XLSX = BASE_DIR / "github_hardware_language_projects.xlsx"

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
SEARCH_QUERIES = [
    "language:Verilog",
    "language:SystemVerilog",
    "language:VHDL",
    "language:Bluespec",
    "language:Scala",
    "language:Tcl",
    "language:Smarty",
    
    "topic:verilog",
    "topic:systemverilog",
    "topic:vhdl",
    "topic:bluespec",
    "topic:chisel",
    "topic:spinalhdl",
    "topic:amaranth",
    "topic:nmigen",
    "topic:circt",
    "topic:fpga",
    "topic:asic",
    "topic:hdl",
    "topic:rtl",
    "topic:risc-v",
    "topic:riscv",
    "topic:soc",
    "topic:softcore",
    "topic:soft-core",
    "topic:processor",
    "topic:cpu-core",
    "topic:cpucore",
    "topic:cpu",
    "topic:vector",
    "topic:ara",
    "topic:rvv",
    "topic:scala",
    "topic:riscv-boom",
    "topic:boom",
    "topic:rocket-chip",
    "topic:riscv32imfc",
    "topic:systemverilog-hdl",
    "topic:rv64gc",
    "topic:ariane",
    "topic:nuclei",
    "topic:core",
    "topic:e203",
    "topic:hummingbird",
    "topic:rv32",
    "topic:openrisc",
    "topic:vhdl",

]

MAX_PAGES_PER_QUERY = 20
MAX_TOTAL_ITEMS = 3000
MIN_STARS = 500
MIN_PR_COUNT = 200

SORT = "stars"
ORDER = "desc"

REQUIRED_HDL_LANGUAGES = {
    "verilog",
    "systemverilog",
    "vhdl",
    "bluespec",
    "scala",
    "smarty",
}


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


def search_repositories(query, max_pages):
    url = "https://api.github.com/search/repositories"
    results = []
    for page in range(1, max_pages + 1):
        params = {
            "q": f"{query} stars:>={MIN_STARS}",
            "sort": SORT,
            "order": ORDER,
            "per_page": 100,
            "page": page,
        }
        data = request_json(url, params=params, default={})
        items = data.get("items", []) if isinstance(data, dict) else []
        if not items:
            break

        print(f"query={query!r}, page={page}, items={len(items)}")
        results.extend(items)

        if len(items) < 100:
            break
        time.sleep(1.0)
    return results


def get_languages(owner, repo):
    url = f"https://api.github.com/repos/{owner}/{repo}/languages"
    data = request_json(url, default={})
    return data if isinstance(data, dict) else {}


def get_pr_count(owner, repo):
    # Use GitHub search API total_count to estimate PR volume for a repository.
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


def get_top_languages_list(languages_map, top_n=3):
    if not languages_map:
        return []
    sorted_languages = sorted(
        languages_map.items(), key=lambda entry: entry[1], reverse=True
    )[:top_n]
    return [language for language, _ in sorted_languages]


def format_top_languages(languages_map, top_n=3):
    if not languages_map:
        return ""
    total = sum(languages_map.values())
    if total <= 0:
        return ""

    sorted_languages = sorted(
        languages_map.items(), key=lambda entry: entry[1], reverse=True
    )[:top_n]

    parts = []
    for language, bytes_count in sorted_languages:
        ratio = bytes_count / total * 100
        parts.append(f"{language}({ratio:.1f}%)")
    return ",".join(parts)


def top_languages_contains_language(top_languages_text, target_language):
    if not top_languages_text:
        return False
    target = normalize(target_language)
    for part in str(top_languages_text).split(","):
        language_name = normalize(part.split("(", 1)[0])
        if language_name == target:
            return True
    return False


def has_required_hdl_in_top3(languages_map):
    top3_languages = get_top_languages_list(languages_map, top_n=3)
    normalized_top3 = {normalize(language) for language in top3_languages if normalize(language)}
    return bool(normalized_top3 & REQUIRED_HDL_LANGUAGES)


def build_row(item):
    full_name = item["full_name"]
    owner, repo = full_name.split("/", 1)

    languages_map = get_languages(owner, repo)
    pr_count = get_pr_count(owner, repo)
    top3_languages = format_top_languages(languages_map, top_n=3)
    top3_has_required_hdl = has_required_hdl_in_top3(languages_map)

    return {
        "repo_name": item.get("name", ""),
        "full_name": full_name,
        "top3_languages": top3_languages,
        "top3_has_required_hdl": top3_has_required_hdl,
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
        "top3_languages",
        "top3_has_required_hdl",
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


def fetch_hardware_language_projects(max_total_items=MAX_TOTAL_ITEMS):
    require_requests()

    repos = {}
    for query in SEARCH_QUERIES:
        items = search_repositories(query, max_pages=MAX_PAGES_PER_QUERY)
        for item in items:
            full_name = item.get("full_name")
            if full_name and full_name not in repos:
                repos[full_name] = item
            if len(repos) >= max_total_items:
                break
        if len(repos) >= max_total_items:
            break
        time.sleep(1.0)

    rows = []
    for index, item in enumerate(repos.values(), start=1):
        print(f"Fetching metadata {index}/{len(repos)}: {item['full_name']}")

        if item.get("stargazers_count", 0) < MIN_STARS:
            continue

        row = build_row(item)
        if row["top3_has_required_hdl"] and int(row.get("pr_count", 0) or 0) >= MIN_PR_COUNT:
            rows.append(row)

        time.sleep(0.4)

    # Final post-filter: remove repositories whose top3 languages include Assembly.
    rows = [
        row for row in rows
        if not top_languages_contains_language(row.get("top3_languages", ""), "assembly")
    ]

    rows.sort(key=lambda row: int(row.get("stars", 0) or 0), reverse=True)
    return rows


def parse_args():
    parser = argparse.ArgumentParser(
        description="Collect and filter GitHub hardware-language repositories."
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
    rows = fetch_hardware_language_projects()
    write_csv(rows)
    write_optional_xlsx(rows)
    print(f"Filtered project count: {len(rows)}")
    print(f"Total PR count: {sum(int(row.get('pr_count', 0) or 0) for row in rows)}")
    print(f"Output: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
