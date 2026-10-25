"""FTPS thumbnail lookup (thumbnail.py), against a fake FTP server object."""
import ftplib
import io
import ssl
import threading
import zipfile

import pytest

from bamboozle import thumbnail


def _3mf(*plates, gcode=None):
    """A 3MF (zip) holding Metadata/plate_N.png (content b"PNG<N>") per plate."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in plates:
            zf.writestr(f"Metadata/plate_{n}.png", f"PNG{n}".encode())
            zf.writestr(f"Metadata/plate_{n}_small.png", f"SMALL{n}".encode())
            zf.writestr(f"Metadata/pick_{n}.png", f"PICK{n}".encode())
        for n in gcode or ():
            zf.writestr(f"Metadata/plate_{n}.gcode", b"G28")
    return buf.getvalue()


class FakeFtp:
    """The calls fetch_thumbnail makes; `files` maps full paths to bytes."""

    def __init__(self, files, chunk=4096, on_chunk=None):
        self.files = files
        self.dirs = {p.rsplit("/", 1)[0] or "/" for p in files} | {"/"}
        self.cwd_path = "/"
        self.chunk = chunk
        self.on_chunk = on_chunk
        self.listed = []
        self.retrieved = []
        self.closed = False

    def cwd(self, path):
        if path not in self.dirs:
            raise ftplib.error_perm("550 Failed to change directory.")
        self.cwd_path = path

    def nlst(self, *args):
        self.listed.append(self.cwd_path)
        prefix = self.cwd_path.rstrip("/") + "/"
        return [p[len(prefix):] for p in self.files
                if p.startswith(prefix) and "/" not in p[len(prefix):]]

    def retrbinary(self, cmd, callback):
        path = cmd.split(" ", 1)[1]
        self.retrieved.append(path)
        data = self.files[path]
        for i in range(0, len(data), self.chunk):
            if self.on_chunk:
                self.on_chunk()
            callback(data[i:i + self.chunk])

    def quit(self):
        self.closed = True

    def close(self):
        self.closed = True


@pytest.fixture
def ftp(monkeypatch):
    holder = {}

    def install(files, **kw):
        holder["ftp"] = FakeFtp(files, **kw)
        return holder["ftp"]

    monkeypatch.setattr(thumbnail, "_connect_ftp", lambda ip, code: holder["ftp"])
    return install


# --- Candidate names ----------------------------------------------------------


def test_candidates_come_from_the_file_name_and_the_subtask_name():
    assert thumbnail.candidate_names("Family one.3mf", "Family one") == [
        "Family one.3mf",
        "Family one.gcode.3mf",
        "Family_one.gcode.3mf",
        "Family_one.3mf",
    ]


def test_an_sd_card_gcode_path_is_not_a_candidate():
    assert thumbnail.candidate_names("/data/Metadata/plate_2.gcode", "Cube") == [
        "Cube.gcode.3mf", "Cube.3mf",
    ]


def test_no_names_no_candidates():
    assert thumbnail.candidate_names("", "") == []


# --- Lookup -------------------------------------------------------------------


@pytest.mark.parametrize("folder", ["/", "/cache", "/model", "/data"])
def test_the_3mf_is_found_in_any_of_the_storage_folders(ftp, folder):
    path = folder.rstrip("/") + "/Cube.gcode.3mf"
    ftp({path: _3mf(1)})
    assert thumbnail.fetch_thumbnail("ip", "code", "/data/Metadata/plate_1.gcode", "Cube") == b"PNG1"


def test_no_matching_3mf_is_a_miss_not_some_other_jobs_picture(ftp):
    """The old code fell back to the last 3MF listed: the wrong job's picture."""
    fake = ftp({"/cache/Other job.3mf": _3mf(1)})
    assert thumbnail.fetch_thumbnail("ip", "code", "Cube.3mf", "Cube") is None
    assert fake.retrieved == []
    assert fake.closed


def test_one_connection_walks_the_folders_and_stops_at_the_first_hit(ftp):
    fake = ftp({"/cache/Cube.3mf": _3mf(1), "/model/Cube.3mf": _3mf(2)})
    assert thumbnail.fetch_thumbnail("ip", "code", "Cube.3mf", "Cube") == b"PNG1"
    assert fake.retrieved == ["/cache/Cube.3mf"]


# --- Which picture --------------------------------------------------------------


def test_the_plate_comes_from_the_gcode_path(ftp):
    ftp({"/Cube.gcode.3mf": _3mf(1, 2, 3)})
    assert thumbnail.fetch_thumbnail("ip", "code", "/data/Metadata/plate_2.gcode", "Cube") == b"PNG2"


def test_without_a_plate_in_the_name_the_sliced_plate_inside_the_3mf_is_used(ftp):
    ftp({"/cache/Cube.3mf": _3mf(1, 2, 3, gcode=[3])})
    assert thumbnail.fetch_thumbnail("ip", "code", "Cube.3mf", "Cube") == b"PNG3"


def test_the_object_mask_is_never_used_as_a_preview(ftp):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Metadata/pick_1.png", b"PICK1")
    ftp({"/cache/Cube.3mf": buf.getvalue()})
    assert thumbnail.fetch_thumbnail("ip", "code", "Cube.3mf", "Cube") is None


def test_a_missing_plate_picture_does_not_borrow_another_plates(ftp):
    ftp({"/cache/Cube.3mf": _3mf(1, 3)})
    assert thumbnail.fetch_thumbnail("ip", "code", "/data/Metadata/plate_2.gcode", "Cube") is None


# --- Failures and limits ----------------------------------------------------------


def test_a_connect_failure_raises_so_the_caller_can_retry_later(monkeypatch):
    def refuse(ip, code):
        raise ConnectionRefusedError("421 too many connections")

    monkeypatch.setattr(thumbnail, "_connect_ftp", refuse)
    with pytest.raises(ConnectionRefusedError):
        thumbnail.fetch_thumbnail("ip", "code", "Cube.3mf", "Cube")


def test_a_download_past_the_deadline_is_abandoned(ftp, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(thumbnail.time, "monotonic", lambda: clock[0])

    def tick():
        clock[0] += 10

    fake = ftp({"/cache/Cube.3mf": _3mf(1) * 4}, chunk=64, on_chunk=tick)
    with pytest.raises(TimeoutError):
        thumbnail.fetch_thumbnail("ip", "code", "Cube.3mf", "Cube", deadline_s=30)
    assert fake.closed


def test_a_cancelled_download_stops(ftp):
    cancel = threading.Event()
    fake = ftp({"/cache/Cube.3mf": _3mf(1) * 4}, chunk=64, on_chunk=cancel.set)
    with pytest.raises(thumbnail.FetchCancelled):
        thumbnail.fetch_thumbnail("ip", "code", "Cube.3mf", "Cube", cancel=cancel)
    assert fake.closed


def test_ftps_is_capped_at_tls_1_2():
    """P2S / H2C need it (BamBuddy's ftp_profiles); every model speaks 1.2."""
    ctx = thumbnail._tls_context()
    assert ctx.maximum_version == ssl.TLSVersion.TLSv1_2
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_data_connections_time_out_too():
    """Only the control socket had a timeout; a stalled transfer hung forever."""
    ftp = thumbnail._new_ftp(thumbnail._tls_context())
    assert ftp.timeout == thumbnail.SOCKET_TIMEOUT_SECONDS
