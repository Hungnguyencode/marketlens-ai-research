"""Local jobs: explicit research authorization, independent worker, no Streamlit side effects."""
from __future__ import annotations

import argparse
import json
import signal
import threading

from .job_store import DEFAULT_DATABASE, JobState, JobStore, JobStoreError, ResearchInputError
from .job_worker import Worker


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default error can echo arbitrary supplied values (including credentials).
        self.exit(2, "Invalid CLI arguments. Use --help for allowed options.\n")


def main(argv=None) -> int:
    parser = SafeParser(description="Persistent local smoke and explicitly authorized single-niche research jobs")
    parser.add_argument("--db", default=str(DEFAULT_DATABASE), help="SQLite database path; defaults to ignored outputs/jobs")
    parser.add_argument("--artifact-dir", help="Fixed artifact root for this DB; defaults to <db-directory>/artifacts")
    parser.add_argument("--input-root", help="Allowed local input directory; persisted per DB, defaults to repository root")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=SafeParser)
    submit = commands.add_parser("submit-smoke", help="Queue a TEST/SMOKE infrastructure job")
    submit.add_argument("--steps", type=int, default=3)
    research = commands.add_parser("submit-research", help="Queue one research request; external operations require explicit flags")
    research.add_argument("--topic", required=True)
    research.add_argument("--mode", required=True, choices=["csv_import", "replay", "live"])
    research.add_argument("--input", dest="input_path", help="Existing CSV or evidence JSON inside the configured input root")
    research.add_argument("--source-url", help="Actual Google Trends export page URL (csv_import only)")
    research.add_argument("--allow-network", action="store_true", help="Authorize existing Google Books collection (live mode only)")
    research.add_argument("--allow-inference", action="store_true", help="Authorize local Ollama planning/analysis and existing bounded repair")
    research.add_argument("--model", default="qwen3:4b-instruct")
    research.add_argument("--request-timeout", type=int, default=15)
    research.add_argument("--inference-timeout", type=int, default=120)
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
        store = JobStore(args.db, artifact_root=args.artifact_dir, input_root=args.input_root)
        exit_code = 0
        if args.command == "submit-smoke":
            result = store.submit("smoke", {"steps": args.steps}).model_dump(mode="json")
        elif args.command == "submit-research":
            payload = {"topic": args.topic, "mode": args.mode, "input_path": args.input_path,
                       "source_url": args.source_url, "allow_network": args.allow_network,
                       "allow_inference": args.allow_inference, "model": args.model,
                       "request_timeout_seconds": args.request_timeout,
                       "inference_timeout_seconds": args.inference_timeout}
            result = store.submit("research", payload).model_dump(mode="json")
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
        # ASCII JSON escapes preserve Unicode paths/topics even with a Windows legacy pipe encoding.
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return exit_code
    except JobStoreError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}))
        return 2
    except ResearchInputError as exc:
        print(json.dumps({"error": "INVALID_RESEARCH_REQUEST", "message": str(exc)}))
        return 2
    except (ValueError, OSError):
        # Never emit third-party tracebacks or input values through the public CLI.
        print(json.dumps({"error": "JOB_COMMAND_FAILED",
                          "message": "Check command, request schema, IDs, database/input availability and artifact configuration. Details withheld."}))
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"worker": "INTERRUPTED", "automatic_replays": 0}))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
