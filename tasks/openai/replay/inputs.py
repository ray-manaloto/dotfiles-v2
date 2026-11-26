"""Read immutable merged PR patches through the production patch checks."""

import json
import os
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode

import review

from dotfiles_py.logging import get_logger

REPOS = ("ray-manaloto/dotfiles-v2", "ray-manaloto/knowledge-base-v2")
LOGGER = get_logger("openai.replay")


def github(path, *, query=None, paginate=False, raw=False):
    if query:
        path += "?" + urlencode(query)
    args = ["gh", "api", "--method", "GET"]
    if paginate:
        args.extend(["--paginate", "--slurp"])
    if raw:
        args.extend(["-H", "Accept: application/vnd.github.raw+json"])
    result = subprocess.run([*args, path], capture_output=True, check=False, timeout=90)
    if result.returncode:
        if b"HTTP 404" in result.stderr:
            raise FileNotFoundError("GitHub returned 404.")
        raise RuntimeError("GitHub GET failed. Check gh authentication and API limits.")
    return result.stdout if raw else json.loads(result.stdout)


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def merged_inventory():
    for repo in REPOS:
        pages = github(
            "search/issues",
            query={"q": f"repo:{repo} is:pr is:merged", "per_page": 100},
            paginate=True,
        )
        numbers = {item["number"] for page in pages for item in page["items"]}
        if (
            any(page["incomplete_results"] for page in pages)
            or len(numbers) != pages[0]["total_count"]
        ):
            raise RuntimeError("GitHub merged PR inventory was incomplete.")
        yield from ((repo, number) for number in sorted(numbers))


def collect_one(cache, repo, number):
    identifier = f"{REPOS.index(repo)}-{number}"
    path = cache / f"pr-{identifier}.json"
    if path.exists():
        return read_json(path)
    endpoint = f"repos/{repo}/pulls/{number}"
    pr = github(endpoint)
    if not (
        pr["merged"]
        and pr["base"]["repo"]["full_name"] == repo
        and all(review.SHA.fullmatch(pr[key]) for key in ("merge_commit_sha",))
        and all(review.SHA.fullmatch(pr[key]["sha"]) for key in ("base", "head"))
    ):
        raise RuntimeError("GitHub returned unexpected merged PR metadata.")
    row = {
        "id": identifier,
        "repo": repo,
        "head": pr["head"]["sha"],
        "merge": pr["merge_commit_sha"],
        "merged_at": pr["merged_at"],
        "default_branch": pr["base"]["repo"]["default_branch"],
    }
    count = pr["changed_files"]
    if count > review.GITHUB_FILE_LIMIT:
        row["decline"] = "github-file-limit"
    else:
        # GitHub's merged PR files endpoint provides the same merge-base-to-head
        # patch view as production. Use the captured merged SHA for follow-up.
        files = list(review.pr_files(lambda url, _: github(url), endpoint, None, count))
        try:
            row["data"], skipped = review.patch_data(files)
            row["skipped"] = dict(Counter(item["reason"] for item in skipped))
        except review.ReviewError as exc:
            row["decline"] = str(exc)
            row["skipped"] = dict(
                Counter(
                    reason
                    for item in files
                    if (
                        reason := review.skip_reason(
                            item["filename"], item.get("previous_filename", ""), item
                        )
                    )
                )
            )
    write_json(path, row)
    return row


def collect(cache: Path, *, cached_only=False):
    manifest = cache / "inventory.json"
    if cached_only:
        if not manifest.exists():
            raise RuntimeError("Collect GitHub inputs first with the prepare phase.")
        return [
            read_json(cache / f"pr-{REPOS.index(repo)}-{number}.json")
            for repo, number in read_json(manifest)
        ]
    refs = read_json(manifest) if manifest.exists() else list(merged_inventory())
    if not manifest.exists():
        write_json(manifest, refs)
    with ThreadPoolExecutor(max_workers=4) as workers:
        rows = list(workers.map(lambda ref: collect_one(cache, *ref), refs))
    LOGGER.info("github_inventory_cached", prs=len(rows))
    return rows
