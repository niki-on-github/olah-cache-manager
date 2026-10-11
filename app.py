"""olah-cache-manager: web UI to list and delete olah HuggingFace mirror cache entries.

A small FastAPI app that runs alongside an olah mirror instance and operates
directly on the olah cache tree (``repos-path``). It reuses olah's own cache
eviction primitives (``olah.utils.disk_utils``) so deletions match olah's
semantics exactly:

* whole on-disk cache *entries* (directories containing ``meta.bin``) are removed
  -- never individual ``blocks/block_*.bin`` files, which would leave ``meta.bin``
  and ``chunks.crc`` pointing at missing blocks (a corrupt entry);
* each entry is removed only after taking a non-blocking exclusive lock on its
  ``meta.lock``, so an entry olah is currently opening/creating/resizing is
  skipped rather than corrupted;
* loose cached API responses (``*.json`` plus their ``*.json.body`` sidecar) are
  removed together.

Deletion is disabled unless ``MANAGER_DELETE_ENABLED=true``.
"""

from __future__ import annotations

import datetime
import os
import shutil
from typing import Dict, List, Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from olah.utils.disk_utils import (
    _try_evict_entry,
    collect_cache_units,
    convert_bytes_to_human_readable,
)

REPOS_PATH = os.environ.get("MANAGER_REPOS_PATH", "/data/repos")
DELETE_ENABLED = os.environ.get("MANAGER_DELETE_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
REPO_TYPES = ("models", "datasets", "spaces")

APP_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="olah cache manager", docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=os.path.join(APP_DIR, "templates"))


def _scan_repo(repo_dir: str) -> Dict:
    """Total size, last access, entry count and file count for one repo dir.

    "Last access" is the newest ``meta.lock`` mtime among the repo's cache
    entries, which is the same signal olah's LRU eviction sorts on.
    """
    size = 0
    files = 0
    entries = 0
    last = 0.0
    for dirpath, _dirnames, filenames in os.walk(repo_dir):
        if "meta.bin" in filenames:
            entries += 1
            lock = os.path.join(dirpath, "meta.lock")
            marker = lock if os.path.exists(lock) else os.path.join(dirpath, "meta.bin")
            try:
                last = max(last, os.stat(marker).st_mtime)
            except OSError:
                pass
        for name in filenames:
            files += 1
            try:
                size += os.stat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return {"size": size, "mtime": last, "entries": entries, "files": files}


def _human_time(ts: float) -> str:
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def list_repos() -> List[Dict]:
    """Enumerate cached repos under ``<repos>/api/{models,datasets,spaces}/<org>/<repo>``."""
    repos: List[Dict] = []
    for rtype in REPO_TYPES:
        base = os.path.join(REPOS_PATH, "api", rtype)
        if not os.path.isdir(base):
            continue
        for org in sorted(os.listdir(base)):
            org_dir = os.path.join(base, org)
            if not os.path.isdir(org_dir):
                continue
            for repo in sorted(os.listdir(org_dir)):
                repo_dir = os.path.join(org_dir, repo)
                if not os.path.isdir(repo_dir):
                    continue
                stats = _scan_repo(repo_dir)
                repos.append(
                    {
                        "type": rtype,
                        "org": org,
                        "repo": repo,
                        "path": f"{org}/{repo}",
                        "size": stats["size"],
                        "size_h": convert_bytes_to_human_readable(stats["size"]),
                        "mtime_h": _human_time(stats["mtime"]),
                        "entries": stats["entries"],
                        "files": stats["files"],
                    }
                )
    return repos


def _safe_repo_dir(rtype: str, org: str, repo: str) -> Optional[str]:
    """Resolve a repo dir, rejecting anything that escapes ``<repos>/api/<type>``."""
    if rtype not in REPO_TYPES:
        return None
    for part in (org, repo):
        if not part or part in (".", "..") or "/" in part or "\\" in part or "\x00" in part:
            return None
    base = os.path.abspath(os.path.join(REPOS_PATH, "api", rtype))
    target = os.path.abspath(os.path.join(base, org, repo))
    if os.path.commonpath([base, target]) != base or target == base:
        return None
    return target


def _delete_repo(repo_dir: str) -> Dict:
    """Evict every cache unit under ``repo_dir`` using olah's lock-safe helpers."""
    _total, units = collect_cache_units(REPOS_PATH)
    prefix = repo_dir + os.sep
    freed = 0
    skipped = 0
    for unit in units:
        path = os.path.abspath(unit["path"])
        if not (path == repo_dir or path.startswith(prefix)):
            continue
        if unit["kind"] == "entry":
            if _try_evict_entry(unit["path"]):
                freed += unit["size"]
            else:
                skipped += 1
        else:
            for target in (unit["path"], unit["path"] + ".body"):
                try:
                    os.remove(target)
                except OSError:
                    pass
            freed += unit["size"]
    # Only clean up the now-empty tree when nothing was busy; a skipped entry must
    # stay in place (removing it mid-download would corrupt the in-flight cache).
    if skipped == 0:
        shutil.rmtree(repo_dir, ignore_errors=True)
    return {
        "freed": freed,
        "freed_h": convert_bytes_to_human_readable(freed),
        "skipped_busy": skipped,
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    repos = list_repos()
    total = sum(r["size"] for r in repos)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "repos": repos,
            "total_h": convert_bytes_to_human_readable(total),
            "delete_enabled": DELETE_ENABLED,
            "repos_path": REPOS_PATH,
        },
    )


@app.get("/healthz")
def healthz():
    return {"ok": True, "repos_path": REPOS_PATH, "delete_enabled": DELETE_ENABLED}


@app.post("/delete")
def delete(rtype: str = Form(...), org: str = Form(...), repo: str = Form(...)):
    if not DELETE_ENABLED:
        return JSONResponse(
            {"ok": False, "error": "deletion disabled (set MANAGER_DELETE_ENABLED=true)"},
            status_code=403,
        )
    repo_dir = _safe_repo_dir(rtype, org, repo)
    if repo_dir is None or not os.path.isdir(repo_dir):
        return JSONResponse(
            {"ok": False, "error": "unknown or invalid repository"}, status_code=400
        )
    result = _delete_repo(repo_dir)
    result["ok"] = True
    result["repo"] = f"{org}/{repo}"
    return result
