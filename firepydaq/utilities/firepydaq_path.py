from pathlib import Path
import ctypes
import os


def get_firepydaq_dir() -> Path:

    path = (Path.cwd() / ".firepydaq").resolve()

    path.mkdir(
        parents=True,
        exist_ok=True,
    )

    if os.name == "nt":
        try:
            attrs = ctypes.windll.kernel32.GetFileAttributesW(
                str(path)
            )

            if attrs != -1:
                ctypes.windll.kernel32.SetFileAttributesW(
                    str(path),
                    attrs | 0x02,
                )

        except Exception:
            pass

    return path


def get_active_run_dir(common_path) -> Path:

    firepydaq_dir = get_firepydaq_dir()

    run_name = Path(
        common_path
    ).stem

    return (
        firepydaq_dir
        / "active_runs"
        / run_name
    )


def get_telemetry_dir() -> Path:

    firepydaq_dir = get_firepydaq_dir()

    telemetry_dir = (
        firepydaq_dir
        / "telemetry"
    )

    telemetry_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    return telemetry_dir
