import asyncio, json, time, tracemalloc, sqlite3, statistics, tempfile, sys, resource, argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from shmax.journal import EventJournal


async def main():
    with tempfile.TemporaryDirectory(prefix="shmax-journal-stress-") as td:
        j = EventJournal(Path(td) / "events.db")
        await j.start()
        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        tracemalloc.start()
        start = time.perf_counter()
        n = 100000
        for i in range(n):
            j.emit(
                "delivery.success",
                correlation_id=f"{i:024x}",
                direction="max_to_telegram",
            )
            if i % 128 == 127:
                await j.flush()
        await j.flush()
        elapsed = time.perf_counter() - start
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        queries = []
        for _ in range(30):
            t = time.perf_counter()
            j.query(limit=50, category="DELIVERY")
            queries.append((time.perf_counter() - t) * 1000)
        with sqlite3.connect(j.path) as db:
            count = db.execute("select count(*) from events").fetchone()[0]
            plan = [
                list(r)
                for r in db.execute(
                    "explain query plan select * from events where category=? order by last_at desc,id desc limit 50",
                    ("DELIVERY",),
                )
            ]
        before = j.storage()["bytes"]
        j.max_records = 1000
        start = time.perf_counter()
        for _ in range(110):
            await asyncio.to_thread(j._maintenance)
        cleanup = time.perf_counter() - start
        with sqlite3.connect(j.path) as db:
            after_count = db.execute("select count(*) from events").fetchone()[0]
        out = {
            "events_submitted": n,
            "retained_before_cleanup": count,
            "write_seconds": elapsed,
            "events_per_second": n / elapsed,
            "peak_traced_python_bytes": peak,
            "storage_before_cleanup": before,
            "storage_after_cleanup": j.storage()["bytes"],
            "records_after_cleanup": after_count,
            "cleanup_seconds": cleanup,
            "query_median_ms": statistics.median(queries),
            "query_max_ms": max(queries),
            "query_plan": plan,
            "peak_process_rss_native": resource.getrusage(
                resource.RUSAGE_SELF
            ).ru_maxrss,
            "process_rss_highwater_increase_native": resource.getrusage(
                resource.RUSAGE_SELF
            ).ru_maxrss
            - rss_before,
            "rss_native_unit": "bytes" if sys.platform == "darwin" else "KiB",
            "dropped": j.dropped,
            "write_failures": j.failures,
        }
        await j.close()
        args = argparse.ArgumentParser()
        args.add_argument("--output", type=Path)
        options = args.parse_args()
        if options.output:
            options.output.write_text(json.dumps(out, indent=2))
        print(json.dumps(out, indent=2))


asyncio.run(main())
