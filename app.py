"""olah-cache-manager: web UI to list and delete olah HuggingFace mirror cache entries.

A small FastAPI app that runs alongside an olah mirror instance and operates
directly on the olah cache tree (``repos-path``). It reuses olah's own cache
eviction primitive (``olah.utils.disk_utils._try_evict_entry``) so deletions
match olah's semantics exactly:

* whole on-disk cache *entries* (directories containing ``meta.bin``) are removed
  -- never individual ``blocks/block_*.bin`` files, which would leave ``meta.bin``
  and ``chunks.crc`` pointing at missing blocks (a corrupt entry);
* each entry is removed only after taking a non-blocking exclusive lock on its
  ``meta.lock``, so an entry olah is currently opening/creating/resizing is
  skipped rather than corrupted;
* loose cached API responses (``*.json`` plus their ``*.json.body`` sidecar) are
  removed together.

olah stores data in two per-repo trees under ``repos-path``:

* ``api/{models,datasets,spaces}/<org>/<repo>``  -- cached API metadata (small);
* ``files/{models,datasets,spaces}/<org>/<repo>`` -- the file/block cache (the bulk).

Both are scanned so the reported size reflects the real disk usage.

Deletion is disabled unless ``MANAGER_DELETE_ENABLED=true``.
"""

from __future__ import annotations

import datetime
import os
import shutil
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from olah.utils.disk_utils import _try_evict_entry, convert_bytes_to_human_readable

REPOS_PATH = os.environ.get("MANAGER_REPOS_PATH", "/data/repos")
DELETE_ENABLED = os.environ.get("MANAGER_DELETE_ENABLED", "false").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
REPO_TYPES = ("models", "datasets", "spaces")
REPO_TREES = ("api", "files")

APP_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="olah cache manager", docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=os.path.join(APP_DIR, "templates"))

Key = Tuple[str, str, str]


def _scan_dir(path: str) -> Tuple[int, float, int, int]:
    """(size, last_access_mtime, entry_count, file_count) for a directory tree.

    ``last_access`` is the newest mtime of any file. olah bumps the ``meta.lock``
    mtime of a cache entry when it is opened (the same signal its LRU eviction
    sorts on), and touches cached API ``*.json`` files on read, so the newest
    mtime is a faithful last-access proxy for the whole repo.
    """
    size = 0
    files = 0
    entries = 0
    last = 0.0
    for dirpath, _dirnames, filenames in os.walk(path):
        if "meta.bin" in filenames:
            entries += 1
        for name in filenames:
            try:
                st = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue
            size += st.st_size
            files += 1
            if st.st_mtime > last:
                last = st.st_mtime
    return size, last, entries, files


def _human_time(ts: float) -> str:
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def list_repos() -> List[Dict]:
    """Aggregate cached repos across the ``api`` and ``files`` trees."""
    agg: Dict[Key, Dict] = {}
    for tree in REPO_TREES:
        for rtype in REPO_TYPES:
            base = os.path.join(REPOS_PATH, tree, rtype)
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
                    size, last, entries, files = _scan_dir(repo_dir)
                    entry = agg.setdefault(
                        (rtype, org, repo),
                        {"size": 0, "mtime": 0.0, "entries": 0, "files": 0},
                    )
                    entry["size"] += size
                    entry["entries"] += entries
                    entry["files"] += files
                    if last > entry["mtime"]:
                        entry["mtime"] = last

    repos = []
    for (rtype, org, repo), e in agg.items():
        repos.append(
            {
                "type": rtype,
                "org": org,
                "repo": repo,
                "path": f"{org}/{repo}",
                "size": e["size"],
                "size_h": convert_bytes_to_human_readable(e["size"]),
                "mtime": e["mtime"],
                "mtime_h": _human_time(e["mtime"]),
                "entries": e["entries"],
                "files": e["files"],
            }
        )
    repos.sort(key=lambda r: r["size"], reverse=True)
    return repos


def _valid_key(rtype: str, org: str, repo: str) -> bool:
    if rtype not in REPO_TYPES:
        return False
    for part in (org, repo):
        if not part or part in (".", "..") or "/" in part or "\\" in part or "\x00" in part:
            return False
    return True


def _dir_size(path: str) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


def _evict_tree(repo_dir: str) -> Tuple[int, int]:
    """Evict all cache units under ``repo_dir``. Returns (freed_bytes, skipped_busy)."""
    if not os.path.isdir(repo_dir):
        return 0, 0
    freed = 0
    skipped = 0
    for dirpath, dirnames, filenames in os.walk(repo_dir, topdown=True):
        if "meta.bin" in filenames:
            size = _dir_size(dirpath)
            if _try_evict_entry(dirpath):
                freed += size
            else:
                skipped += 1
            dirnames[:] = []
            continue
        for name in filenames:
            if name.endswith(".json") or name.endswith(".body"):
                target = os.path.join(dirpath, name)
                try:
                    freed += os.path.getsize(target)
                    os.remove(target)
                except OSError:
                    pass
    # Only drop the now-empty tree when nothing was busy; a skipped entry must
    # stay in place (removing it mid-download would corrupt the in-flight cache).
    if skipped == 0:
        shutil.rmtree(repo_dir, ignore_errors=True)
    return freed, skipped


def _delete_repo(rtype: str, org: str, repo: str) -> Tuple[int, int]:
    freed = 0
    skipped = 0
    for tree in REPO_TREES:
        f, s = _evict_tree(os.path.join(REPOS_PATH, tree, rtype, org, repo))
        freed += f
        skipped += s
    return freed, skipped


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
    if not _valid_key(rtype, org, repo):
        return JSONResponse(
            {"ok": False, "error": "unknown or invalid repository"}, status_code=400
        )
    exists = any(
        os.path.isdir(os.path.join(REPOS_PATH, tree, rtype, org, repo))
        for tree in REPO_TREES
    )
    if not exists:
        return JSONResponse(
            {"ok": False, "error": "unknown or invalid repository"}, status_code=400
        )
    freed, skipped = _delete_repo(rtype, org, repo)
    return {
        "ok": True,
        "repo": f"{org}/{repo}",
        "freed": freed,
        "freed_h": convert_bytes_to_human_readable(freed),
        "skipped_busy": skipped,
    }
