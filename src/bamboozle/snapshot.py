"""Everything the dashboard shows, decoded from a merged report.

Pure: `decode(print, info, model)` runs the decoders in report.py, hms.py and
protocol.py over `MergedReport.print` / `.info` and returns the `PrinterState`
fields plus what the thumbnail logic needs to know about the job. The
connection adds what doesn't come from the report (online, camera,
thumbnail, last_error).
"""
from __future__ import annotations

import logging
from typing import Any, NamedTuple

from . import hms, protocol, report

logger = logging.getLogger(__name__)


class Job(NamedTuple):
    """What the report says about the current print job, for the thumbnail."""
    key: str  # subtask_name, else gcode_file; "" when no job is known
    gcode_file: str
    subtask: str
    gcode_state: str
    internal: bool  # the file is on internal storage, out of FTPS reach


def decode(print_payload: dict, info_payload: dict | None, model: str,
           printer: str = "") -> tuple[dict[str, Any], Job]:
    """`PrinterState` fields (minus the connection's own) and the job.

    A decoder that fails costs only its own fields (logged), never the rest.
    """
    def guarded(what, fn, default):
        try:
            return fn()
        except Exception as e:
            logger.warning("Decoding the %s from %s failed: %s", what, printer, e)
            return default

    job_fields = guarded("job", lambda: report.job_fields(print_payload), {
        "gcode_state": "UNKNOWN", "gcode_file": "", "subtask_name": ""})
    gcode_file = job_fields.pop("gcode_file")
    subtask = job_fields.pop("subtask_name")
    status = guarded("report", lambda: report.status_fields(print_payload, info_payload, model), {})
    errors = guarded("errors", lambda: hms.printer_errors(print_payload), [])
    filaments = guarded("filaments", lambda: report.filaments(print_payload), [])
    fields = {
        **job_fields,
        **status,
        "file_name": subtask or gcode_file,
        "filaments": filaments,
        "errors": errors,
        "developer_mode": guarded("Developer Mode", lambda: protocol.developer_mode(print_payload), None),
        "online": job_fields["gcode_state"] != "UNKNOWN" or status.get("nozzle_temp") is not None,
    }
    job = Job(
        key=subtask or gcode_file,
        gcode_file=gcode_file,
        subtask=subtask,
        gcode_state=job_fields["gcode_state"],
        internal=guarded("storage", lambda: protocol.file_on_internal_storage(print_payload), False),
    )
    return fields, job
