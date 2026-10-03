"""The packet staging store and its stage and hold endpoints (design §4.2, §6).

Stage B1. Responses are checked against ``tests/fixtures/packet-api.json``
with ``packet_helpers.assert_shape``. Limits are scaled down through the app
config, as launch-hardening's tests do (its §15): no test builds a full-size
file.
"""
import io
import json
import multiprocessing
import os
import threading
import time

import pytest
from pypdf import PdfReader, PdfWriter

import app as flask_app_module
import packet_helpers as ph
from routes import packet_stage
from utils import staging
from utils.cleanup import StagingJanitor

APP = flask_app_module.app
MD = b"# Packet notes\n\nSome text for the packet.\n"


def store():
    return staging.Store.from_config(APP.config)


def subject_of(client):
    """The metering subject the test client's requests resolve to."""
    with APP.test_request_context(environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        from utils.identity import anon_subject
        return anon_subject()


@pytest.fixture
def clock(monkeypatch):
    """A settable wall clock for the store."""
    now = [time.time()]
    monkeypatch.setattr(staging, "_now", lambda: now[0])
    return now


@pytest.fixture
def job_calls(monkeypatch):
    """Count the stage route's run_in_job calls (each one is a job)."""
    calls = []
    real = packet_stage.run_in_job

    def spy(work, tmpdir):
        calls.append(tmpdir)
        return real(work, tmpdir)

    monkeypatch.setattr(packet_stage, "run_in_job", spy)
    return calls


# ---------------------------------------------------------------------------
# Stage: convert in a job, keep only checked results
# ---------------------------------------------------------------------------
def test_stage_a_pdf_answers_the_fixture_shape(client):
    data = ph.pdf_bytes(3)
    body = ph.assert_shape(ph.stage(client, data, "scan.pdf"), "stage", "ok_pdf")
    assert body["pages"] == 3
    assert body["name"] == "scan.pdf" and body["type"] == "pdf"
    assert body["key"] == f"{ph.sha(data)}::"
    assert staging.valid_id(body["stage_id"])
    entry = os.path.join(client.staging_dir, body["stage_id"])
    assert sorted(os.listdir(entry)) == sorted(
        ["doc.pdf", "meta.json"] + [f"thumb-160-{i}.png" for i in range(3)])
    with open(os.path.join(entry, "doc.pdf"), "rb") as fh:
        assert fh.read() == data   # the PDF's own bytes, copied by the job
    assert body["bytes"] == staging.dir_bytes(entry)


def test_stage_converts_in_a_job_and_keys_by_engine_and_paper(client, job_calls, shared_list, monkeypatch):
    pids = shared_list()
    real = staging.stage_work

    def spy(*args):
        pids.append(os.getpid())
        return real(*args)

    monkeypatch.setattr(staging, "stage_work", spy)
    body = ph.assert_shape(ph.stage(client, MD, "notes.md", paper="a4"), "stage", "ok_docx")
    assert body["type"] == "md"
    assert body["key"] == f"{ph.sha(MD)}:reflow:a4"
    assert body["pages"] >= 1
    assert len(job_calls) == 1
    assert list(pids) and pids[0] != os.getpid()   # the work ran in a forked child


def test_stage_leaves_no_thread_and_nothing_in_the_temp_directory(client):
    before = threading.active_count()
    ph.staged(client, ph.pdf_bytes(2))
    ph.stage(client, b"%PDF-1.4 not really", "broken.pdf")
    assert threading.active_count() == before
    assert ph.tmp_left(client) == []
    assert os.listdir(os.path.join(client.staging_dir, ".trash")) == []


def test_dedup_by_the_whole_conversion_key(client, job_calls):
    a = ph.staged(client, MD, "notes.md", paper="letter")
    b = ph.staged(client, MD, "notes.md", paper="letter")
    c = ph.staged(client, MD, "notes.md", paper="a4")
    assert a["stage_id"] == b["stage_id"]
    assert c["stage_id"] != a["stage_id"]
    assert len(job_calls) == 2   # the repeat cost no job
    assert len(ph.entry_names(client)) == 2


def test_a_pdf_key_is_the_same_under_either_paper(client, job_calls):
    data = ph.pdf_bytes(1)
    a = ph.staged(client, data, paper="letter")
    b = ph.staged(client, data, paper="a4")
    assert a["key"] == b["key"] == f"{ph.sha(data)}::"
    assert a["stage_id"] == b["stage_id"]
    assert len(job_calls) == 1


# ---------------------------------------------------------------------------
# B7: each page's size, for its number on the page images (§4.2 Convert)
# ---------------------------------------------------------------------------
def _sized_pdf(pages, crop=None, rotate=None):
    """A PDF with one page per size in ``pages``. ``crop`` maps a page index
    to its crop box (x0, y0, x1, y1); ``rotate`` maps one to its /Rotate."""
    from pypdf.generic import NameObject, NumberObject
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=pages[0])
    for size in pages:
        c.setPageSize(size)
        c.drawString(72, 72, "sized")
        c.showPage()
    c.save()
    reader = PdfReader(io.BytesIO(buf.getvalue()))
    writer = PdfWriter()
    for index, page in enumerate(reader.pages):
        page = writer.add_page(page)
        if crop and index in crop:
            page.cropbox.lower_left = crop[index][:2]
            page.cropbox.upper_right = crop[index][2:]
        if rotate and index in rotate:
            page[NameObject("/Rotate")] = NumberObject(rotate[index])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_sizes_of_a_uniform_pdf_are_one_run(client):
    body = ph.staged(client, ph.pdf_bytes(5))
    assert body["sizes"] == [[5, 612.0, 792.0, 0, 612.0, 792.0]]


def test_sizes_of_a_mixed_pdf_run_in_page_order_and_add_up_to_pages(client):
    a4 = (595.276, 841.89)
    body = ph.staged(client, _sized_pdf([(612, 792), (612, 792), a4, (792, 612), (612, 792)]))
    assert body["sizes"] == [
        [2, 612.0, 792.0, 0, 612.0, 792.0],
        [1, 595.28, 841.89, 0, 595.28, 841.89],
        [1, 792.0, 612.0, 0, 792.0, 612.0],
        [1, 612.0, 792.0, 0, 612.0, 792.0],
    ]
    assert sum(run[0] for run in body["sizes"]) == body["pages"] == 5


def test_sizes_use_the_crop_box_clipped_to_the_media_box(client):
    data = _sized_pdf([(612, 792)] * 3, crop={0: (100, 150, 500, 700.5), 2: (-50, 0, 700, 800)})
    body = ph.staged(client, data)
    assert body["sizes"] == [
        [1, 400.0, 550.5, 0, 612.0, 792.0],
        [2, 612.0, 792.0, 0, 612.0, 792.0],
    ]


def test_sizes_carry_the_pages_own_rotate_reduced_modulo_360(client):
    data = _sized_pdf([(612, 792)] * 5, rotate={0: 90, 1: 450, 2: -90, 3: 45, 4: 360})
    body = ph.staged(client, data)
    assert [run[3] for run in body["sizes"]] == [90, 270, 45, 0]
    assert [run[0] for run in body["sizes"]] == [2, 1, 1, 1]


def test_sizes_are_kept_in_meta_and_returned_on_a_dedup_hit(client, job_calls):
    data = _sized_pdf([(612, 792), (792, 612)])
    first = ph.staged(client, data)
    assert store().read_meta(first["stage_id"])["sizes"] == first["sizes"]
    again = ph.assert_shape(ph.stage(client, data, "scan.pdf", holder=ph.OTHER_HOLDER),
                            "stage", "ok_pdf")
    assert len(job_calls) == 1
    assert again["stage_id"] == first["stage_id"]
    assert again["sizes"] == first["sizes"] == [[1, 612.0, 792.0, 0, 612.0, 792.0],
                                                [1, 792.0, 612.0, 0, 792.0, 612.0]]


def test_an_entry_staged_before_b7_answers_without_sizes(client, job_calls):
    data = ph.pdf_bytes(2)
    first = ph.staged(client, data)
    st = store()
    path = os.path.join(st.entry_dir(first["stage_id"]), staging.META_NAME)
    with open(path, encoding="utf-8") as fh:
        meta = json.load(fh)
    del meta["sizes"]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh)
    again = ph.staged(client, data, holder=ph.OTHER_HOLDER)
    assert again["stage_id"] == first["stage_id"]
    assert "sizes" not in again
    assert set(again) == {"stage_id", "pages", "name", "type", "key", "bytes"}


def _damaged_page_pdf(damage):
    """Two pages; the second has no /MediaBox or a /Rotate that isn't a number."""
    from pypdf.generic import NameObject
    reader = PdfReader(io.BytesIO(ph.pdf_bytes(2)))
    writer = PdfWriter()
    for index, page in enumerate(reader.pages):
        page = writer.add_page(page)
        if index == 1 and damage == "no-mediabox":
            del page[NameObject("/MediaBox")]
        elif index == 1:
            page[NameObject("/Rotate")] = NameObject("/Bad")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.mark.parametrize("damage", ["no-mediabox", "bad-rotate"])
def test_a_page_whose_boxes_cant_be_read_stages_without_sizes(client, damage):
    """Design §4.2: one damaged page never fails a stage. The file has no
    sizes, so its pages show no number (§2.1), and it builds with numbers off."""
    data = _damaged_page_pdf(damage)
    with pytest.raises(Exception):
        staging.page_sizes(PdfReader(io.BytesIO(data)))   # the case is real
    body = ph.staged(client, data, "damaged.pdf")
    assert body["pages"] == 2
    assert "sizes" not in body
    assert "sizes" not in store().read_meta(body["stage_id"])
    response = ph.build(client, ph.packet(ph.file_item("i1", body), page_numbers=False), [body])
    assert ph.assert_shape(response, "build", "ok")["pages"] == 2


def test_the_too_large_tile_is_marked_for_the_page_script(tmp_path):
    from PIL import Image
    out = str(tmp_path / "ph.png")
    staging._placeholder_png(160, out)
    image = Image.open(out)
    assert image.text == {"docist": "placeholder"}
    with open(out, "rb") as fh:
        head = fh.read(256)
    assert b"docist\x00placeholder" in head   # where packet.js looks


def test_dedup_is_per_owner(client):
    data = ph.pdf_bytes(1)
    a = ph.staged(client, data)
    other = APP.test_client()
    b = ph.staged(other, data)
    assert a["stage_id"] != b["stage_id"]


def test_a_pdf_the_job_cannot_open_is_unreadable(client):
    body = ph.assert_shape(ph.stage(client, b"%PDF-1.4\nnot a pdf at all\n", "bad.pdf"),
                           "stage", "unreadable")
    assert body["code"] == "unreadable"
    assert ph.entry_names(client) == []


def test_a_password_pdf_is_encrypted(client):
    writer = PdfWriter()
    writer.add_blank_page(612, 792)
    writer.encrypt("secret")
    buf = io.BytesIO()
    writer.write(buf)
    ph.assert_shape(ph.stage(client, buf.getvalue(), "locked.pdf"), "stage", "encrypted")
    assert ph.entry_names(client) == []


def test_content_that_belies_its_extension_is_rejected(client, job_calls):
    ph.assert_shape(ph.stage(client, b"MZ\x90\x00 not a pdf", "evil.pdf"), "stage", "rejected")
    assert job_calls == []


def test_an_unsupported_type_is_rejected(client):
    ph.assert_shape(ph.stage(client, b"\x00\x01", "thing.exe"), "stage", "rejected")


def test_a_converter_failure_is_rejected_with_its_reason(client, monkeypatch):
    import converters

    def boom(src, dst, opts=None):
        raise ValueError("the table is too wide")

    monkeypatch.setitem(converters._REGISTRY, ".md", boom)
    body = ph.assert_shape(ph.stage(client, MD, "notes.md"), "stage", "rejected")
    assert body["error"] == "the table is too wide"


def test_a_limit_in_the_stage_job_is_rejected_with_its_message(client, monkeypatch):
    from pdf_ops import limits

    def hog(*args):
        raise MemoryError("simulated")

    monkeypatch.setattr(staging, "stage_work", hog)
    body = ph.assert_shape(ph.stage(client, ph.pdf_bytes(1)), "stage", "rejected")
    assert body["error"] == limits.memory_message()
    assert ph.tmp_left(client) == []


def test_a_server_failure_is_a_500_with_a_code(client, monkeypatch):
    def broken(work, tmpdir):
        raise OSError("fork failed")

    monkeypatch.setattr(packet_stage, "run_in_job", broken)
    ph.assert_shape(ph.stage(client, ph.pdf_bytes(1)), "stage", "server_error")


@pytest.mark.parametrize("data", [
    {"paper": "letter", "holder": ph.HOLDER},                                     # no file
    {"file": (io.BytesIO(b"x"), ""), "paper": "letter", "holder": ph.HOLDER},     # no name
    {"file": [(io.BytesIO(b"%PDF-1"), "a.pdf"), (io.BytesIO(b"%PDF-1"), "b.pdf")],
     "paper": "letter", "holder": ph.HOLDER},                                     # two files
    {"file": (io.BytesIO(b"%PDF-1"), "a.pdf"), "other": (io.BytesIO(b"x"), "b.pdf"),
     "paper": "letter", "holder": ph.HOLDER},                                     # an extra part
    {"file": (io.BytesIO(b"%PDF-1"), "a.pdf"), "paper": "legal", "holder": ph.HOLDER},
    {"file": (io.BytesIO(b"%PDF-1"), "a.pdf"), "paper": "letter", "holder": "short"},
    {"file": (io.BytesIO(b"%PDF-1"), "a.pdf"), "paper": "letter"},
    {"file": (io.BytesIO(b"%PDF-1"), "a.pdf"), "paper": "letter", "holder": "x" * 33},
    {"file": (io.BytesIO(b"%PDF-1"), "a.pdf"), "paper": "letter", "holder": "bad holder!" * 3},
])
def test_a_malformed_stage_form_is_bad_request(client, data, job_calls):
    response = client.post("/packet/stage", data=data, content_type="multipart/form-data")
    ph.assert_shape(response, "stage", "bad_request")
    assert job_calls == []


def test_a_body_over_the_limit_is_the_413(client, monkeypatch):
    monkeypatch.setitem(APP.config, "MAX_CONTENT_LENGTH", 64 * 1024)
    response = ph.stage(client, b"%PDF-1.4\n" + b"0" * (100 * 1024))
    body = ph.assert_shape(response, "stage", "upload_too_large")
    assert body["limit_mb"] == 0.1 or body["limit_mb"] > 0


def test_a_paid_users_large_image_is_refused_at_the_free_limit(client, make_user, login, monkeypatch, job_calls):
    # Scaled down: free 1 MiB, paid 2 MiB (an 89 MB image under 50/90 MB).
    monkeypatch.setitem(APP.config, "MAX_CONTENT_LENGTH", 1024 * 1024)
    monkeypatch.setitem(APP.config, "MAX_CONTENT_LENGTH_PAID", 2 * 1024 * 1024)
    user = make_user(email="paid@x.io", plan="monthly")
    login(client, user)
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * int(1.5 * 1024 * 1024)
    body = ph.assert_shape(ph.stage(client, big, "photo.png"), "stage", "rejected")
    assert "1 MB" in body["error"]
    assert job_calls == []
    # A PDF of the same size is a paid user's to send (the stage endpoint is paid).
    pdf = ph.pdf_bytes(1) + b"%" + b"0" * int(1.5 * 1024 * 1024)
    response = ph.stage(client, pdf, "big.pdf")
    assert response.status_code == 200, response.get_json()


def test_a_pdf_over_the_per_build_cap_is_refused_at_stage(client, monkeypatch):
    monkeypatch.setattr(packet_stage, "packet_limit_bytes", lambda user: 1000)
    body = ph.assert_shape(ph.stage(client, ph.pdf_bytes(4)), "stage", "too_large")
    assert body["limit_bytes"] == 1000
    assert ph.entry_names(client) == []
    assert ph.tmp_left(client) == []


# ---------------------------------------------------------------------------
# Caps: per subject (a fresh cookie doesn't reset them) and global
# ---------------------------------------------------------------------------
def test_the_subject_file_cap_holds_across_fresh_cookies(client, monkeypatch, job_calls):
    monkeypatch.setitem(APP.config, "STAGING_SUBJECT_FILES", 2)
    for i in range(2):
        fresh = APP.test_client()
        ph.staged(fresh, ph.pdf_bytes(1, marker=f"f{i}"))
    fresh = APP.test_client()
    response = ph.stage(fresh, ph.pdf_bytes(1, marker="third"))
    body = ph.assert_shape(response, "stage", "staging_quota")
    assert body["subject_files"] == 2 and body["cap_files"] == 2
    assert len(job_calls) == 2   # refused before any work


def test_the_subject_byte_cap_holds_across_fresh_cookies(client, monkeypatch):
    first = ph.staged(APP.test_client(), ph.pdf_bytes(2, marker="a"))
    monkeypatch.setitem(APP.config, "STAGING_SUBJECT_BYTES", first["bytes"] + 100)
    response = ph.stage(APP.test_client(), ph.pdf_bytes(2, marker="b"))
    body = ph.assert_shape(response, "stage", "staging_quota")
    assert body["cap_bytes"] == first["bytes"] + 100
    assert body["subject_bytes"] == first["bytes"]


def test_a_quota_refusal_carries_the_subjects_and_the_holders_numbers(client, monkeypatch):
    other = "OtherTabOtherTabOtherTab"
    a = ph.staged(client, ph.pdf_bytes(1, marker="a"))
    b = ph.staged(client, ph.pdf_bytes(2, marker="b"))
    c = ph.staged(client, ph.pdf_bytes(3, marker="c"), holder=other)
    monkeypatch.setitem(APP.config, "STAGING_SUBJECT_FILES", 3)
    body = ph.assert_shape(ph.stage(client, ph.pdf_bytes(1, marker="d")), "stage", "staging_quota")
    assert body["subject_files"] == 3
    assert body["subject_bytes"] == a["bytes"] + b["bytes"] + c["bytes"]
    assert body["holder_files"] == 2
    assert body["holder_bytes"] == a["bytes"] + b["bytes"]
    assert body["cap_bytes"] == staging.SUBJECT_CAP_BYTES


def test_releasing_brings_the_subject_back_under_the_cap(client, monkeypatch):
    monkeypatch.setitem(APP.config, "STAGING_SUBJECT_FILES", 1)
    a = ph.staged(client, ph.pdf_bytes(1, marker="a"))
    assert ph.stage(client, ph.pdf_bytes(1, marker="b")).status_code == 400
    assert ph.hold(client, release=[a["stage_id"]]).status_code == 200
    assert ph.entry_names(client) == []   # its last holder went, so it went
    ph.staged(client, ph.pdf_bytes(1, marker="b"))


def test_expired_entries_are_swept_before_a_quota_refusal(client, monkeypatch, clock):
    monkeypatch.setitem(APP.config, "STAGING_SUBJECT_FILES", 1)
    ph.staged(client, ph.pdf_bytes(1, marker="a"))
    clock[0] += 121 * 60
    ph.staged(client, ph.pdf_bytes(1, marker="b"))
    assert len(ph.entry_names(client)) == 1


def test_the_global_cap_refuses_everyone(client, monkeypatch):
    first = ph.staged(client, ph.pdf_bytes(2, marker="a"))
    monkeypatch.setitem(APP.config, "STAGING_GLOBAL_BYTES", first["bytes"] + 100)
    response = ph.stage(APP.test_client(), ph.pdf_bytes(2, marker="b"))
    ph.assert_shape(response, "stage", "staging_full")


def test_the_caps_are_checked_again_with_the_real_size(client, monkeypatch):
    # The upload is small, its converted PDF and thumbnails are not.
    ph.staged(client, ph.pdf_bytes(1, marker="seed"))
    used = store().ledger_snapshot()["bytes"]
    monkeypatch.setitem(APP.config, "STAGING_GLOBAL_BYTES", used + len(MD) + 10)
    response = ph.stage(client, MD, "notes.md")
    ph.assert_shape(response, "stage", "staging_full")
    assert len(ph.entry_names(client)) == 1
    assert ph.tmp_left(client) == []


# ---------------------------------------------------------------------------
# Owner isolation and ids
# ---------------------------------------------------------------------------
def test_another_owner_gets_the_same_404(client):
    row = ph.staged(client, ph.pdf_bytes(2))
    other = APP.test_client()
    for url in (f"/packet/stage/{row['stage_id']}/thumb/0",
                f"/packet/stage/{row['stage_id']}/page/1?w=1200"):
        ph.assert_shape(other.get(url), "thumb", "not_found")
    body = ph.assert_shape(ph.hold(other, keep=[row["stage_id"]]), "hold", "ok")
    assert body == {"live": {}, "gone": [row["stage_id"]]}
    # Another owner's release does nothing.
    ph.hold(other, release=[row["stage_id"]])
    assert ph.hold(client, keep=[row["stage_id"]]).get_json()["live"] == {row["stage_id"]: 2}


@pytest.mark.parametrize("stage_id,n", [
    ("short", "0"), ("x" * 23, "0"), ("..passwd.............x", "0"),
    ("opGk5pSvU5a10JKiN529WQ", "0"),          # well formed, unknown
    (None, "-1"), (None, "2"), (None, "abc"), (None, "1e3"), (None, "9999999"),
])
def test_malformed_and_unknown_ids_and_indexes_get_404(client, stage_id, n):
    row = ph.staged(client, ph.pdf_bytes(2))
    sid = stage_id or row["stage_id"]
    for kind in ("thumb", "page"):
        response = client.get(f"/packet/stage/{sid}/{kind}/{n}")
        ph.assert_shape(response, kind, "not_found")


# ---------------------------------------------------------------------------
# Holds
# ---------------------------------------------------------------------------
def test_hold_answers_live_and_gone_for_kept_ids_only(client):
    a = ph.staged(client, ph.pdf_bytes(2, marker="a"))
    b = ph.staged(client, ph.pdf_bytes(1, marker="b"))
    missing = ph.API["samples"]["expired_stage_id"]
    body = ph.assert_shape(ph.hold(client, keep=[a["stage_id"], missing], release=[b["stage_id"]]),
                           "hold", "ok")
    assert body == {"live": {a["stage_id"]: 2}, "gone": [missing]}


def test_holds_renew_from_the_last_renewal(client, clock):
    row = ph.staged(client, ph.pdf_bytes(1))
    clock[0] += 100 * 60
    assert ph.hold(client, keep=[row["stage_id"]]).get_json()["live"]
    clock[0] += 100 * 60          # 200 minutes since staging, 100 since renewal
    assert ph.hold(client, keep=[row["stage_id"]]).get_json()["live"]
    clock[0] += 121 * 60          # past the TTL since the last renewal
    body = ph.hold(client, keep=[row["stage_id"]]).get_json()
    assert body == {"live": {}, "gone": [row["stage_id"]]}
    ph.assert_shape(client.get(f"/packet/stage/{row['stage_id']}/thumb/0"), "thumb", "not_found")


def test_the_ttl_comes_from_the_config(client, clock, monkeypatch):
    monkeypatch.setitem(APP.config, "STAGING_TTL_MINUTES", 5)
    row = ph.staged(client, ph.pdf_bytes(1))
    clock[0] += 6 * 60
    assert ph.hold(client, keep=[row["stage_id"]]).get_json()["gone"] == [row["stage_id"]]


def test_an_entry_expires_at_the_maximum_age_despite_renewal(client, clock):
    row = ph.staged(client, ph.pdf_bytes(1))
    for _ in range(24):
        clock[0] += 60 * 60
        body = ph.hold(client, keep=[row["stage_id"]]).get_json()
    assert body["gone"] == [row["stage_id"]]


def test_a_stage_another_tab_holds_survives_one_tabs_release(client):
    row = ph.staged(client, ph.pdf_bytes(1))
    sid = row["stage_id"]
    assert ph.hold(client, keep=[sid], holder=ph.OTHER_HOLDER).get_json()["live"] == {sid: 1}
    ph.hold(client, release=[sid])
    assert ph.entry_names(client) == [sid]
    assert ph.hold(client, keep=[sid], holder=ph.OTHER_HOLDER).get_json()["live"] == {sid: 1}
    ph.hold(client, release=[sid], holder=ph.OTHER_HOLDER)
    assert ph.entry_names(client) == []


def test_keep_adds_a_holder_to_an_entry_another_tab_staged(client):
    row = ph.staged(client, ph.pdf_bytes(1), holder=ph.OTHER_HOLDER)
    ph.hold(client, keep=[row["stage_id"]])
    meta = store().read_meta(row["stage_id"])
    assert set(meta["holders"]) == {ph.HOLDER, ph.OTHER_HOLDER}


def test_the_ninth_holder_drops_the_stalest(client, clock):
    row = ph.staged(client, ph.pdf_bytes(1), holder="H0" * 11)
    sid = row["stage_id"]
    for i in range(1, 9):
        clock[0] += 1
        ph.hold(client, keep=[sid], holder=f"H{i}" * 11)
    holders = store().read_meta(sid)["holders"]
    assert len(holders) == staging.MAX_HOLDERS
    assert "H0" * 11 not in holders and "H8" * 11 in holders


@pytest.mark.parametrize("body", [
    {"holder": "short", "keep": [], "release": []},
    {"holder": "x" * 33, "keep": [], "release": []},
    {"holder": "has space has space has space", "keep": [], "release": []},
    {"holder": ph.HOLDER, "keep": "notalist", "release": []},
    {"holder": ph.HOLDER, "keep": [1, 2], "release": []},
    {"holder": ph.HOLDER, "keep": ["a" * 22] * 0 + [f"{i:022d}" for i in range(300)],
     "release": [f"{i:022d}" for i in range(300, 501)]},
    {"holder": ph.HOLDER, "keep": ["A" * 22], "release": ["A" * 22]},
    ["not", "an", "object"],
])
def test_bad_hold_requests_are_refused(client, body):
    ph.assert_shape(client.post("/packet/hold", json=body), "hold", "bad_request")


def test_a_hold_body_that_is_not_json_is_refused(client):
    response = client.post("/packet/hold", data=b"{not json", content_type="application/json")
    ph.assert_shape(response, "hold", "bad_request")
    response = client.post("/packet/hold", data=b"[" * 100000, content_type="application/json")
    ph.assert_shape(response, "hold", "bad_request")


def test_exactly_500_ids_are_allowed(client):
    ids = [f"{i:022d}" for i in range(500)]
    body = ph.assert_shape(ph.hold(client, keep=ids[:250], release=ids[250:]), "hold", "ok")
    assert len(body["gone"]) == 250


def test_a_hold_body_over_1_mib_is_the_413(client):
    body = json.dumps({"holder": ph.HOLDER, "keep": [], "release": [], "pad": "x" * (1 << 20)})
    response = client.post("/packet/hold", data=body, content_type="application/json")
    got = ph.assert_shape(response, "hold", "upload_too_large")
    assert got["limit_mb"] == 1


# ---------------------------------------------------------------------------
# The ledger and the janitor
# ---------------------------------------------------------------------------
def test_the_ledger_counts_each_subject_and_the_total(client):
    a = ph.staged(client, ph.pdf_bytes(1, marker="a"))
    b = ph.staged(APP.test_client(), ph.pdf_bytes(2, marker="b"))
    ledger = store().ledger_snapshot()
    assert ledger["files"] == 2 and ledger["bytes"] == a["bytes"] + b["bytes"]
    (row,) = ledger["subjects"].values()
    assert set(row["entries"]) == {a["stage_id"], b["stage_id"]}


def test_the_ledger_is_rebuilt_after_a_lost_write(client):
    a = ph.staged(client, ph.pdf_bytes(1, marker="a"))
    ph.staged(client, ph.pdf_bytes(2, marker="b"))
    st = store()
    right = st.ledger_snapshot()
    # A crash between an entry's rename and its ledger write: the ledger
    # misses the entry.
    lost = json.loads(json.dumps(right))
    row = next(iter(lost["subjects"].values()))
    gone = row["entries"].pop(a["stage_id"])
    row["bytes"] -= gone["bytes"]
    row["files"] -= 1
    lost["bytes"] -= gone["bytes"]
    lost["files"] -= 1
    with open(os.path.join(client.staging_dir, ".ledger.json"), "w") as fh:
        json.dump(lost, fh)
    assert st.ledger_snapshot()["files"] == 1
    st.sweep()
    assert st.ledger_snapshot() == right


def test_a_corrupt_ledger_is_rebuilt_on_read(client):
    a = ph.staged(client, ph.pdf_bytes(1))
    with open(os.path.join(client.staging_dir, ".ledger.json"), "w") as fh:
        fh.write("{half")
    assert store().ledger_snapshot()["files"] == 1
    assert a["stage_id"] in next(iter(store().ledger_snapshot()["subjects"].values()))["entries"]


def test_the_janitor_removes_expired_entries_and_skips_one_under_lock(client, clock):
    a = ph.staged(client, ph.pdf_bytes(1, marker="a"))
    b = ph.staged(client, ph.pdf_bytes(1, marker="b"))
    st = store()
    clock[0] += 121 * 60
    fd = st.use(a["stage_id"])          # a build or render is reading it
    try:
        assert st.sweep() == 1
        assert ph.entry_names(client) == [a["stage_id"]]
    finally:
        os.close(fd)
    assert st.sweep() == 1
    assert ph.entry_names(client) == []
    assert st.ledger_snapshot()["files"] == 0
    assert b["stage_id"] not in ph.entry_names(client)


def test_old_lane_files_are_removed_and_a_held_one_kept(client, clock):
    st = store()
    with st.lane("ip:old", "work"):
        pass
    with st.lane("ip:held", "render"):
        pass
    lanes = sorted(os.listdir(st.lanes_root))
    day_ago = clock[0] - staging.LANE_MAX_AGE - 60
    for name in lanes:
        os.utime(os.path.join(st.lanes_root, name), (day_ago, day_ago))
    held_name = [n for n in lanes if n.endswith(".render")][0]
    import fcntl
    fd = os.open(os.path.join(st.lanes_root, held_name), os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        st.sweep()
    finally:
        os.close(fd)
    assert os.listdir(st.lanes_root) == [held_name]


def test_temp_directories_a_crash_left_are_removed(client, clock):
    st = store()
    left = os.path.join(st.tmp_root, "req-crashed")
    os.makedirs(os.path.join(left, "job"))
    old = clock[0] - staging.TMP_MAX_AGE - 60
    os.utime(left, (old, old))
    fresh = os.path.join(st.tmp_root, "req-running")
    os.makedirs(fresh)
    st.sweep()
    assert os.listdir(st.tmp_root) == ["req-running"]


def test_the_staging_janitor_runs_at_most_once_per_interval(client):
    janitor = StagingJanitor(interval=300)
    calls = []
    real = staging.Store.sweep
    try:
        staging.Store.sweep = lambda self, now=None: calls.append(1) or 0
        janitor.maybe_sweep(APP.config, now=1000.0)
        janitor.maybe_sweep(APP.config, now=1100.0)
        janitor.maybe_sweep(APP.config, now=1400.0)
    finally:
        staging.Store.sweep = real
    assert len(calls) == 2


def test_staging_config_comes_from_the_environment():
    assert APP.config["STAGING_TTL_MINUTES"] == int(os.environ.get("DOCIST_STAGING_TTL_MINUTES", 120))
    assert APP.config["STAGING_MAX_HOURS"] == int(os.environ.get("DOCIST_STAGING_MAX_HOURS", 24))


# ---------------------------------------------------------------------------
# The work lane
# ---------------------------------------------------------------------------
def _hold_lane(root, subject, kind, ready, done):
    st = staging.Store(root, ttl_seconds=7200, max_age_seconds=86400)
    with st.lane(subject, kind) as taken:
        assert taken
        ready.set()
        done.wait(30)


def test_the_work_lane_is_busy_while_another_process_holds_it(client, job_calls):
    ctx = multiprocessing.get_context("fork")
    ready, done = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold_lane,
                       args=(str(client.staging_dir), subject_of(client), "work", ready, done))
    proc.start()
    try:
        assert ready.wait(30)
        response = ph.stage(client, ph.pdf_bytes(1))
        body = ph.assert_shape(response, "stage", "busy")
        assert int(response.headers["Retry-After"]) >= 1
        assert body["code"] == "busy"
        assert job_calls == []
    finally:
        done.set()
        proc.join(30)
    ph.staged(client, ph.pdf_bytes(1))


def test_a_dedup_needs_no_lane(client):
    data = ph.pdf_bytes(1)
    row = ph.staged(client, data)
    with store().lane(subject_of(client), "work") as taken:
        assert taken
        assert ph.staged(client, data)["stage_id"] == row["stage_id"]
