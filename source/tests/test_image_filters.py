"""Image tags and local OCR checks use only generated temporary fixtures."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

import image_filters
import scanner
from tests.test_app import FixtureCase, sample_image


def ocr_result(*words):
    return SimpleNamespace(lines=[SimpleNamespace(words=[SimpleNamespace(text=word) for word in words])])


class ImageMetadataTests(FixtureCase):
    def test_capture_names_folders_and_software_are_evidence_without_text_fallback(self):
        for filename in ("Screenshot 2026-10-13.png", "screen_shot_100.png", "Screen-Capture-42.jpg",
                         "Snipping Tool (3).png", "Snip-12.png"):
            with self.subTest(filename=filename):
                tags, reasons = image_filters._metadata_categories(self.root / filename)
                self.assertEqual(tags, ["screenshot"])
                self.assertEqual(reasons, ["Screenshot filename"])
        tags, reasons = image_filters._metadata_categories(self.root / "Screen_Shots" / "image.png")
        self.assertEqual(tags, ["screenshot"])
        self.assertEqual(reasons, ["Screenshot folder"])
        tags, reasons = image_filters._metadata_categories(self.root / "image.png", "Greenshot 1.3")
        self.assertEqual(tags, ["screenshot"])
        self.assertEqual(reasons, ["Screen capture software metadata"])
        with patch("image_filters._internet_zone", return_value=False):
            for filename in ("Screenshotography.png", "Text document.png", "receipt.jpg", "camera.jpg"):
                self.assertEqual(image_filters._metadata_categories(self.root / filename, "Photoshop"), ([], []))

    def test_download_tags_can_overlap_with_screenshot_and_detected_text(self):
        path = self.root / "Downloads" / "Screenshot.png"
        with patch("image_filters.image_text_status", return_value="detected"):
            tags, reasons, status = image_filters.classify_image(path, sample_image())
        self.assertEqual(tags, ["screenshot", "downloaded", "text"])
        self.assertEqual(status, "detected")
        self.assertEqual(reasons, ["Screenshot filename", "Downloads folder",
                                   "Text detected locally with Windows OCR"])
        for filename in ("download.png", "Downloaded (2).jpg", "image_download_123.png"):
            with self.subTest(filename=filename):
                tags, reasons = image_filters._metadata_categories(self.root / filename)
                self.assertEqual(tags, ["downloaded"])
                self.assertEqual(reasons, ["Downloaded filename"])

    def test_zone_signal_requires_an_internet_or_restricted_zone_in_correct_section(self):
        path = self.root / "camera.png"
        for content, expected in (
                (b"[ZoneTransfer]\r\nZoneId=3\r\n", True),
                (b"[zonetransfer]\nZoneId = 4\n", True),
                ("[ZoneTransfer]\nZoneId=3\n".encode("utf-16"), True),
                (b"[ZoneTransfer]\nZoneId=0\n", False),
                (b"[ZoneTransfer]\nZoneId=2\n", False),
                (b"[AnotherSection]\nZoneId=3\n", False),
                (b"ZoneId=3\n", False),
                (b"[ZoneTransfer]\nHostUrl=https://example.invalid/ZoneId=3\n", False)):
            with self.subTest(content=content):
                with patch("image_filters._read_zone_stream", return_value=content):
                    self.assertEqual(image_filters._internet_zone(path), expected)

    def test_download_zone_details_are_not_exposed_in_labels(self):
        private_url = "https://example.invalid/private/generated-name.png"
        content = f"[ZoneTransfer]\nZoneId=3\nHostUrl={private_url}\n".encode()
        with patch("image_filters._read_zone_stream", return_value=content):
            tags, reasons, status = image_filters.classify_image(self.root / "image.png")
        self.assertEqual(tags, ["downloaded"])
        self.assertEqual(reasons, ["Windows Internet zone metadata"])
        self.assertEqual(status, "error")
        self.assertNotIn(private_url, repr((tags, reasons, status)))

    def test_missing_ocr_keeps_metadata_and_reports_unavailable(self):
        context = image_filters._OcrContext(reason="No Windows OCR language is installed.")
        with patch("image_filters._ocr_context", return_value=context):
            tags, reasons, status = image_filters.classify_image(self.root / "Screenshot.png", sample_image())
            capabilities = image_filters.ocr_capabilities()
        self.assertEqual(tags, ["screenshot"])
        self.assertEqual(status, "unavailable")
        self.assertFalse(capabilities["available"])
        self.assertEqual(capabilities["engine"], "Windows OCR")
        self.assertNotIn("text", tags)

    def test_corrupt_capture_image_retains_metadata_and_exact_matching(self):
        first = self.root / "Screenshot-broken.jpg"
        second = self.root / "Screenshot-copy.jpg"
        first.write_bytes(b"Generated invalid jpeg duplicate bytes")
        second.write_bytes(first.read_bytes())
        result = scanner.scan_folder(self.root)
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.groups[0]["kind"], "exact")
        self.assertEqual(len(result.errors), 2)
        for photo in result.photos.values():
            self.assertFalse(photo.previewAvailable)
            self.assertEqual(photo.imageCategories, ["screenshot"])
            self.assertEqual(photo.imageTextStatus, "error")

    def test_scanner_publishes_tags_and_ocr_status_only_for_images(self):
        picture = sample_image()
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Software", "Snipping Tool")
        picture.save(self.root / "photo.png", pnginfo=metadata)
        (self.root / "file.custom").write_bytes(b"Generated arbitrary file")
        (self.root / "clip.mp4").write_bytes(b"Generated video placeholder")
        with patch("scanner.classify_image", return_value=(["screenshot", "text"], ["Fixed evidence"], "detected")) as classify:
            result = scanner.scan_folder(self.root)
        classify.assert_called_once()
        self.assertEqual(classify.call_args.args[2], "Snipping Tool")
        photo = next(photo for photo in result.photos.values() if photo.name == "photo.png")
        self.assertEqual(photo.public()["imageCategories"], ["screenshot", "text"])
        self.assertEqual(photo.public()["imageCategoryReasons"], ["Fixed evidence"])
        self.assertEqual(photo.public()["imageTextStatus"], "detected")
        self.assertNotIn("recognizedText", photo.public())
        for photo in result.photos.values():
            if photo.mediaType != "image":
                self.assertEqual(photo.imageCategories, [])
                self.assertEqual(photo.imageTextStatus, "not-image")

    def test_unavailable_text_detection_does_not_interrupt_duplicate_comparison(self):
        self.write_exact_pair()
        with patch("scanner.classify_image", return_value=([], [], "unavailable")):
            result = scanner.scan_folder(self.root)
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.groups[0]["kind"], "exact")
        self.assertEqual(result.errors, [])
        self.assertTrue(all(photo.previewAvailable for photo in result.photos.values()))
        self.assertTrue(all(photo.imageTextStatus == "unavailable" for photo in result.photos.values()))


class TextRecognitionTests(unittest.TestCase):
    def test_minimum_real_word_evidence_rejects_isolated_logos_and_numeric_noise(self):
        self.assertTrue(image_filters._enough_text(ocr_result("Generated", "receipt", "total")))
        self.assertFalse(image_filters._enough_text(ocr_result("Logo")))
        self.assertFalse(image_filters._enough_text(ocr_result("Company", "name")))
        self.assertFalse(image_filters._enough_text(ocr_result("1", "2", "3", "4", "5", "6")))
        self.assertFalse(image_filters._enough_text(ocr_result("123456", "789012", "345678")))
        self.assertFalse(image_filters._enough_text(ocr_result("ab", "cd", "ef")))

    def test_ocr_thumbnail_and_transparency_handling_leave_source_pixels_unchanged(self):
        picture = Image.new("RGBA", (600, 400), (0, 0, 0, 0))
        ImageDraw.Draw(picture).rectangle((100, 100, 200, 200), fill=(0, 0, 0, 255))
        before = picture.tobytes()
        captured = []
        bitmap = SimpleNamespace(close=lambda: captured.append("closed"))

        class BitmapFactory:
            @staticmethod
            def create_copy_from_buffer(buffer, pixel_format, width, height):
                captured.append((buffer, width, height))
                return bitmap

        context = image_filters._OcrContext(engine=object(), bitmap=BitmapFactory,
                                            pixel_format=object(), max_dimension=120)
        with patch("image_filters._ocr_context", return_value=context), \
                patch("image_filters._recognize_image", new=AsyncMock(return_value=True)):
            self.assertEqual(image_filters.image_text_status(picture), "detected")
        buffer, width, height = captured[0]
        self.assertEqual((width, height), (120, 80))
        self.assertEqual(buffer[:4], b"\xff\xff\xff\xff")
        self.assertEqual(captured[-1], "closed")
        self.assertEqual(picture.size, (600, 400))
        self.assertEqual(picture.tobytes(), before)

    def test_timeout_is_reported_as_error_and_bitmap_is_closed(self):
        closed = []
        bitmap = SimpleNamespace(close=lambda: closed.append(True))
        factory = SimpleNamespace(create_copy_from_buffer=lambda *args: bitmap)
        context = image_filters._OcrContext(engine=object(), bitmap=factory, pixel_format=object())
        with patch("image_filters._ocr_context", return_value=context), \
                patch("image_filters._recognize_image", new=AsyncMock(side_effect=TimeoutError)):
            tags, reasons, status = image_filters.classify_image(Path("Screenshot.png"), sample_image())
        self.assertEqual(tags, ["screenshot"])
        self.assertEqual(status, "error")
        self.assertEqual(closed, [True])


@unittest.skipUnless(os.name == "nt", "Native text recognition requires Windows")
class NativeImageRecognitionTests(FixtureCase):
    def require_ocr(self):
        capabilities = image_filters.ocr_capabilities()
        if not capabilities["available"]:
            self.skipTest(capabilities["reason"])
        font_path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"
        if not font_path.is_file():
            self.skipTest("Generated OCR fixture requires the system Arial font")
        return ImageFont.truetype(str(font_path), 36)

    def receipt(self, transparent=False):
        font = self.require_ocr()
        picture = Image.new("RGBA" if transparent else "RGB", (900, 550),
                            (0, 0, 0, 0) if transparent else "white")
        draw = ImageDraw.Draw(picture)
        for position, text in enumerate(("Generated test receipt", "Local image text recognition",
                                        "Quantity three items", "Payment total twenty dollars")):
            draw.text((50, 40 + 95 * position), text, fill=(0, 0, 0, 255), font=font)
        return picture

    def test_native_windows_ocr_recognizes_generated_text_and_retains_no_words(self):
        picture = self.receipt()
        path = self.root / "holiday.png"
        picture.save(path)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        tags, reasons, status = image_filters.classify_image(path, picture)
        self.assertEqual(status, "detected")
        self.assertEqual(tags, ["text"])
        self.assertEqual(reasons, ["Text detected locally with Windows OCR"])
        self.assertNotIn("receipt", repr((tags, reasons, status)).lower())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_native_windows_ocr_rejects_shapes_even_with_a_text_document_filename(self):
        self.require_ocr()
        picture = sample_image(size=(900, 550))
        path = self.root / "Text-document.png"
        picture.save(path)
        tags, reasons, status = image_filters.classify_image(path, picture)
        self.assertEqual(status, "no-text")
        self.assertEqual(tags, [])
        self.assertEqual(reasons, [])

    def test_native_windows_ocr_reads_transparent_text(self):
        picture = self.receipt(transparent=True)
        before = picture.tobytes()
        self.assertEqual(image_filters.image_text_status(picture), "detected")
        self.assertEqual(picture.tobytes(), before)

    def test_native_download_zone_read_is_bounded_and_preserves_original_bytes(self):
        path = self.root / "photo.png"
        sample_image().save(path)
        before = path.read_bytes()
        stream = Path(str(path) + ":Zone.Identifier")
        try:
            stream.write_bytes(b"[ZoneTransfer]\r\nZoneId=3\r\nHostUrl=https://example.invalid/generated\r\n")
        except OSError as error:
            self.skipTest("Alternate data streams unavailable for generated fixtures: %s" % error)
        self.assertTrue(image_filters._internet_zone(path))
        tags, reasons = image_filters._metadata_categories(path)
        self.assertEqual(tags, ["downloaded"])
        self.assertEqual(reasons, ["Windows Internet zone metadata"])
        stream.write_bytes(b"[ZoneTransfer]\n" + b"#" * (image_filters.ZONE_MAX_BYTES + 10) + b"\nZoneId=3\n")
        self.assertFalse(image_filters._internet_zone(path))
        self.assertEqual(len(image_filters._read_zone_stream(path)), image_filters.ZONE_MAX_BYTES)
        self.assertEqual(path.read_bytes(), before)

