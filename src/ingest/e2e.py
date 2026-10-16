"""Headless end-to-end run: engines, download, conversion, verification.

Used on CI against the frozen Windows build, and handy for diagnosing a PC:
    Ingest.exe --e2e https://example.com/video --report result.json
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path


def run_e2e(url: str, profile: str = "", report_path: str | None = None, timeout: float = 1200) -> int:
    from .core.engines import Engines
    from .core.library import Library
    from .core.queue import Events, QueueManager
    from .core.settings import SettingsStore, copy_options

    store = SettingsStore()
    s = store.settings
    s.skip_existing = False
    out_dir = Path(tempfile.mkdtemp(prefix="ingest-e2e-"))
    s.output.output_dir = str(out_dir)
    engines = Engines(lambda: store.settings)
    report: dict = {"url": url, "ok": False, "steps": []}

    def step(msg: str) -> None:
        report["steps"].append(f"{time.strftime('%H:%M:%S')} {msg}")
        if sys.stdout is not None:
            print(msg, flush=True)

    for key in engines.missing_required():
        step(f"installing {key}")
        try:
            engines.install(key)
        except Exception as e:
            step(f"install {key} failed: {e}")
    report["engines"] = {k: engines.version(k) for k in ("ytdlp", "ffmpeg", "deno")}
    step(f"engines {report['engines']}")

    seen = {}

    def updated(job, fields):
        key = (job.status, job.stage, job.stage_text)
        if seen.get(job.id) != key:
            seen[job.id] = key
            step(f"{job.status} {job.stage} {job.stage_text}")

    qm = QueueManager(store, engines, Library(Path(tempfile.mkdtemp()) / "lib.sqlite3"), Events(updated=updated),
                      state_path=Path(tempfile.mkdtemp()) / "queue.json")
    qm.start_capability_probe()
    caps = qm.caps()
    report["caps"] = caps.__dict__
    opts = copy_options(s.output, **({"profile": profile} if profile else {}))
    qm.add_urls([url], opts)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if qm.jobs and all(j.status in ("done", "failed", "canceled") for j in qm.jobs):
            break
        time.sleep(0.5)
    jobs = []
    for j in qm.jobs:
        jobs.append({"status": j.status, "title": j.title, "outputs": j.outputs, "plan": j.plan_summary,
                     "reasons": j.plan_reasons, "error": j.error, "warnings": j.warnings, "report": j.report,
                     "log": Path(j.log_path).read_text(encoding="utf-8", errors="replace")[-6000:]
                     if j.log_path and Path(j.log_path).exists() else ""})
    report["jobs"] = jobs
    report["ok"] = bool(jobs) and all(j["status"] == "done" and (j["report"] or {}).get("premiere") for j in jobs)
    qm.shutdown(2)
    text = json.dumps(report, indent=2, default=str)
    if report_path:
        Path(report_path).write_text(text, encoding="utf-8")
    if sys.stdout is not None:
        print(text)
    return 0 if report["ok"] else 1
