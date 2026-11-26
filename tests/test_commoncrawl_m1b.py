"""
Unit and Bounded Verification Tests for Milestone 1B:
Common Crawl WebGraph Feasibility Benchmark.
"""
import pytest
from app.sources.commoncrawl.webgraph_benchmark import WebGraphBenchmark, WebGraphBenchmarkReport


def test_m1b_manifest_discovery():
    """Verify discovery of real active WebGraph release and path manifests."""
    bench = WebGraphBenchmark()
    info = bench.discover_active_release()
    assert "release" in info
    assert len(info["vertices_paths"]) > 0
    assert len(info["edges_paths"]) > 0
    assert any("vertices" in p for p in info["vertices_paths"])
    assert any("edges" in p for p in info["edges_paths"])


def test_m1b_streaming_benchmark():
    """Verify bounded streaming benchmark runs without memory bloat."""
    bench = WebGraphBenchmark()
    # Test with very conservative budget: 0.5 MiB
    report: WebGraphBenchmarkReport = bench.run_benchmark(max_mb_per_partition=0.5)

    assert report.release_id is not None
    assert report.total_vertex_partitions > 0
    assert report.total_edge_partitions > 0

    # Vertices verification
    assert report.vertex_benchmark.lines_parsed > 0
    assert report.vertex_benchmark.peak_ram_mb < 50.0  # Must be strictly under 50 MB
    assert len(report.vertex_benchmark.sample_records) > 0
    # Check sample format: <id>\t<reversed_domain>
    assert "\t" in report.vertex_benchmark.sample_records[0]

    # Edges verification
    assert report.edge_benchmark.lines_parsed > 0
    assert report.edge_benchmark.peak_ram_mb < 50.0  # Must be strictly under 50 MB
    assert len(report.edge_benchmark.sample_records) > 0
    # Check sample format: <src_id>\t<dst_id>
    assert "\t" in report.edge_benchmark.sample_records[0]

    assert report.feasibility_decision == "DEFERRED_TO_COLUMNAR_QUERY"
