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
    job_fields = report.job_fields(print_payload)
    gcode_file = job_fields.pop("gcode_file")
    subtask = job_fields.pop("subtask_name")
    try:
        status = report.status_fields(print_payload, info_payload, model)
    except Exception as e:
        logger.warning("Decoding the report from %s failed: %s", printer, e)
        status = {}
    try:
        errors = hms.printer_errors(print_payload)
    except Exception as e:
        logger.warning("Decoding the errors from %s failed: %s", printer, e)
        errors = []
    try:
        filaments = report.filaments(print_payload)
    except Exception as e:
        logger.warning("Decoding the filaments from %s failed: %s", printer, e)
        filaments = []
    fields = {
        **job_fields,
        **status,
        "file_name": subtask or gcode_file,
        "filaments": filaments,
        "errors": errors,
        "developer_mode": protocol.developer_mode(print_payload),
        "online": job_fields["gcode_state"] != "UNKNOWN" or status.get("nozzle_temp") is not None,
    }
    job = Job(
        key=subtask or gcode_file,
        gcode_file=gcode_file,
        subtask=subtask,
        gcode_state=job_fields["gcode_state"],
        internal=protocol.file_on_internal_storage(print_payload),
    )
    return fields, job
