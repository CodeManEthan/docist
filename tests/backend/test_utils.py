"""Tests for the utils package: naming, validation, cleanup, rate limiting."""
import os

import pytest

from utils.naming import display_name, result_name
from utils.validation import UploadValidationError, validate_upload
from utils.cleanup import OutputJanitor, prune_old_files
from utils.ratelimit import RateLimiter


# --------------------------------------------------------------------------
# result_name / display_name
# --------------------------------------------------------------------------
class TestResultName:
    def test_key_prefix_and_friendly_name(self):
        name = result_name("doc.pdf")
        assert len(name) == 32 + 1 + len("doc.pdf")
        assert display_name(name) == "doc.pdf"

    def test_names_are_unique(self):
        assert len({result_name("doc.pdf") for _ in range(100)}) == 100

    def test_extension_is_preserved(self):
        assert result_name("bundle.zip").endswith("_bundle.zip")

    @pytest.mark.parametrize("stored", [
        "doc.pdf", "report-merged.pdf", "0" * 31 + "_doc.pdf",
        "G" * 32 + "_doc.pdf", "0" * 32 + "_", "", None,
    ])
    def test_display_name_rejects_unkeyed(self, stored):
        assert display_name(stored) is None


# --------------------------------------------------------------------------
# validate_upload
# --------------------------------------------------------------------------
class TestValidateUpload:
    def _write(self, tmp_path, name, data):
        p = tmp_path / name
        p.write_bytes(data)
        return str(p)

    def test_real_pdf_passes(self, tmp_path, builders):
        path = builders.pdf(tmp_path / "ok.pdf")
        validate_upload(str(path))  # no exception

    def test_renamed_binary_as_pdf_rejected(self, tmp_path):
        path = self._write(tmp_path, "evil.pdf", b"\x7fELF\x02\x01\x01" + b"\x00" * 64)
        with pytest.raises(UploadValidationError):
            validate_upload(path)

    def test_real_png_passes(self, tmp_path, builders):
        path = builders.png(tmp_path / "img.png")
        validate_upload(str(path))

    def test_jpg_claiming_png_rejected(self, tmp_path, builders):
        jpg = builders.jpg(tmp_path / "img.jpg")
        renamed = tmp_path / "img.png"
        os.rename(jpg, renamed)
        with pytest.raises(UploadValidationError):
            validate_upload(str(renamed))

    def test_docx_zip_container_passes(self, tmp_path, builders):
        path = builders.docx(tmp_path / "doc.docx")
        validate_upload(str(path))

    def test_text_formats_accept_plain_text(self, tmp_path):
        path = self._write(tmp_path, "notes.md", b"# heading\nbody\n")
        validate_upload(path)

    def test_text_format_with_nul_bytes_rejected(self, tmp_path):
        path = self._write(tmp_path, "notes.txt", b"looks ok\x00but is binary")
        with pytest.raises(UploadValidationError):
            validate_upload(path)

    def test_unknown_extension_passes_through(self, tmp_path):
        path = self._write(tmp_path, "data.xyz", b"\x00\x01\x02anything")
        validate_upload(path)  # registry decides support, not the sniffer

    def test_explicit_ext_overrides_path(self, tmp_path):
        path = self._write(tmp_path, "payload.bin", b"not a pdf at all")
        with pytest.raises(UploadValidationError):
            validate_upload(path, ext=".pdf")

    def test_tiff_passes(self, tmp_path, builders):
        path = builders.tiff(tmp_path / "scan.tiff")
        validate_upload(str(path))


# --------------------------------------------------------------------------
# cleanup
# --------------------------------------------------------------------------
class TestCleanup:
    def _aged_file(self, folder, name, age, now):
        p = folder / name
        p.write_bytes(b"x")
        os.utime(p, (now - age, now - age))
        return p

    def test_old_files_removed_new_kept(self, tmp_path):
        now = 1_000_000.0
        old = self._aged_file(tmp_path, "old.pdf", age=7200, now=now)
        fresh = self._aged_file(tmp_path, "fresh.pdf", age=60, now=now)
        removed = prune_old_files(str(tmp_path), max_age=3600, now=now)
        assert removed == 1
        assert not old.exists()
        assert fresh.exists()

    def test_missing_folder_is_noop(self, tmp_path):
        assert prune_old_files(str(tmp_path / "nope"), max_age=10) == 0

    def test_subdirectories_ignored(self, tmp_path):
        (tmp_path / "sub").mkdir()
        assert prune_old_files(str(tmp_path), max_age=0, now=9e9) == 0
        assert (tmp_path / "sub").exists()

    def test_janitor_throttles(self, tmp_path):
        now = 1_000_000.0
        janitor = OutputJanitor(max_age=3600, interval=300)
        self._aged_file(tmp_path, "a.pdf", age=7200, now=now)
        assert janitor.maybe_prune(str(tmp_path), now=now) == 1
        # Within the interval: nothing happens even though a file qualifies.
        self._aged_file(tmp_path, "b.pdf", age=7200, now=now)
        assert janitor.maybe_prune(str(tmp_path), now=now + 10) == 0
        assert (tmp_path / "b.pdf").exists()
        # After the interval it runs again.
        assert janitor.maybe_prune(str(tmp_path), now=now + 301) == 1
        assert not (tmp_path / "b.pdf").exists()


# --------------------------------------------------------------------------
# rate limiter
# --------------------------------------------------------------------------
class TestRateLimiter:
    def test_allows_up_to_limit_then_blocks(self):
        rl = RateLimiter(max_requests=3, window=60)
        now = 1000.0
        assert rl.allow("ip", now=now)
        assert rl.allow("ip", now=now + 1)
        assert rl.allow("ip", now=now + 2)
        assert not rl.allow("ip", now=now + 3)

    def test_window_slides(self):
        rl = RateLimiter(max_requests=2, window=10)
        assert rl.allow("ip", now=100.0)
        assert rl.allow("ip", now=101.0)
        assert not rl.allow("ip", now=105.0)
        # First hit (t=100) leaves the window after t=110.
        assert rl.allow("ip", now=110.5)

    def test_keys_are_independent(self):
        rl = RateLimiter(max_requests=1, window=60)
        assert rl.allow("a", now=1.0)
        assert rl.allow("b", now=1.0)
        assert not rl.allow("a", now=2.0)

    def test_retry_after(self):
        rl = RateLimiter(max_requests=1, window=30)
        assert rl.allow("ip", now=100.0)
        assert not rl.allow("ip", now=110.0)
        assert rl.retry_after("ip", now=110.0) == pytest.approx(20.0)
        assert rl.retry_after("unknown", now=110.0) == 0.0

    def test_blocked_requests_do_not_extend_window(self):
        rl = RateLimiter(max_requests=1, window=10)
        assert rl.allow("ip", now=100.0)
        for t in (101.0, 105.0, 109.0):
            assert not rl.allow("ip", now=t)
        assert rl.allow("ip", now=110.5)
