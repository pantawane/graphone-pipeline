"""Final crawl: news, jobs, export and audit in one go. Run on a weekday, as close to submission as possible."""
import subprocess
import sys
import time

use_llm = "--llm" in sys.argv
steps = [
    ("news", [sys.executable, "run_news.py"] + (["--llm"] if use_llm else [])),
    ("jobs", [sys.executable, "run_jobs.py"]),
    ("export", [sys.executable, "export_sheet.py"]),
]
if "--skip-audit" not in sys.argv:
    steps.append(("audit", [sys.executable, "audit_final.py", "--check-urls"]))

t0 = time.time()
for name, cmd in steps:
    print(f"\n{'=' * 20} {name} {'=' * 20}", flush=True)
    t = time.time()
    code = subprocess.run(cmd).returncode
    print(f"[{name}] finished in {time.time() - t:.0f}s with exit code {code}", flush=True)
    if code != 0 and name != "audit":
        sys.exit(f"stopped: {name} failed")
print(f"\ntotal {time.time() - t0:.0f}s")