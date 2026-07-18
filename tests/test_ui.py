"""Guest-side rich-ui flattening (duck-typed; no real plotly/pandas)."""

from __future__ import annotations

import json

from dud.guest.ui import flatten_rich


class FakePlotly:
    __module__ = "plotly.graph_objs._figure"

    def to_json(self):
        return '{"data": [], "layout": {}}'


class FakeDataFrame:
    __module__ = "pandas.core.frame"
    columns = ["a", "b"]

    def __len__(self):
        return 42

    def head(self, n):
        return self

    def to_json(self, orient=None, date_format=None):
        return '{"columns": ["a", "b"], "index": [0], "data": [[1, 2]]}'


def _files(workspace):
    ui = workspace / "ui"
    return sorted(p.name for p in ui.iterdir()) if ui.exists() else []


def test_plotly_becomes_spec_file(tmp_path):
    ui = {"plot": FakePlotly()}
    handled = flatten_rich(ui, str(tmp_path))
    assert handled == {"plot"}
    assert _files(tmp_path) == ["plot.plotly.json"]
    body = json.loads((tmp_path / "ui" / "plot.plotly.json").read_text())
    assert "data" in body and "layout" in body


def test_pandas_becomes_table_with_total(tmp_path):
    ui = {"df": FakeDataFrame()}
    handled = flatten_rich(ui, str(tmp_path))
    assert handled == {"df"}
    payload = json.loads((tmp_path / "ui" / "df.table.json").read_text())
    assert payload["columns"] == ["a", "b"]
    assert payload["total"] == 42  # full length, not the truncated head


def test_representable_values_are_left_to_cross(tmp_path):
    ui = {"stats": [{"label": "n", "value": 3}], "note": "hello", "n": 7}
    handled = flatten_rich(ui, str(tmp_path))
    assert handled == set()  # host renderer owns these
    assert _files(tmp_path) == []


def test_mixed_ui_partitions(tmp_path):
    ui = {"chart": FakePlotly(), "cards": [{"label": "x", "value": 1}]}
    handled = flatten_rich(ui, str(tmp_path))
    assert handled == {"chart"}  # only the rich one is materialized here
    assert _files(tmp_path) == ["chart.plotly.json"]


def test_serialization_failure_leaves_value(tmp_path):
    class Broken:
        __module__ = "plotly.x"

        def to_json(self):
            raise RuntimeError("boom")

    ui = {"bad": Broken()}
    assert flatten_rich(ui, str(tmp_path)) == set()
