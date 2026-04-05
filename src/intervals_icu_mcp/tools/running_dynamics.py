"""Running dynamics tools — extracts Garmin metrics from FIT files via Intervals.icu."""

import io
import json
from typing import Annotated

from fastmcp import Context

from ..auth import ICUConfig
from ..client import ICUAPIError, ICUClient

DYNAMICS_FIELDS = [
    "stance_time",
    "stance_time_percent",
    "stance_time_balance",
    "vertical_oscillation",
    "vertical_ratio",
    "step_length",
]


def _parse_fit_dynamics(fit_bytes: bytes) -> dict:
    """Parse running dynamics from FIT file bytes."""
    from garmin_fit_sdk import Decoder, Stream

    stream = Stream.from_byte_array(bytearray(fit_bytes))
    decoder = Decoder(stream)
    messages, errors = decoder.read(
        apply_scale_and_offset=True,
        expand_components=True,
    )

    records = messages.get("record_mesgs", [])
    if not records:
        return {"error": "No record messages found in FIT file"}

    # Collect per-second dynamics
    samples = []
    for r in records:
        row = {}
        for field in DYNAMICS_FIELDS:
            val = r.get(field)
            if val is not None:
                row[field] = round(float(val), 2)
        if row:
            samples.append(row)

    if not samples:
        return {"error": "No running dynamics data in this FIT file (device may not support it)"}

    # Compute summary stats
    summary = {}
    for field in DYNAMICS_FIELDS:
        values = [s[field] for s in samples if field in s]
        if values:
            summary[field] = {
                "avg": round(sum(values) / len(values), 1),
                "min": round(min(values), 1),
                "max": round(max(values), 1),
                "samples": len(values),
            }

    # Also extract session-level averages if available
    session_msgs = messages.get("session_mesgs", [])
    session_avgs = {}
    if session_msgs:
        sess = session_msgs[0]
        for prefix in ["avg_", "max_"]:
            for field in DYNAMICS_FIELDS:
                key = f"{prefix}{field}"
                val = sess.get(key)
                if val is not None:
                    session_avgs[key] = round(float(val), 2)

    return {
        "summary": summary,
        "session_averages": session_avgs if session_avgs else None,
        "sample_count": len(samples),
        "total_records": len(records),
    }


async def get_running_dynamics(
    activity_id: Annotated[str, "Activity ID to extract running dynamics for"],
    include_time_series: Annotated[
        bool,
        "Include per-second time series data (large). Default false — only returns summary stats.",
    ] = False,
    ctx: Context | None = None,
) -> str:
    """Extract Garmin running dynamics from an activity's FIT file.

    Downloads the original FIT file and parses Garmin-specific running dynamics
    that aren't available through the standard Intervals.icu streams API:

    - stance_time: Ground contact time in ms
    - stance_time_percent: GCT as % of stride cycle
    - stance_time_balance: Left/right GCT balance (%)
    - vertical_oscillation: Vertical bounce per step (mm)
    - vertical_ratio: Vertical oscillation / step length (%)
    - step_length: Single step length (mm)

    Requires a Garmin device with running dynamics support (Fenix, Forerunner, etc.).

    Args:
        activity_id: The unique ID of the activity
        include_time_series: Whether to include per-second data (default: false)

    Returns:
        JSON with summary statistics and optionally per-second time series
    """
    assert ctx is not None
    config: ICUConfig = ctx.get_state("config")

    try:
        async with ICUClient(config) as client:
            fit_bytes = await client.download_fit_file(activity_id)

        result = _parse_fit_dynamics(fit_bytes)

        if "error" in result:
            return json.dumps(result)

        # Add per-second data if requested
        if include_time_series:
            from garmin_fit_sdk import Decoder, Stream

            stream = Stream.from_byte_array(bytearray(fit_bytes))
            decoder = Decoder(stream)
            messages, _ = decoder.read(
                apply_scale_and_offset=True,
                expand_components=True,
            )
            time_series = []
            for r in messages.get("record_mesgs", []):
                row = {}
                for field in DYNAMICS_FIELDS:
                    val = r.get(field)
                    if val is not None:
                        row[field] = round(float(val), 2)
                if row:
                    ts = r.get("timestamp")
                    if ts is not None:
                        row["timestamp"] = str(ts)
                    time_series.append(row)
            result["time_series"] = time_series

        return json.dumps(result)

    except ICUAPIError as e:
        return json.dumps({"error": f"API error: {e}"})
    except ImportError:
        return json.dumps({"error": "garmin-fit-sdk not installed — required for FIT file parsing"})
    except Exception as e:
        return json.dumps({"error": f"FIT parsing error: {e}"})
