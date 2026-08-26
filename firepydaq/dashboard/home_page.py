"""Compact FIREpyDAQ Home page.

Presentation only. Runtime is updated from the dashboard process monotonic clock.
"""
from __future__ import annotations

from pathlib import Path

from dash import html


def shorten_path(path_str):

    if not path_str:
        return "Not specified"

    path = Path(path_str)

    parts = path.parts

    for marker in (
        "02_ExperimentData",
        "Experiment",
        "ExperimentData",
        "Calibration",
    ):
        if marker in parts:
            idx = parts.index(marker)
            return str(Path(*parts[idx:]))

    return path.name

def make_home_page(paths, charts):
    data_path = str(paths.get("datapath", "") or "")
    config_path = str(paths.get("configpath", "") or "")
    formulae_path = str(paths.get("formulaepath", "") or "")
    chunk_dir = str(paths.get("chunk_dir", "") or "")
    test_name = Path(data_path).stem if data_path else "Current experiment"

    details = [
        ("Configuration", shorten_path(config_path)),
        ("Live chunks", shorten_path(chunk_dir)),
        ("Final data", shorten_path(data_path)),
    ]
    if formulae_path:
        details.append(("Formulae", formulae_path))

    return [
        html.Div(
            className="home-compact-header",
            children=[
                html.Div([
                    html.Span("CURRENT TEST", className="home-kicker"),
                    html.H1(test_name, className="home-test-title"),
                ]),
                html.Div(
                    [html.Span(className="home-live-dot"), "LIVE"],
                    className="home-live-pill",
                ),
            ],
        ),
        html.Div(
            className="home-metric-grid home-metric-grid-two",
            children=[
                html.Div(
                    className="home-metric-card",
                    children=[
                        html.Span("Runtime", className="home-metric-label"),
                        html.Strong("00:00", id="home-runtime", className="home-metric-value"),
                        html.Span("Since dashboard launch", className="home-metric-note"),
                    ],
                ),
                html.Div(
                    className="home-metric-card",
                    children=[
                        html.Span("Graph pages", className="home-metric-label"),
                        html.Strong(str(len(charts)), className="home-metric-value"),
                        html.Span("Generated from configuration", className="home-metric-note"),
                    ],
                ),
            ],
        ),
        html.Details(
            className="home-advanced",
            children=[
                html.Summary("Advanced information"),
                html.Div(
                    className="home-detail-list",
                    children=[
                        html.Div(
                            className="home-detail-row",
                            children=[
                                html.Span(label, className="home-detail-label"),
                                html.Code(value or "Not specified", className="home-detail-path"),
                            ],
                        )
                        for label, value in details
                    ],
                ),
            ],
        ),
    ]
