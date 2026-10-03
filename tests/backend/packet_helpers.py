"""Helpers shared by the packet server's tests (stage B1).

``assert_shape`` checks a response against ``tests/fixtures/packet-api.json``:
the status, the body's field names and their JSON types, the ``code``, and
the header names the fixture lists. Human ``error`` text may differ from the
fixture's sample; everything the client branches on may not.
"""
import hashlib
import io
import json
import os
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
API = json.loads((FIXTURES / "packet-api.json").read_text(encoding="utf-8"))
HOLDER = API["samples"]["holder"]
OTHER_HOLDER = "Tab2Tab2Tab2Tab2Tab2Tab2"


def case(endpoint, name):
    return API["endpoints"][endpoint]["responses"][name]


def _json_type(value):
    if isinstance(value, bool):
        return bool
    if isinstance(value, (int, float)):
        return (int, float)
    if value is None:
        return type(None)
    return type(value)


def assert_shape(response, endpoint, name, *, code=None, usage=False):
    """``response`` has the fixture case's status, body fields, field types,
    ``code`` and header names. ``code`` overrides the fixture's code (the
    build's ``bad_items`` case, whose code is the first item's). Metering's
    ``X-Docist-Usage-*`` headers are checked only with ``usage=True``: the
    test client runs with metering off unless a test turns it on."""
    expected = API["shared"][name] if endpoint == "shared" else case(endpoint, name)
    assert response.status_code == expected["status"], (response.status_code, response.data[:300])
    for header in expected.get("headers", {}):
        if header.startswith("X-Docist-Usage-") and not usage:
            continue
        assert header in response.headers, f"missing header {header}"
    body = expected.get("body")
    if body is None:
        return None
    got = response.get_json()
    assert isinstance(got, dict), response.data[:300]
    assert set(got) == set(body), (sorted(got), sorted(body))
    for key, value in body.items():
        assert isinstance(got[key], _json_type(value)), (key, got[key])
    want_code = code if code is not None else body.get("code")
    if want_code is not None:
        assert got["code"] == want_code, got
    if "items" in body:
        for item in got["items"]:
            assert set(item) == {"id", "code"}, item
    return got


def pdf_bytes(pages=1, marker="Src", size=letter):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=size)
    for i in range(pages):
        c.setFont("Helvetica", 14)
        c.drawString(72, size[1] - 72, f"{marker}-p{i}")
        c.showPage()
    c.save()
    return buf.getvalue()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def stage(client, data, name="scan.pdf", paper="letter", holder=HOLDER):
    return client.post("/packet/stage", data={
        "file": (io.BytesIO(data), name), "paper": paper, "holder": holder,
    }, content_type="multipart/form-data")


def staged(client, data, name="scan.pdf", **kw):
    response = stage(client, data, name, **kw)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def hold(client, keep=(), release=(), holder=HOLDER):
    return client.post("/packet/hold", json={
        "holder": holder, "keep": list(keep), "release": list(release)})


def file_item(item_id, row, label=None, pages=None):
    item = {"id": item_id, "kind": "file", "hash": row["key"].split(":")[0],
            "label": label or row["name"], "pages": pages}
    if pages is not None:
        item["key"] = row["key"]
        item["src_pages"] = row["pages"]
    return item


def packet(*items, title="Packet", cover=None, sections=None, **options):
    if sections is None:
        sections = [{"id": "s1", "title": "", "divider": False, "items": list(items)}]
    return {"version": 1, "title": title, "options": options, "cover": cover,
            "sections": sections}


def build(client, pkt, rows, holder=HOLDER):
    stages = {r["key"].split(":")[0]: r["stage_id"] for r in rows}
    return client.post("/packet/build", json={"packet": pkt, "stages": stages, "holder": holder})


def entry_names(client):
    root = client.staging_dir
    if not os.path.isdir(root):
        return []
    return sorted(n for n in os.listdir(root) if not n.startswith("."))


def tmp_left(client):
    return os.listdir(os.path.join(client.staging_dir, ".tmp"))
