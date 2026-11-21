"""Download a pinned, verified Hy4-Flash Maya-S set; safe to rerun."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time

ROOT = Path(r"D:\Models\Hy4-Flash\Maya-S")
LOCK = threading.Lock()
STATE = {"pid": os.getpid(), "state": "running", "files": {}}


def status(name=None, **values):
    with LOCK:
        target = STATE if name is None else STATE["files"].setdefault(name, {})
        target.update(values)
        STATE["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        temp = ROOT / "download-status.tmp"
        temp.write_text(json.dumps(STATE, indent=2), encoding="utf-8")
        temp.replace(ROOT / "download-status.json")


def verify(path, entry):
    if not path.exists() or path.stat().st_size != entry["bytes"]:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest() == entry["sha256"]


def download(entry):
    name = entry["name"]
    final = ROOT / name
    part = ROOT / (name + ".part")
    status(name, state="checking", expected_bytes=entry["bytes"])
    if final.exists():
        if not verify(final, entry):
            raise RuntimeError(f"Existing file failed verification: {final}")
        status(name, state="verified")
        return
    if part.exists() and part.stat().st_size > entry["bytes"]:
        raise RuntimeError(f"Oversized partial file: {part}")
    if not part.exists() or part.stat().st_size != entry["bytes"]:
        env = os.environ.copy()
        for key in list(env):
            if key.lower() in ("http_proxy", "https_proxy", "all_proxy"):
                del env[key]
        with (ROOT / (name + ".curl.log")).open("ab") as log:
            args = ["curl.exe", "--noproxy", "*", "--location", "--fail",
                    "--show-error", "--continue-at", "-", "--retry", "30",
                    "--retry-all-errors", "--retry-delay", "10", "--connect-timeout", "30",
                    "--speed-limit", "1024", "--speed-time", "120", "--output", str(part), entry["url"]]
            child = subprocess.Popen(args, stdout=log, stderr=log, env=env,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            while child.poll() is None:
                status(name, state="downloading", curl_pid=child.pid,
                       downloaded_bytes=part.stat().st_size if part.exists() else 0)
                time.sleep(5)
            if child.returncode:
                raise RuntimeError(f"curl failed ({child.returncode}): {name}; see its curl log")
    status(name, state="verifying", downloaded_bytes=part.stat().st_size)
    if not verify(part, entry):
        raise RuntimeError(f"Size/SHA-256 mismatch: {part}")
    part.replace(final)
    status(name, state="verified", downloaded_bytes=entry["bytes"])
    print(f"Verified: {name}", flush=True)


if __name__ == "__main__":
    try:
        manifest = json.loads((ROOT / "download-manifest.json").read_text(encoding="utf-8-sig"))
        status(state="running", revision=manifest["revision"])
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(download, manifest["files"]))
        status(state="complete", message="All files passed size and SHA-256 verification")
    except Exception as exc:
        status(state="failed", message=str(exc))
        raise
