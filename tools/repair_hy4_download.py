"""Repair the ranges around the interrupted writes, then verify whole shards."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

ROOT = Path(r"D:\Models\Hy4-Flash\Maya-S")
STATE = {"pid": os.getpid(), "state": "running", "files": {}}


def status(**values):
    STATE.update(values)
    STATE["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with (ROOT / "repair-status.tmp").open("w", encoding="utf-8") as f:
        json.dump(STATE, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    (ROOT / "repair-status.tmp").replace(ROOT / "repair-status.json")


try:
    manifest = json.loads((ROOT / "download-manifest.json").read_text(encoding="utf-8-sig"))
    offsets = {"Hy4-Flash-Maya-S-IQ2_S-00001-of-00002.gguf": 32848345435,
               "Hy4-Flash-Maya-XS-IQ2_XXS-00002-of-00002.gguf": 32802797997}
    status(state="repairing")
    for entry in manifest["files"]:
        name = entry["name"]
        if name not in offsets:
            continue
        part = ROOT / (name + ".part")
        if not part.exists():
            raise RuntimeError(f"Missing partial file: {part}")
        start = (offsets[name] // (4 * 1024 * 1024) - 1) * (4 * 1024 * 1024)
        end = start + 12 * 1024 * 1024 - 1
        patch = ROOT / (name + ".repair-range")
        headers = ROOT / (name + ".repair-headers")
        status(file=name, state="fetching_repair_range", start=start, end=end)
        env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
        subprocess.run(["curl.exe", "--noproxy", "*", "--fail", "--location", "--show-error",
                        "--range", f"{start}-{end}", "--max-filesize", str(end-start+1),
                        "--connect-timeout", "20", "--max-time", "300", "--retry", "5",
                        "--dump-header", str(headers), "--output", str(patch),
                        entry["url"] + "?repair=" + str(int(time.time()))],
                       env=env, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        ranges = re.findall(r"content-range:\s*bytes\s+(\d+)-(\d+)/(\d+)", headers.read_text(), re.I)
        if not ranges or tuple(map(int, ranges[-1])) != (start, end, entry["bytes"]):
            raise RuntimeError("Unexpected Content-Range; partial file was not modified")
        data = patch.read_bytes()
        if len(data) != end-start+1:
            raise RuntimeError("Wrong repair size")
        backup = ROOT / (name + ".repair-backup")
        with part.open("r+b") as f:
            f.seek(start)
            if not backup.exists():
                backup.write_bytes(f.read(len(data)))
            f.seek(start)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        STATE["files"][name] = "range_repaired"
        status(state="range_repaired")
    for entry in manifest["files"]:
        name = entry["name"]
        final = ROOT / name
        path = final if final.exists() else ROOT / (name + ".part")
        status(state="verifying", file=name)
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(8*1024*1024), b""):
                digest.update(block)
        if path.stat().st_size != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
            raise RuntimeError(f"Whole-file SHA-256 still differs: {name}")
        if path != final:
            path.replace(final)
        STATE["files"][name] = "verified"
        status()
        print("Verified:", name, flush=True)
    status(state="complete", message="All files passed whole-file SHA-256 verification")
except Exception as exc:
    status(state="failed", message=str(exc))
    raise
