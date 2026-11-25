"""Pre-submission checks for every deliverable. Run: python preflight.py"""
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

results: list[tuple[str, str, str]] = []


def add(level: str, name: str, detail: str = "") -> None:
    results.append((level, name, detail))
    print(f"[{level:4}] {name}" + (f": {detail}" if detail else ""), flush=True)


def sh(*cmd: str) -> tuple[int, str]:
    try:
        p = subprocess.run(list(cmd), capture_output=True, text=True, timeout=300)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"


def main() -> int:
    con = sqlite3.connect("data/pipeline.db")
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0]

    print("== data minimums (Phase I)")
    n_start, n_prod = q("SELECT COUNT(*) FROM records WHERE record_type='STARTUP'"), q(
        "SELECT COUNT(*) FROM records WHERE record_type='PRODUCT'")
    n_pap = q("SELECT COUNT(*) FROM records WHERE record_type='RESEARCH_PAPER' "
              "AND json_extract(data_json,'$.content.github_stars') IS NOT NULL")
    add("PASS" if n_start >= 1000 else "FAIL", "startups >= 1,000", f"{n_start:,}")
    add("PASS" if n_prod >= 1000 else "FAIL", "products >= 1,000", f"{n_prod:,}")
    add("PASS" if n_pap >= 1000 else "FAIL", "papers with GitHub stars >= 1,000", f"{n_pap:,}")

    print("\n== export")
    xlsx = Path("output/frontier_atlas_output.xlsx")
    if not xlsx.exists():
        add("FAIL", "export file exists", "run python export_sheet.py")
    else:
        age_h = (datetime.now().timestamp() - xlsx.stat().st_mtime) / 3600
        add("PASS" if age_h < 12 else "WARN", "export is recent", f"{age_h:.1f} h old (news and jobs must be inside 24 h)")
        try:
            from openpyxl import load_workbook
            wb = load_workbook(xlsx, read_only=True)
            want = ["Startups", "Products", "Research Papers", "Jobs", "News", "Entity Mapping Log"]
            add("PASS" if wb.sheetnames == want else "FAIL", "6 tab names, exact and in order", ", ".join(wb.sheetnames))
            rows = {ws.title: max(ws.max_row - 1, 0) for ws in wb.worksheets}
            add("PASS" if rows.get("Products", 0) == n_prod else "WARN", "Products tab matches the database",
                f"tab {rows.get('Products')} vs database {n_prod}")
            add("PASS" if rows.get("Startups", 0) == n_start else "WARN", "Startups tab matches the database",
                f"tab {rows.get('Startups')} vs database {n_start}")
            for tab in ("Jobs", "News"):
                add("PASS" if rows.get(tab, 0) > 0 else "WARN", f"{tab} tab has rows", str(rows.get(tab)))
        except Exception as exc:
            add("WARN", "could not read the workbook", f"{type(exc).__name__}: {exc}")

    print("\n== documents")
    readme, pdf = Path("README.md"), Path("architecture.pdf")
    if readme.exists():
        txt = readme.read_text(encoding="utf-8")
        add("PASS" if f"{n_prod:,}" in txt else "WARN", "README shows the current product count",
            f"expects {n_prod:,}: run python make_docs.py if not")
        add("PASS" if Path(".env.example").exists() else "FAIL", ".env.example exists (the README tells people to copy it)")
    else:
        add("FAIL", "README.md exists")
    if pdf.exists():
        pages = len(re.findall(rb"/Type\s*/Page[^s]", pdf.read_bytes()))
        add("PASS" if 1 <= pages <= 3 else "FAIL", "architecture.pdf is 3 pages or fewer", f"{pages} pages")
        if xlsx.exists():
            add("PASS" if pdf.stat().st_mtime >= xlsx.stat().st_mtime else "WARN",
                "architecture.pdf is newer than the export", "re-run make_docs.py after the final export")
    else:
        add("FAIL", "architecture.pdf exists")

    print("\n== repository")
    code, out = sh("git", "grep", "-I", "-n", "-E", r"AIza[0-9A-Za-z_-]{20,}|gsk_[0-9A-Za-z]{20,}|ghp_[0-9A-Za-z]{20,}")
    add("PASS" if code == 1 and not out else "FAIL", "no API keys in tracked files", out[:200] if out else "")
    code, out = sh("git", "ls-files", ".env", "data", "output")
    add("PASS" if not out else "FAIL", ".env, data/ and output/ are not tracked", out[:200])
    code, out = sh("git", "status", "--porcelain")
    add("PASS" if not out else "FAIL", "everything is committed", f"{len(out.splitlines())} uncommitted change(s)" if out else "")
    code, out = sh("git", "rev-list", "--count", "@{u}..HEAD")
    if code == 0:
        add("PASS" if out.strip() == "0" else "FAIL", "everything is pushed", f"{out.strip()} commit(s) not pushed")
    else:
        add("WARN", "could not compare with the remote", "run git push -u origin master once")

    print("\n== tests")
    code, out = sh(sys.executable, "-m", "pytest", "-q")
    add("PASS" if code == 0 else "FAIL", "unit tests", out.splitlines()[-1] if out else "")

    fails = sum(1 for r in results if r[0] == "FAIL")
    warns = sum(1 for r in results if r[0] == "WARN")
    print(f"\n{fails} FAIL, {warns} WARN")
    print("still manual: open the repo and the Sheet in a private window, and tick all 30 rows of output/audit_sample.md")
    return 1 if fails else 0


sys.exit(main())