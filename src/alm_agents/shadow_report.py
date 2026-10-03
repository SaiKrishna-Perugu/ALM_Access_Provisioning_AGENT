"""What the agents would have written: the evidence for turning writes on.

    python -m alm_agents.shadow_report                      # the last 14 days, as Markdown
    python -m alm_agents.shadow_report --days 7 --csv shadow.csv

During the PROD shadow phase every run is a dry run: the agents read the real
queue, plan, and stop short of writing. This report reads the run registry
(it needs the deployment's database settings, nothing else) and lists every
write the dry runs planned. It also lists every run that halted or failed, and
what the runs cost. Nothing is written anywhere.

A planned write is a line of the approval card the dry run previewed: the
user, and what a writing run would have asked a human to approve for them.
The CSV has one row per planned write, with two empty columns: what actually
happened (from the CLI's log, or what the requesters were given by hand), and
whether it matches. Filling those in for a week or two is the comparison the
go-live decision rests on (docs/ENTERPRISE_PLAN.md, Phase 8).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

SYSTEM_TRIGGERS = ("retention",)
CSV_COLUMNS = ("date", "thread_id", "work_item_id", "userid", "operation", "planned",
               "what_actually_happened", "matches")


def _when(value: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


async def collect(store, *, days: int, now: datetime | None = None,
                  limit: int = 5000) -> dict:
    """The dry runs created in the last ``days`` days, and what each planned."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    runs = [r for r in await store.list_runs(limit=limit)
            if r.get("mode") == "dry" and r.get("trigger") not in SYSTEM_TRIGGERS
            and _when(r.get("created_at", "")) >= since]
    planned, blocked, rows = Counter(), Counter(), []
    tokens, items = 0, set()
    summaries = []
    for run in sorted(runs, key=lambda r: r.get("created_at", "")):
        report = run.get("report") or {}
        results = report.get("results") or []
        # What a writing run would have done is what it would have asked a
        # human to approve: the card a dry run previews, saved like any other.
        request, _ = await store.get_approval(run["thread_id"])
        writes = [{"userid": item.userid, "operation": item.action or item.state.value,
                   "work_item_id": ", ".join(item.work_item_ids)}
                  for item in (request.items if request else [])]
        held = [r for r in results if "ALM_WRITES_DISABLED_OPERATIONS" in str(r.get("message"))
                or "ALM_ALLOWED_OPERATIONS" in str(r.get("message"))]
        for w in writes:
            planned[w["operation"]] += 1
            rows.append({"date": str(run.get("created_at", ""))[:10],
                         "thread_id": run["thread_id"], "work_item_id": w["work_item_id"],
                         "userid": w["userid"], "operation": w["operation"],
                         "planned": "yes", "what_actually_happened": "", "matches": ""})
        for r in held:
            blocked[r.get("operation", "")] += 1
        run_tokens = int((report.get("metrics") or {}).get("tokens") or 0)
        tokens += run_tokens
        items.update(run.get("scope") or [])
        summaries.append({
            "thread_id": run["thread_id"], "created": str(run.get("created_at", ""))[:16],
            "status": run.get("status", ""), "work_items": run.get("scope") or [],
            "writes": [f"{w['userid']} ({w['operation']})" for w in writes],
            "halt_reason": report.get("halt_reason", "") if report.get("halted") else "",
            "error": run.get("error", ""), "tokens": run_tokens,
            "version": report.get("version") or run.get("version", ""),
        })
    return {
        "since": since, "until": now, "runs": summaries, "rows": rows,
        "planned": dict(planned), "blocked": dict(blocked), "tokens": tokens,
        "work_items": len(items),
        "halted": sum(1 for s in summaries if s["halt_reason"]),
        "failed": sum(1 for s in summaries if s["status"] == "failed"),
        "versions": sorted({s["version"] for s in summaries if s["version"]}),
    }


def markdown(data: dict, *, environment: str = "") -> str:
    runs = data["runs"]
    per_item = round(data["tokens"] / data["work_items"]) if data["work_items"] else 0
    lines = [
        f"# Shadow report{f': {environment}' if environment else ''}",
        "",
        f"Dry runs created {data['since']:%Y-%m-%d %H:%M} to {data['until']:%Y-%m-%d %H:%M} UTC. "
        "Nothing in this period was written; this is what would have been.",
        "",
        "| | |", "|---|---|",
        f"| Dry runs | {len(runs)} |",
        f"| Work items | {data['work_items']} |",
        f"| Planned writes | {sum(data['planned'].values())} |",
        f"| Runs that halted | {data['halted']} |",
        f"| Runs that failed | {data['failed']} |",
        f"| Model tokens | {data['tokens']:,} ({per_item:,} per work item) |",
        f"| Prompt and model versions | {', '.join(data['versions']) or '-'} |",
        "",
    ]
    if data["planned"]:
        lines += ["## Planned writes by action", "", "| Action | Planned |", "|---|---|"]
        lines += [f"| {op} | {n} |" for op, n in sorted(data["planned"].items())]
        lines.append("")
    if data["blocked"]:
        lines += ["## Held back by staged enablement", "", "| Operation | Times |", "|---|---|"]
        lines += [f"| {op} | {n} |" for op, n in sorted(data["blocked"].items())]
        lines.append("")
    troubled = [r for r in runs if r["halt_reason"] or r["status"] == "failed"]
    if troubled:
        lines += ["## Runs that did not finish cleanly", "",
                  "| Run | Work items | Why |", "|---|---|---|"]
        lines += [f"| {r['thread_id']} | {', '.join(r['work_items']) or 'queue'} | "
                  f"{(r['halt_reason'] or r['error'])[:160]} |" for r in troubled]
        lines.append("")
    lines += ["## Every run", "", "| Created | Run | Work items | Status | Planned writes | Tokens |",
              "|---|---|---|---|---|---|"]
    lines += [f"| {r['created']} | {r['thread_id']} | {', '.join(r['work_items']) or 'queue'} | "
              f"{r['status']} | {', '.join(r['writes']) or '-'} | {r['tokens']:,} |"
              for r in runs]
    return "\n".join(lines) + "\n"


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    from alm_core.config import get_settings
    from alm_core.logging import configure
    from alm_core.store import get_store

    parser = argparse.ArgumentParser(prog="python -m alm_agents.shadow_report",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=14, help="how far back (default 14)")
    parser.add_argument("--out", type=Path, help="write the Markdown here instead of stdout")
    parser.add_argument("--csv", type=Path, help="also write one row per planned write")
    args = parser.parse_args(argv)
    configure()
    settings = get_settings()

    async def run() -> dict:
        store = await get_store(settings)
        try:
            return await collect(store, days=args.days)
        finally:
            await store.close()

    data = asyncio.run(run())
    text = markdown(data, environment=getattr(settings, "environment", ""))
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    if args.csv:
        write_csv(data["rows"], args.csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
