"""Create a new, clearly synthetic input workspace; never submit jobs or call services."""
import argparse
import sys
from pathlib import Path


def create_demo(directory):
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=False)
    inputs = root / "inputs"
    inputs.mkdir()
    path = inputs / "SYNTHETIC-google-trends.csv"
    path.write_text("SYNTHETIC DEMO ONLY — NOT REAL MARKET EVIDENCE\nDanh mục: Tất cả danh mục\n\n"
                    "Tuần,cozy coloring book: (Hoa Kỳ)\n2025-10-19,54\n2025-10-26,<1\n"
                    "2025-11-02,80\n2025-11-09,64\n", encoding="utf-8-sig")
    return path


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, help="New dedicated directory; existing directories are rejected")
    args = parser.parse_args()
    try:
        path = create_demo(args.directory)
    except FileExistsError:
        parser.error("Demo directory already exists; choose a new directory. Nothing was overwritten.")
    print("SYNTHETIC DEMO ONLY — NOT REAL MARKET EVIDENCE")
    print("CSV:", path)
    print("Use topic: SYNTHETIC mentor demo; keep inference OFF.")
    print("Leave the source URL blank in Workspace. No real Google Trends export or source URL exists for this demo.")


if __name__ == "__main__":
    main()
