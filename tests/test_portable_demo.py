"""Fresh-source demo/config checks; no private files or services."""
from pathlib import Path

import httpx
import pytest

from market_research.multi_niche import load_batch_config, run_batch
from market_research.trends_csv import GoogleTrendsCsvCollector, SYNTHETIC_SOURCE
from scripts.create_synthetic_demo import create_demo


ROOT = Path(__file__).resolve().parents[1]


def test_portable_registry_runs_honestly_without_local_csv_or_network(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network forbidden in fresh-source test")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    config = load_batch_config(ROOT / "niches.example.json")
    assert all(niche.trends is None and niche.catalog_snapshot is None for niche in config.niches)
    batch = run_batch(config, output_dir=tmp_path / "SYNTHETIC-empty-batch", collect_live=False)
    assert all(niche["evidence_coverage"] == "NO_EVIDENCE" for niche in batch["niches"])
    assert batch["ranking_status"] == "NOT_YET_RANKABLE"


def test_synthetic_demo_is_utf8_and_does_not_overwrite_existing_data(tmp_path):
    root = tmp_path / "SYNTHETIC-demo"
    path = create_demo(root)
    original = path.read_bytes()
    assert original.startswith(b"\xef\xbb\xbf") and "SYNTHETIC DEMO ONLY" in original.decode("utf-8-sig")
    rows = GoogleTrendsCsvCollector(path, SYNTHETIC_SOURCE).collect(
        run_id="SYNTHETIC", niche="SYNTHETIC demo", keyword="cozy coloring book", target_region="United States")
    assert len(rows) == 4 and rows[1].metric_value is None
    assert rows[1].raw_observation["raw_value"] == "<1"
    with pytest.raises(FileExistsError):
        create_demo(root)
    assert path.read_bytes() == original
