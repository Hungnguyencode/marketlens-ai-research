"""Local job CLI. No collector, Ollama or Streamlit integration in Milestone 3.1."""
from __future__ import annotations

import argparse
import json
import signal
import threading

from .job_store import DEFAULT_DATABASE, JobState, JobStore, JobStoreError
from .job_worker import Worker


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error can echo arbitrary supplied values (including credentials).
        self.exit(2, "Invalid CLI arguments. Use --help for allowed options.\n")


def main(argv=None) -> int:
    parser = SafeParser(description="Persistent local jobs (M3.1: offline TEST/SMOKE only)")
    parser.add_argument("--db", default=str(DEFAULT_DATABASE), help="SQLite database path; defaults to ignored outputs/jobs")
    parser.add_argument("--artifact-dir", help="Fixed artifact root for this DB; defaults to <db-directory>/artifacts")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=SafeParser)
    submit = commands.add_parser("submit-smoke", help="Queue a TEST/SMOKE infrastructure job")
    submit.add_argument("--steps", type=int, default=3)
    listing = commands.add_parser("list", help="List most recent jobs")
    listing.add_argument("--limit", type=int, default=50)
    inspect = commands.add_parser("inspect", help="Inspect persisted execution state")
    inspect.add_argument("job_id")
    cancel = commands.add_parser("cancel", help="Cancel queued job or request cooperative running cancellation")
    cancel.add_argument("job_id")
    commands.add_parser("recover", help="Mark expired RUNNING jobs INTERRUPTED; never requeue")
    worker = commands.add_parser("worker", help="Independent sequential worker; Ctrl+C stops safely")
    worker.add_argument("--once", action="store_true", help="Process at most one queued job, then exit")
    worker.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    try:
        if args.command == "worker" and not 0.1 <= args.poll_seconds <= 60:
            raise ValueError("Invalid polling interval")
        store = JobStore(args.db, artifact_root=args.artifact_dir)
        exit_code = 0
        if args.command == "submit-smoke":
            result = store.submit("smoke", {"steps": args.steps}).model_dump(mode="json")
        elif args.command == "list":
            result = [job.model_dump(mode="json") for job in store.list_jobs(args.limit)]
        elif args.command == "inspect":
            result = store.get(args.job_id).model_dump(mode="json")
        elif args.command == "cancel":
            result = store.request_cancel(args.job_id).model_dump(mode="json")
        elif args.command == "recover":
            result = {"interrupted_jobs": store.recover_stale(), "automatic_replays": 0}
        else:
            shutdown = threading.Event()
            previous = {}
            try:
                for signum in (signal.SIGINT, signal.SIGTERM):
                    previous[signum] = signal.signal(signum, lambda *_: shutdown.set())
                with Worker(store, shutdown=shutdown) as executor:
                    if args.once:
                        job = executor.run_once()
                        result = job.model_dump(mode="json") if job else {"queue": "EMPTY_OR_STOPPED"}
                        if job and job.state != JobState.COMPLETED:
                            exit_code = 1
                    else:
                        executor.run_forever(args.poll_seconds)
                        result = {"worker": "STOPPED"}
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return exit_code
    except JobStoreError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}))
        return 2
    except (ValueError, OSError):
        # Never emit third-party tracebacks or input values through the public CLI.
        print(json.dumps({"error": "JOB_COMMAND_FAILED",
                          "message": "Check command, allowed smoke schema, IDs, database availability and artifact configuration. Details withheld."}))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"worker": "INTERRUPTED", "automatic_replays": 0}))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
