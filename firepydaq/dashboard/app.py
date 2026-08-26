from __future__ import annotations

import atexit
import json
import logging
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Timer
import time
import dash_daq as daq
import numpy as np
import polars as pl
import pandas as pd
import plotly.graph_objects as go
from dash import ALL, Dash, Input, Output, ctx, dcc, html, no_update
from plotly.subplots import make_subplots

from ..dashboard.raw_stream import RawDataSubscriber
from .home_page import make_home_page
from ..utilities.firepydaq_path import (
        get_firepydaq_dir,
        get_active_run_dir,
    )

FIREPYDAQ_DIR = get_firepydaq_dir()
REFRESH_MS = 1000
MAX_PLOT_POINTS = 2000
DASHBOARD_PORT = 1222
LOGGER = logging.getLogger(__name__)


def _first(settings, *names, default=""):
    normalized = {str(k).strip().lower().replace("_", " "): v for k, v in settings.items()}
    for name in names:
        key = name.strip().lower().replace("_", " ")
        value = normalized.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _resolve_path(value, base_dir):
    if not value:
        return ""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return str(path.resolve())


def _load_paths(kwargs):
    settings = {}
    json_path = kwargs.get("jsonpath")
    base_dir = Path.cwd()
    if json_path:
        json_path = Path(json_path).expanduser().resolve()
        base_dir = json_path.parent
        with json_path.open("r", encoding="utf-8") as stream:
            settings = json.load(stream)

    config_path = kwargs.get("configpath") or _first(
        settings, "Configuration File", "Configuration Path", "Config File", "configpath"
    )
    formulae_path = kwargs.get("formulaepath") or _first(
        settings, "Formulae File", "Formula File", "Formulae Path", "formulaepath"
    )
    data_path = kwargs.get("datapath") or _first(
        settings, "Test Name"
    )

    config_path = _resolve_path(config_path, base_dir)
    formulae_path = _resolve_path(formulae_path, base_dir)
    data_path = _resolve_path(data_path, base_dir)

    # Existing settings normally contain the intended final .parquet path. It is
    # permitted not to exist while acquisition is running.
    if data_path and not data_path.lower().endswith(".parquet"):
        data_path += ".parquet"

    chunk_dir = get_active_run_dir(
        data_path
    ).resolve()

    if not config_path or not Path(config_path).is_file():
        raise FileNotFoundError(f"Configuration file not found: {config_path or '<unset>'}")
    return {
        "jsonpath": str(json_path) if json_path else "",
        "configpath": config_path,
        "formulaepath": formulae_path,
        "datapath": data_path,
        "chunk_dir": chunk_dir,
    }


def _read_chart_info(config_path, formulae_path=""):
    config = pl.read_csv(config_path, ignore_errors=True)
    config.columns = [c.strip() for c in config.columns]
    required = {"Chart", "Layout", "Position", "Label"}
    missing = required.difference(config.columns)
    if missing:
        raise ValueError(f"Configuration is missing dashboard columns: {sorted(missing)}")
    if "Legend" not in config.columns:
        config = config.with_columns(pl.col("Label").alias("Legend"))
    if "Processed_Unit" not in config.columns:
        config = config.with_columns(pl.lit("").alias("Processed_Unit"))
    return config.sort(["Chart", "Position"])


def _decimate(x, y, max_points=MAX_PLOT_POINTS):
    x = np.asarray(x)
    y = np.asarray(y)
    count = min(len(x), len(y))
    if count == 0:
        return np.asarray([]), np.asarray([])
    x, y = x[-count:], y[-count:]
    if count <= max_points:
        return x, y
    step = int(np.ceil(count / max_points))
    return x[::step], y[::step]


def create_dash_app(**kwargs):
    paths = _load_paths(kwargs)
    # print("=" * 80)
    # print("CONFIG:", paths["configpath"])
    # print("DATA:", paths["datapath"])
    # print("CHUNKS:", paths["chunk_dir"])
    # print("=" * 80)
    chart_info = _read_chart_info(paths["configpath"], paths["formulaepath"])

    subscriber = RawDataSubscriber(
        config_path=paths["configpath"],
        formulae_path=paths["formulaepath"],
        chunk_dir=paths["chunk_dir"],
        max_seconds=300,
        max_ui_hz=2,
    )
    subscriber.start()
    atexit.register(subscriber.stop)

    app = Dash(__name__, suppress_callback_exceptions=True)
    log_handler = RotatingFileHandler("DashboardError.log", maxBytes=100_000, backupCount=2)
    log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger("werkzeug").addHandler(log_handler)

    charts = [str(v).strip() for v in chart_info["Chart"].unique().sort().to_list()]

    def blank_figure(chart):
        rows = chart_info.filter(pl.col("Chart") == chart)
        layout_count = max(int(v) for v in rows["Layout"].to_list())
        figure = make_subplots(rows=layout_count, cols=1, shared_xaxes=True)
        figure.update_layout(
            title_text=f"{chart} Graphs",
            uirevision=chart,
            margin=dict(l=60, r=25, t=55, b=45),
        )
        figure.update_xaxes(title_text="Time (s)", row=layout_count, col=1)
        return figure

    dashboard_started_at = time.monotonic()

    def serve_layout():
        sidebar = []
        for chart in charts + ["Home"]:
            sidebar.extend([
                html.Button(chart, id={"type": "sidebar-btn", "index": chart}, className="button"),
                html.Br(),
            ])

        plot_layouts = [
            html.Div(
                dcc.Graph(
                    id={"type": "graphs", "index": chart},
                    figure=blank_figure(chart),
                    responsive=True,
                    style={"height": "75vh", "width": "100%"},
                ),
                id={"type": "plot-layout", "index": chart},
                className="sub-layout",
                style={"display": "none"},
            )
            for chart in charts
        ]
        file_items = [
            html.P([html.Strong("Configuration File: "), paths["configpath"]]),
            html.P([html.Strong("Live Chunk Directory: "), str(paths["chunk_dir"])]),
            html.P([html.Strong("Final Data File: "), paths["datapath"]]),
        ]
        if paths["formulaepath"]:
            file_items.append(html.P([html.Strong("Formulae File: "), paths["formulaepath"]]))
        # plot_layouts.append(
        #     html.Div(
        #         [html.H1("FIREpyDAQ experiment dashboard"), *file_items],
        #         id={"type": "plot-layout", "index": "Home"},
        #         className="sub-layout",
        #         style={"display": "block"},
        #     )
        # )

        plot_layouts.append(
            html.Div(
                make_home_page(paths, charts),
                id={"type": "plot-layout", "index": "Home"},
                className="sub-layout",
                style={"display": "block"},
            )
        )

        return html.Div([
            html.Div([
                html.Div("FIREpyDAQ Dashboard", className="titlebar-head"),
                html.Div([
                    html.Button(html.Img(id="snap", src="/assets/icons8-graph-50.png"), id="snapshot"),
                    html.Button(html.Img(id="play", src="/assets/icons8-pause-48.png"), id="pause-play"),
                    html.Img(id="light", src="/assets/icons8-sun-24.png"),
                    daq.BooleanSwitch(id="display-switch"),
                    html.Img(id="dark", src="/assets/icons8-moon-24.png"),
                ], className="titlebar-tool"),
            ], id="titlebar", className="titlebar"),
            html.Div(sidebar, id="sidebar", className="sidebar"),
            html.Div(plot_layouts, id="central-layout", className="main-layout"),
            dcc.Interval(id="refresh", interval=REFRESH_MS, n_intervals=0),
            html.Div(id="snapshot-status", style={"display": "none"}),
        ])

    app.layout = serve_layout

    @app.callback(
        Output("home-runtime", "children"),
        Input("refresh", "n_intervals"),
    )
    def update_home_runtime(_interval):
        elapsed = max(0, int(time.monotonic() - dashboard_started_at))
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    app.clientside_callback(
        """
        function(on) {
            const r = document.documentElement;
            const v = on
              ? ['#2b2b2c','#181818','#167fca','#605f5f','#e5e4e2']
              : ['white','#f0f0f0','#167fca','#c3c3c3','black'];
            ['--main-color','--bg','--highlight','--hover','--txt'].forEach((k,i) => r.style.setProperty(k,v[i]));
            return '#167fca';
        }
        """,
        Output("display-switch", "color"),
        Input("display-switch", "on"),
    )

    @app.callback(Output("play", "src"), Output("refresh", "disabled"), Input("pause-play", "n_clicks"))
    def pause(clicks):
        paused = bool(clicks and clicks % 2 == 1)
        return ("/assets/icons8-play-50.png" if paused else "/assets/icons8-pause-48.png"), paused

    @app.callback(
        Output({"type": "plot-layout", "index": ALL}, "style"),
        Input({"type": "sidebar-btn", "index": ALL}, "n_clicks"),
    )
    def navigate(_clicks):
        selected = ctx.triggered_id.get("index", "Home") if isinstance(ctx.triggered_id, dict) else "Home"
        return [{"display": "block"} if item == selected else {"display": "none"} for item in charts + ["Home"]]

    @app.callback(Output({"type": "graphs", "index": ALL}, "figure"), Input("refresh", "n_intervals"))
    def refresh(_interval):
        snapshot = subscriber.snapshot()
        # print(snapshot.keys())
        figures = []
        for chart in charts:
            figure = blank_figure(chart)
            if snapshot and "Time" in snapshot:
                rows = chart_info.filter(pl.col("Chart") == chart)
                for row in rows.iter_rows(named=True):
                    label = str(row["Label"]).strip()
                    if label not in snapshot:
                        continue
                    x_plot, y_plot = _decimate(snapshot["Time"], snapshot[label])
                    position = int(row["Position"])
                    legend = str(row.get("Legend", label)).strip()
                    figure.add_trace(
                        go.Scattergl(x=x_plot, y=y_plot, mode="lines", name=legend),
                        row=position,
                        col=1,
                    )
                    unit = str(row.get("Processed_Unit", "")).strip()
                    if unit:
                        figure.update_yaxes(title_text=unit, row=position, col=1)
            figures.append(figure)
        return figures

    @app.callback(
        Output("snapshot-status", "children"),
        Input("snapshot", "n_clicks"),
        Input({"type": "graphs", "index": ALL}, "figure"),
        prevent_initial_call=True,
    )
    def snapshots(clicks, figures):
        if not clicks or ctx.triggered_id != "snapshot":
            return no_update
        base = str(Path(paths["datapath"] or "dashboard").with_suffix(""))
        for index, figure in enumerate(figures or []):
            go.Figure(figure).write_html(f"{base}_{charts[index]}.html")
        return f"Saved {len(figures or [])} snapshots"

    def open_browser():
        webbrowser.open_new(f"http://127.0.0.1:{DASHBOARD_PORT}/")

    if __name__ == "main":
        return app

    Timer(1.0, open_browser).start()
    try:
        app.run(port=DASHBOARD_PORT, debug=False, use_reloader=False)
    finally:
        subscriber.stop()


if __name__ == "__main__":
    create_dash_app()
