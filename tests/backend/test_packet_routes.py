"""The packet render and build endpoints (design §4.3, §4.4, §6).

Stage B1, with B3's cover and divider builds. Responses are checked against
``tests/fixtures/packet-api.json`` with ``packet_helpers.assert_shape``; the
page-plan vectors tagged this stage or earlier run through the real build
endpoint as well as through ``test_packet_pipeline.py``.
"""
import io
import json
import multiprocessing
import os

import pytest
from PIL import Image
from pypdf import PdfReader

import app as flask_app_module
import packet_helpers as ph
from routes import packet_build, packet_stage
from utils import staging

APP = flask_app_module.app
STAGE = "B3"
CONSUMER = "pipeline"
PLAN = json.loads((ph.FIXTURES / "packet-plan.json").read_text(encoding="utf-8"))


def store():
    return staging.Store.from_config(APP.config)


def subject_of():
    with APP.test_request_context(environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        from utils.identity import anon_subject
        return anon_subject()


def png_size(response):
    return Image.open(io.BytesIO(response.data)).size


@pytest.fixture(autouse=True)
def fresh_budget():
    APP.extensions.pop("packet_render_budget", None)
    yield
    APP.extensions.pop("packet_render_budget", None)


@pytest.fixture
def jobs_run(monkeypatch):
    """Count jobs started by the render and build routes."""
    calls = []
    for module in (packet_stage, packet_build):
        real = module.run_in_job

        def spy(work, tmpdir, _real=real):
            calls.append(tmpdir)
            return _real(work, tmpdir)

        monkeypatch.setattr(module, "run_in_job", spy)
    return calls


# ---------------------------------------------------------------------------
# Renders
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind,w,width", [
    ("thumb", None, 160), ("thumb", "1", 160), ("thumb", "160", 160), ("thumb", "200", 320),
    ("thumb", "320", 320), ("thumb", "5000", 320), ("thumb", "abc", 160),
    ("page", None, 1200), ("page", "100", 160), ("page", "320", 320), ("page", "321", 1200),
    ("page", "99999", 1200), ("page", "x", 1200),
])
def test_render_widths_are_clamped(client, kind, w, width):
    row = ph.staged(client, ph.pdf_bytes(2))
    url = f"/packet/stage/{row['stage_id']}/{kind}/1" + (f"?w={w}" if w is not None else "")
    response = client.get(url)
    ph.assert_shape(response, kind, "ok")
    assert response.headers["Content-Type"] == "image/png"
    assert png_size(response)[0] == width


def test_renders_carry_cache_headers_and_answer_if_none_match(client):
    row = ph.staged(client, ph.pdf_bytes(1))
    for url in (f"/packet/stage/{row['stage_id']}/thumb/0?w=160",
                f"/packet/stage/{row['stage_id']}/page/0?w=1200"):
        response = client.get(url)
        assert response.headers["Cache-Control"] == "private, max-age=3600"
        etag = response.headers["ETag"]
        assert etag.startswith('"')
        again = client.get(url, headers={"If-None-Match": etag})
        assert again.status_code == 304
        assert again.data == b""


def test_thumbnails_are_cached_and_counted_but_page_renders_are_not(client, jobs_run):
    row = ph.staged(client, ph.pdf_bytes(30))
    sid = row["stage_id"]
    entry = os.path.join(client.staging_dir, sid)
    jobs_run.clear()   # the stage's own job
    # The stage job rendered the first 24 at 160: those are file reads.
    assert client.get(f"/packet/stage/{sid}/thumb/23").status_code == 200
    assert jobs_run == []
    # Page 25 at 160 and page 0 at 320 are misses: one job each, then cached.
    for url in (f"/packet/stage/{sid}/thumb/25", f"/packet/stage/{sid}/thumb/0?w=320"):
        assert client.get(url).status_code == 200
        assert client.get(url).status_code == 200
    assert len(jobs_run) == 2
    assert os.path.isfile(os.path.join(entry, "thumb-160-25.png"))
    assert os.path.isfile(os.path.join(entry, "thumb-320-0.png"))
    meta = store().read_meta(sid)
    assert meta["bytes"] == staging.dir_bytes(entry) > row["bytes"]
    assert store().ledger_snapshot()["bytes"] == meta["bytes"]
    # A 1200 render is a job every time and leaves nothing on disk.
    before = sorted(os.listdir(entry))
    for _ in range(2):
        assert client.get(f"/packet/stage/{sid}/page/3?w=1200").status_code == 200
    assert len(jobs_run) == 4
    assert sorted(os.listdir(entry)) == before
    assert ph.tmp_left(client) == []


def test_a_thumbnail_over_a_cap_is_served_but_not_cached(client, monkeypatch):
    row = ph.staged(client, ph.pdf_bytes(1))
    monkeypatch.setitem(APP.config, "STAGING_SUBJECT_BYTES", row["bytes"] + 10)
    response = client.get(f"/packet/stage/{row['stage_id']}/thumb/0?w=320")
    ph.assert_shape(response, "thumb", "ok")
    assert png_size(response)[0] == 320
    assert not os.path.exists(os.path.join(client.staging_dir, row["stage_id"], "thumb-320-0.png"))
    assert store().read_meta(row["stage_id"])["bytes"] == row["bytes"]


def test_a_page_over_frame_pixels_gets_the_too_large_tile(client, monkeypatch):
    from pdf_ops import limits
    row = ph.staged(client, ph.pdf_bytes(2))
    monkeypatch.setattr(limits, "FRAME_PIXELS", 10_000)
    response = client.get(f"/packet/stage/{row['stage_id']}/page/1?w=1200")
    ph.assert_shape(response, "page", "ok")
    image = Image.open(io.BytesIO(response.data)).convert("RGB")
    assert image.size == (1200, round(1200 * 792 / 612))
    assert image.getpixel((5, 5)) == staging._PLACEHOLDER_FILL


def test_a_huge_page_staged_gets_tiles_and_the_rest_render(client, monkeypatch):
    from pdf_ops import limits
    monkeypatch.setattr(limits, "FRAME_PIXELS", 160 * 210)
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    c.drawString(72, 700, "normal")
    c.showPage()
    c.setPageSize((612, 5000))
    c.drawString(72, 700, "tall")
    c.showPage()
    c.save()
    row = ph.staged(client, buf.getvalue(), "tall.pdf")
    assert row["pages"] == 2
    normal = client.get(f"/packet/stage/{row['stage_id']}/thumb/0")
    tall = client.get(f"/packet/stage/{row['stage_id']}/thumb/1")
    assert png_size(normal)[0] == 160
    tile = Image.open(io.BytesIO(tall.data)).convert("RGB")
    assert tile.size == (160, round(160 * 792 / 612))
    assert tile.getpixel((3, 3)) == staging._PLACEHOLDER_FILL
    assert Image.open(io.BytesIO(normal.data)).convert("RGB").getpixel((3, 3)) != staging._PLACEHOLDER_FILL


def test_renders_check_the_owner(client):
    row = ph.staged(client, ph.pdf_bytes(1))
    other = APP.test_client()
    ph.assert_shape(other.get(f"/packet/stage/{row['stage_id']}/thumb/0"), "thumb", "not_found")
    ph.assert_shape(other.get(f"/packet/stage/{row['stage_id']}/page/0"), "page", "not_found")


def test_the_render_budget_is_spent_only_by_misses(client, monkeypatch, jobs_run):
    monkeypatch.setitem(APP.config, "PACKET_RENDERS_PER_MINUTE", 2)
    row = ph.staged(client, ph.pdf_bytes(3))
    sid = row["stage_id"]
    jobs_run.clear()
    for _ in range(5):
        assert client.get(f"/packet/stage/{sid}/thumb/0").status_code == 200   # cached
    assert client.get(f"/packet/stage/{sid}/page/0").status_code == 200
    assert client.get(f"/packet/stage/{sid}/page/1").status_code == 200
    response = client.get(f"/packet/stage/{sid}/page/2")
    ph.assert_shape(response, "page", "busy")
    assert int(response.headers["Retry-After"]) >= 1
    ph.assert_shape(client.get(f"/packet/stage/{sid}/thumb/2?w=320"), "thumb", "busy")
    assert len(jobs_run) == 2
    assert client.get(f"/packet/stage/{sid}/thumb/2").status_code == 200   # still cached


def _hold_lane(root, subject, kind, ready, done):
    st = staging.Store(root, ttl_seconds=7200, max_age_seconds=86400)
    with st.lane(subject, kind) as taken:
        assert taken
        ready.set()
        done.wait(30)


@pytest.fixture
def lane_held_elsewhere(client):
    """Hold one of the test subject's lanes from another process."""
    ctx = multiprocessing.get_context("fork")
    started = []

    def hold(kind):
        ready, done = ctx.Event(), ctx.Event()
        proc = ctx.Process(target=_hold_lane,
                           args=(str(client.staging_dir), subject_of(), kind, ready, done))
        proc.start()
        started.append((proc, done))
        assert ready.wait(30)

    yield hold
    for proc, done in started:
        done.set()
        proc.join(30)


def test_the_render_lane_across_two_processes(client, lane_held_elsewhere, jobs_run):
    row = ph.staged(client, ph.pdf_bytes(2))
    sid = row["stage_id"]
    jobs_run.clear()
    lane_held_elsewhere("render")
    ph.assert_shape(client.get(f"/packet/stage/{sid}/page/0"), "page", "busy")
    ph.assert_shape(client.get(f"/packet/stage/{sid}/thumb/1?w=320"), "thumb", "busy")
    assert client.get(f"/packet/stage/{sid}/thumb/1").status_code == 200   # cached: no lane
    assert jobs_run == []
    # The work lane is a different lane.
    ph.staged(client, ph.pdf_bytes(1, marker="other"))


def test_a_limit_in_a_render_job_is_a_400_with_a_code(client, monkeypatch):
    from pdf_ops import limits
    row = ph.staged(client, ph.pdf_bytes(1))

    def hog(*args):
        raise MemoryError("simulated")

    monkeypatch.setattr(staging, "render_work", hog)
    body = ph.assert_shape(client.get(f"/packet/stage/{row['stage_id']}/page/0"), "page", "limit")
    assert body["error"] == limits.memory_message()
    ph.assert_shape(client.get(f"/packet/stage/{row['stage_id']}/thumb/0?w=320"), "thumb", "limit")


def test_a_render_server_failure_is_a_500_with_a_code(client, monkeypatch):
    row = ph.staged(client, ph.pdf_bytes(1))
    monkeypatch.setattr(packet_stage, "run_in_job", lambda work, tmp: (_ for _ in ()).throw(OSError("x")))
    ph.assert_shape(client.get(f"/packet/stage/{row['stage_id']}/page/0"), "page", "server_error")
    ph.assert_shape(client.get(f"/packet/stage/{row['stage_id']}/thumb/0?w=320"), "thumb", "server_error")


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def _texts(reader):
    return [page.extract_text() or "" for page in reader.pages]


def test_build_happy_path(client):
    a = ph.staged(client, ph.pdf_bytes(3, marker="A"), "a.pdf")
    b = ph.staged(client, b"# Notes\n\nNotesBody\n", "notes.md")
    pkt = ph.packet(
        ph.file_item("i1", a, pages=[{"src": 2, "rot": 0}, {"src": 0, "rot": 90}]),
        {"id": "i2", "kind": "blank"},
        ph.file_item("i3", b),
        title="Application - J. Pluto",
    )
    response = ph.build(client, pkt, [a, b])
    body = ph.assert_shape(response, "build", "ok")
    assert body["pages"] == 3 + b["pages"]
    assert body["filename"].endswith("_Application_-_J._Pluto.pdf")
    out = os.path.join(client.output_dir, body["filename"])
    reader = PdfReader(out)
    texts = _texts(reader)
    assert len(texts) == body["pages"]
    assert "A-p2" in texts[0] and "A-p0" in texts[1] and "NotesBody" in texts[3]
    assert reader.pages[1].rotation == 90
    download = client.get(f"/download?filename={body['filename']}")
    assert download.status_code == 200 and download.data[:5] == b"%PDF-"
    assert ph.tmp_left(client) == []


def test_build_is_named_after_the_cover_or_packet(client):
    a = ph.staged(client, ph.pdf_bytes(1))
    pkt = ph.packet(ph.file_item("i1", a), title="")
    body = ph.build(client, pkt, [a]).get_json()
    assert body["filename"].endswith("_packet.pdf")


def test_build_with_a_cover_and_dividers_is_named_after_the_cover(client):
    """B3: cover and divider pages build through the endpoint in the build's
    job, and the stored name comes from the cover title."""
    a = ph.staged(client, ph.pdf_bytes(2, marker="A"))
    b = ph.staged(client, ph.pdf_bytes(1, marker="B"))
    sections = [
        {"id": "s1", "title": "Resume", "divider": True, "items": [ph.file_item("i1", a)]},
        {"id": "s2", "title": "References", "divider": True, "items": [ph.file_item("i2", b)]},
    ]
    cover = {"title": "Application", "subtitle": "J. Pluto", "date": "", "note": ""}
    pkt = ph.packet(cover=cover, sections=sections, recto_starts=True)
    body = ph.assert_shape(ph.build(client, pkt, [a, b]), "build", "ok")
    assert body["filename"].endswith("_Application.pdf")
    reader = PdfReader(os.path.join(client.output_dir, body["filename"]))
    texts = [page.extract_text() or "" for page in reader.pages]
    # cover, pad, divider, A1, A2, pad, divider, B1
    assert body["pages"] == len(texts) == 8
    assert "Application" in texts[0] and "Resume" in texts[2] and "References" in texts[6]
    assert [o.title for o in reader.outline if not isinstance(o, list)] == ["Resume", "References"]


def test_build_refuses_a_cover_whose_fields_are_not_text(client):
    a = ph.staged(client, ph.pdf_bytes(1))
    pkt = ph.packet(ph.file_item("i1", a), cover={"title": ["x"]})
    body = ph.assert_shape(ph.build(client, pkt, [a]), "build", "invalid")
    assert body["items"] == []


def test_build_renews_the_holder_on_every_entry(client):
    a = ph.staged(client, ph.pdf_bytes(1), holder=ph.OTHER_HOLDER)
    pkt = ph.packet(ph.file_item("i1", a))
    assert ph.build(client, pkt, [a]).status_code == 200
    assert ph.HOLDER in store().read_meta(a["stage_id"])["holders"]


def test_build_refuses_expired_stale_and_out_of_range_items(client):
    a = ph.staged(client, ph.pdf_bytes(3, marker="A"), "a.pdf")
    b = ph.staged(client, ph.pdf_bytes(2, marker="B"), "b.pdf")
    other = ph.staged(APP.test_client(), ph.pdf_bytes(1, marker="O"), "o.pdf")
    stale_count = dict(ph.file_item("i3", b, pages=[{"src": 0, "rot": 0}]), src_pages=5)
    stale_key = dict(ph.file_item("i4", b, pages=[{"src": 0, "rot": 0}]), key=b["key"] + "x")
    gone = {"id": "i5", "kind": "file", "hash": "f" * 64, "label": "gone.pdf", "pages": None}
    pkt = ph.packet(
        ph.file_item("i1", other),                                   # another owner's stage
        ph.file_item("i2", a, pages=[{"src": 3, "rot": 0}]),          # past the page count
        stale_count, stale_key, gone,
        ph.file_item("i6", a, pages=[{"src": -1, "rot": 0}]),
        ph.file_item("i7", a),                                       # fine
    )
    stages = {r["key"].split(":")[0]: r["stage_id"] for r in (a, b, other)}
    response = client.post("/packet/build", json={"packet": pkt, "stages": stages, "holder": ph.HOLDER})
    body = ph.assert_shape(response, "build", "bad_items", code="expired")
    assert body["items"] == [
        {"id": "i1", "code": "expired"}, {"id": "i2", "code": "out_of_range"},
        {"id": "i3", "code": "stale"}, {"id": "i4", "code": "stale"},
        {"id": "i5", "code": "expired"}, {"id": "i6", "code": "out_of_range"},
    ]
    assert os.listdir(client.output_dir) == []


def test_build_refuses_a_stage_whose_key_has_another_hash(client):
    a = ph.staged(client, ph.pdf_bytes(1, marker="A"))
    b = ph.staged(client, ph.pdf_bytes(1, marker="B"))
    pkt = ph.packet(ph.file_item("i1", a))
    stages = {a["key"].split(":")[0]: b["stage_id"]}
    response = client.post("/packet/build", json={"packet": pkt, "stages": stages, "holder": ph.HOLDER})
    body = ph.assert_shape(response, "build", "bad_items", code="expired")
    assert body["items"] == [{"id": "i1", "code": "expired"}]


def test_build_refuses_an_expired_stage(client, monkeypatch):
    import time
    a = ph.staged(client, ph.pdf_bytes(1))
    later = time.time() + 121 * 60
    monkeypatch.setattr(staging, "_now", lambda: later)
    body = ph.assert_shape(ph.build(client, ph.packet(ph.file_item("i1", a)), [a]),
                           "build", "bad_items", code="expired")
    assert body["items"] == [{"id": "i1", "code": "expired"}]


@pytest.mark.parametrize("body", [
    None, [], {"packet": [], "stages": {}, "holder": ph.HOLDER},
    {"packet": {"sections": []}, "stages": [], "holder": ph.HOLDER},
    {"packet": {"sections": []}, "stages": {"a": 1}, "holder": ph.HOLDER},
    {"packet": {"sections": []}, "stages": {}, "holder": "short"},
    {"packet": {"sections": "x"}, "stages": {}, "holder": ph.HOLDER},
])
def test_a_malformed_build_is_invalid_with_no_items(client, body):
    response = client.post("/packet/build", data=json.dumps(body), content_type="application/json")
    got = ph.assert_shape(response, "build", "invalid", code="invalid")
    assert got["items"] == []


def test_a_build_body_that_is_not_json_is_invalid(client):
    response = client.post("/packet/build", data=b"{", content_type="application/json")
    assert ph.assert_shape(response, "build", "invalid")["items"] == []


def test_a_malformed_item_is_invalid_and_named(client):
    a = ph.staged(client, ph.pdf_bytes(1))
    pkt = ph.packet(ph.file_item("i1", a), {"id": "i2", "kind": "file", "hash": "", "pages": None})
    body = ph.assert_shape(ph.build(client, pkt, [a]), "build", "invalid")
    assert body["items"] == [{"id": "i2", "code": "invalid"}]


def test_a_build_body_over_1_mib_is_the_413(client):
    body = json.dumps({"packet": {}, "stages": {}, "holder": ph.HOLDER, "pad": "x" * (1 << 20)})
    response = client.post("/packet/build", data=body, content_type="application/json")
    ph.assert_shape(response, "build", "upload_too_large")


def test_build_byte_cap_counts_distinct_staged_pdfs(client, monkeypatch, jobs_run):
    a = ph.staged(client, ph.pdf_bytes(2, marker="A"))
    b = ph.staged(client, ph.pdf_bytes(2, marker="B"))
    size_a = os.path.getsize(store().pdf_path(a["stage_id"]))
    size_b = os.path.getsize(store().pdf_path(b["stage_id"]))
    monkeypatch.setattr(packet_build, "packet_limit_bytes", lambda user: size_a + size_b - 1)
    jobs_run.clear()
    # The same file twice counts once.
    twice = ph.packet(ph.file_item("i1", a), ph.file_item("i2", a))
    assert ph.build(client, twice, [a]).status_code == 200
    both = ph.packet(ph.file_item("i1", a), ph.file_item("i2", b))
    body = ph.assert_shape(ph.build(client, both, [a, b]), "build", "too_large")
    assert body["limit_bytes"] == size_a + size_b - 1 and body["items"] == []
    assert len(jobs_run) == 1   # refused before the job


@pytest.mark.parametrize("pkt_kw,code,name", [
    ({"contents": True}, "unsupported", "unsupported"),
])
def test_pipeline_refusals_map_to_400_with_their_code(client, pkt_kw, code, name):
    a = ph.staged(client, ph.pdf_bytes(1))
    cover = pkt_kw.pop("cover", None)
    pkt = ph.packet(ph.file_item("i1", a), cover=cover, **pkt_kw)
    body = ph.assert_shape(ph.build(client, pkt, [a]), "build", name)
    assert body["code"] == code and body["items"] == []


def test_a_packet_with_no_pages_is_empty(client):
    a = ph.staged(client, ph.pdf_bytes(1))
    pkt = ph.packet(dict(ph.file_item("i1", a, pages=[])))
    ph.assert_shape(ph.build(client, pkt, [a]), "build", "empty")


def test_the_output_page_cap_is_too_large(client, monkeypatch):
    from pdf_ops import packet as packet_mod
    monkeypatch.setattr(packet_mod, "MAX_OUTPUT_PAGES", 3)
    a = ph.staged(client, ph.pdf_bytes(4))
    body = ph.assert_shape(ph.build(client, ph.packet(ph.file_item("i1", a)), [a]),
                           "build", "too_many_pages")
    assert body["code"] == "too_large"


def test_a_staged_pdf_that_breaks_at_build_is_unreadable(client):
    a = ph.staged(client, ph.pdf_bytes(2))
    with open(store().pdf_path(a["stage_id"]), "wb") as fh:
        fh.write(b"%PDF-1.4\ngarbage\n")
    body = ph.assert_shape(ph.build(client, ph.packet(ph.file_item("i1", a)), [a]),
                           "build", "bad_items", code="unreadable")
    assert body["items"] == [{"id": "i1", "code": "unreadable"}]


def test_a_limit_in_the_build_job_is_a_400_with_items(client, monkeypatch):
    from pdf_ops import limits
    a = ph.staged(client, ph.pdf_bytes(1))

    def hog(*args):
        raise MemoryError("simulated")

    monkeypatch.setattr(packet_build, "packet_pipeline", hog)
    body = ph.assert_shape(ph.build(client, ph.packet(ph.file_item("i1", a)), [a]), "build", "limit")
    assert body["error"] == limits.memory_message()
    assert body["items"] == []
    assert os.listdir(client.output_dir) == []


def test_a_build_server_failure_is_a_500_with_a_code(client, monkeypatch):
    a = ph.staged(client, ph.pdf_bytes(1))
    monkeypatch.setattr(packet_build, "run_in_job", lambda work, tmp: (_ for _ in ()).throw(OSError("x")))
    ph.assert_shape(ph.build(client, ph.packet(ph.file_item("i1", a)), [a]), "build", "server_error")


def test_the_build_holds_the_work_lane(client, lane_held_elsewhere, jobs_run):
    a = ph.staged(client, ph.pdf_bytes(1))
    jobs_run.clear()
    lane_held_elsewhere("work")
    response = ph.build(client, ph.packet(ph.file_item("i1", a)), [a])
    ph.assert_shape(response, "build", "busy")
    assert jobs_run == []


def test_a_build_racing_the_janitor_keeps_its_files(client, monkeypatch):
    """The janitor runs while the build's job runs, at a time when every
    entry has expired: the build's shared locks keep them, and the next
    sweep removes them."""
    a = ph.staged(client, ph.pdf_bytes(2, marker="A"))
    b = ph.staged(client, ph.pdf_bytes(1, marker="B"))
    root = str(client.staging_dir)
    real = packet_build.packet_pipeline
    far = __import__("time").time() + 48 * 3600
    swept = []

    def racing(*args):
        st = staging.Store(root, ttl_seconds=7200, max_age_seconds=86400)
        swept.append(st.sweep(now=far))
        return real(*args)

    monkeypatch.setattr(packet_build, "packet_pipeline", racing)
    response = ph.build(client, ph.packet(ph.file_item("i1", a), ph.file_item("i2", b)), [a, b])
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["pages"] == 3
    assert sorted(ph.entry_names(client)) == sorted([a["stage_id"], b["stage_id"]])
    assert store().sweep(now=far) == 2
    assert ph.entry_names(client) == []


# ---------------------------------------------------------------------------
# CSRF, metering, rate limits
# ---------------------------------------------------------------------------
def test_csrf_is_required_on_every_packet_post(csrf_on):
    client = csrf_on
    a = client.post("/packet/stage", data={"file": (io.BytesIO(ph.pdf_bytes(1)), "a.pdf"),
                                           "paper": "letter", "holder": ph.HOLDER},
                    content_type="multipart/form-data")
    b = ph.hold(client, keep=[])
    c = client.post("/packet/build", json={"packet": {}, "stages": {}, "holder": ph.HOLDER})
    for response in (a, b, c):
        body = ph.assert_shape(response, "shared", "csrf")
        assert "code" not in body
        assert body["error"] == ph.API["shared"]["csrf"]["body"]["error"]
    assert ph.entry_names(client) == []


def test_metering_counts_the_build_only(client, metered):
    metered(anon=5)
    a = ph.staged(client, ph.pdf_bytes(2))
    ph.hold(client, keep=[a["stage_id"]])
    client.get(f"/packet/stage/{a['stage_id']}/thumb/0")
    client.get(f"/packet/stage/{a['stage_id']}/page/1")
    stage_response = ph.stage(client, ph.pdf_bytes(1, marker="x"))
    assert "X-Docist-Usage-Remaining" not in stage_response.headers
    response = ph.build(client, ph.packet(ph.file_item("i1", a)), [a])
    ph.assert_shape(response, "build", "ok", usage=True)
    assert response.headers["X-Docist-Usage-Limit"] == "5"
    assert response.headers["X-Docist-Usage-Remaining"] == "4"
    # A refused build costs nothing.
    bad = ph.build(client, ph.packet(ph.file_item("i1", a, pages=[{"src": 9, "rot": 0}])), [a])
    assert bad.status_code == 400
    assert bad.headers["X-Docist-Usage-Remaining"] == "4"


def test_a_build_at_zero_remaining_is_the_daily_limit_with_no_work(client, metered, jobs_run):
    metered(anon=1)
    a = ph.staged(client, ph.pdf_bytes(1))
    assert ph.build(client, ph.packet(ph.file_item("i1", a)), [a]).status_code == 200
    n = len(jobs_run)
    response = ph.build(client, ph.packet(ph.file_item("i1", a)), [a])
    body = ph.assert_shape(response, "build", "daily_limit")
    assert body["limit"] == 1
    assert len(jobs_run) == n
    # Staging still works at 0 left (not metered).
    ph.staged(client, ph.pdf_bytes(1, marker="more"))


def test_the_post_rate_limit_is_shared_and_has_no_code(client, monkeypatch):
    from utils.ratelimit import RateLimiter
    monkeypatch.setitem(APP.config, "RATE_LIMIT_FORCE", True)
    monkeypatch.setattr(APP, "limiter", RateLimiter(1, 60))
    assert ph.hold(client).status_code == 200
    body = ph.assert_shape(ph.hold(client), "shared", "rate_limit")
    assert "code" not in body


def test_the_upload_limit_entries_name_the_stage_endpoint():
    from utils import metering, uploads
    assert "packet_stage.stage" in uploads.PAID_ENDPOINTS
    assert uploads._PAGE_UPLOADS["packet.index"] == "packet_stage.stage"
    assert "packet_build" in metering.METERED_BLUEPRINTS
    assert "packet_stage" not in metering.METERED_BLUEPRINTS
    assert "packet" not in metering.METERED_BLUEPRINTS
    rules = {r.endpoint: r.rule for r in APP.url_map.iter_rules()}
    assert rules["packet_stage.stage"] == "/packet/stage"
    assert rules["packet_build.build"] == "/packet/build"


def test_a_paid_users_per_build_cap_is_the_paid_limit(client, make_user, login):
    from flask import g
    user = make_user(email="paid2@x.io", plan="monthly")
    with APP.test_request_context():
        g.user = user
        assert packet_stage.packet_limit_bytes(user) == APP.config["MAX_CONTENT_LENGTH_PAID"]
        assert packet_stage.packet_limit_bytes(None) == APP.config["MAX_CONTENT_LENGTH"]


# ---------------------------------------------------------------------------
# The page-plan vectors through the real endpoint (B1 and earlier)
# ---------------------------------------------------------------------------
def _vector_params():
    order = PLAN["stage_order"]
    params = []
    for vector in PLAN["vectors"]:
        tag = vector["stage"][CONSUMER]
        marks = []
        if order.index(tag) > order.index(STAGE):
            marks.append(pytest.mark.skip(reason=f"vector tagged {tag}, later than {STAGE}"))
        params.append(pytest.param(vector, id=vector["name"], marks=marks))
    return params


@pytest.mark.parametrize("vector", _vector_params())
def test_page_plan_vectors_through_the_build_endpoint(client, vector):
    """Stage a PDF for each ready hash, rewrite the vector's hashes and keys to
    the staged ones, and build. Vectors with a contents page are refused
    ``unsupported`` until B5; a hash with no ready row is ``expired`` (the
    page never sends one); the rest build page for page, cover and dividers
    included (B3)."""
    rows, real = {}, {}
    for fake, count in vector["page_counts"].items():
        row = ph.staged(client, ph.pdf_bytes(count, marker=f"V{fake}"), f"{fake}.pdf")
        rows[fake] = row
        real[fake] = row["key"].split(":")[0]
    pkt = json.loads(json.dumps(vector["packet"]))
    missing = []
    for section in pkt["sections"]:
        for item in section["items"]:
            if item.get("kind") != "file":
                continue
            fake = item["hash"]
            if fake not in rows:
                missing.append(item["id"])
                item["hash"] = "0" * 64
                continue
            item["hash"] = real[fake]
            if "key" in item:
                item["key"] = rows[fake]["key"]
    response = ph.build(client, pkt, list(rows.values()))
    kinds = {p["kind"] for p in vector["expect"]["pages"]}
    if missing:
        body = ph.assert_shape(response, "build", "bad_items", code="expired")
        assert body["items"] == [{"id": i, "code": "expired"} for i in missing]
        return
    if "contents" in kinds:
        ph.assert_shape(response, "build", "unsupported")
        return
    if not vector["expect"]["pages"]:
        ph.assert_shape(response, "build", "empty")
        return
    body = ph.assert_shape(response, "build", "ok")
    expect = vector["expect"]["pages"]
    assert body["pages"] == len(expect)
    reader = PdfReader(os.path.join(client.output_dir, body["filename"]))
    for page, want in zip(reader.pages, expect):
        text = page.extract_text() or ""
        if want["kind"] == "file":
            assert f"V{want['hash']}-p{want['src']}" in text
            assert page.rotation % 360 == want["rot"]
        else:
            assert not any(f"V{fake}-p" in text for fake in real)
        if want["kind"] == "divider":
            [section] = [s for s in pkt["sections"] if s["id"] == want["section"]]
            assert (section["title"] or "Untitled section") in text
        if want["print"]:
            assert str(want["label"]) in text.split()
