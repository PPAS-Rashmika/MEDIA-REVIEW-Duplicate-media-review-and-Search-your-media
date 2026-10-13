"""Private OCR content indexing uses generated words and temporary images."""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image, ImageDraw, ImageFont

import image_filters
import scanner
from tests.test_app import FixtureCase, sample_image


def recognized_lines(*lines):
    return SimpleNamespace(lines=[SimpleNamespace(words=[SimpleNamespace(text=word) for word in line])
                                  for line in lines])


class OcrContentTests(unittest.TestCase):
    def setUp(self):
        image_filters.clear_ocr_cache()

    def tearDown(self):
        image_filters.clear_ocr_cache()

    def context(self, result):
        bitmap = Mock()
        factory = SimpleNamespace(create_copy_from_buffer=Mock(return_value=bitmap))
        engine = SimpleNamespace(recognize_async=AsyncMock(return_value=result))
        context = image_filters._OcrContext(engine=engine, bitmap=factory, pixel_format=object(),
                                            language="generated-test-language")
        return context, engine, bitmap, factory

    def test_single_name_number_and_single_character_are_retained_below_text_category_threshold(self):
        for word in ("Alice", "4281", "A", "කමල්"):
            with self.subTest(word=word):
                context, engine, _, _ = self.context(recognized_lines((word,)))
                analysis = asyncio.run(image_filters._recognize_image(context, object()))
                self.assertEqual(analysis.status, "no-text")
                self.assertEqual(analysis.text, word)
                self.assertEqual(engine.recognize_async.await_count, 1)

    def test_category_and_content_extraction_reuse_one_recognition_and_leave_pixels_unchanged(self):
        picture = sample_image()
        before = picture.tobytes()
        context, engine, bitmap, factory = self.context(recognized_lines(("Alice", "4281")))
        with patch("image_filters._ocr_context", return_value=context), \
                patch("image_filters._metadata_categories", return_value=([], [])):
            tags, reasons, status = image_filters.classify_image(Path("image.png"), picture)
            text, search_status = image_filters.extract_image_text(picture)
            repeated = image_filters.extract_image_text(picture.copy())
        self.assertEqual((tags, reasons, status), ([], [], "no-text"))
        self.assertEqual((text, search_status), ("Alice 4281", "indexed"))
        self.assertEqual(repeated, (text, search_status))
        self.assertEqual(engine.recognize_async.await_count, 1)
        factory.create_copy_from_buffer.assert_called_once()
        bitmap.close.assert_called_once()
        self.assertEqual(picture.tobytes(), before)

    def test_full_text_keeps_lines_punctuation_and_enforces_private_index_bound(self):
        lines = (("Generated", "customer", "receipt"), ("Alice", "#4281", "$42.50"))
        context, _, _, _ = self.context(recognized_lines(*lines))
        analysis = asyncio.run(image_filters._recognize_image(context, object()))
        self.assertEqual(analysis.status, "detected")
        self.assertEqual(analysis.text, "Generated customer receipt\nAlice #4281 $42.50")
        context, _, _, _ = self.context(recognized_lines(("x" * (image_filters.OCR_TEXT_LIMIT + 20),)))
        analysis = asyncio.run(image_filters._recognize_image(context, object()))
        self.assertEqual(len(analysis.text), image_filters.OCR_TEXT_LIMIT)

    def test_empty_recognition_is_indexed_empty_and_unavailable_returns_no_private_text(self):
        picture = sample_image()
        context, engine, _, _ = self.context(recognized_lines())
        with patch("image_filters._ocr_context", return_value=context):
            self.assertEqual(image_filters.extract_image_text(picture), ("", "empty"))
            self.assertEqual(image_filters.image_text_status(picture), "no-text")
        self.assertEqual(engine.recognize_async.await_count, 1)
        context = image_filters._OcrContext(reason="No OCR language installed.")
        with patch("image_filters._ocr_context", return_value=context):
            self.assertEqual(image_filters.extract_image_text(picture), ("", "unavailable"))
            self.assertEqual(image_filters.image_text_status(picture), "unavailable")

    def test_recognition_failure_does_not_publish_partial_text_and_always_closes_bitmap(self):
        picture = sample_image()
        context, engine, bitmap, _ = self.context(recognized_lines(("Private", "partial", "name")))
        engine.recognize_async.side_effect = TimeoutError
        with patch("image_filters._ocr_context", return_value=context):
            self.assertEqual(image_filters.image_text_status(picture), "error")
            self.assertEqual(image_filters.extract_image_text(picture), ("", "error"))
        self.assertEqual(engine.recognize_async.await_count, 1)
        bitmap.close.assert_called_once()

    def test_clearing_ocr_cache_discards_recognized_words_and_allows_a_fresh_read(self):
        picture = sample_image()
        context, engine, _, _ = self.context(recognized_lines(("Alice",)))
        with patch("image_filters._ocr_context", return_value=context):
            self.assertEqual(image_filters.extract_image_text(picture), ("Alice", "indexed"))
            image_filters.clear_ocr_cache()
            engine.recognize_async.return_value = recognized_lines(("Bob",))
            self.assertEqual(image_filters.extract_image_text(picture), ("Bob", "indexed"))
        self.assertEqual(engine.recognize_async.await_count, 2)


class ScannerContentIndexTests(FixtureCase):
    def test_decoded_oriented_image_populates_private_indices_and_public_diagnostics(self):
        path = self.root / "image.png"
        sample_image(size=(320, 240)).save(path)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        embedding = (1.0, *([0.0] * 511))
        with patch("scanner.classify_image", return_value=([], [], "no-text")) as classify, \
                patch("scanner.extract_image_text", return_value=("Alice 4281", "indexed")) as text, \
                patch("visual_search.embed_image", return_value=embedding) as visual:
            photo = scanner._inspect(path, self.root)
        self.assertEqual(photo.ocrText, "Alice 4281")
        self.assertEqual(photo.visualEmbedding, embedding)
        self.assertEqual(photo.ocrSearchStatus, "indexed")
        self.assertEqual(photo.visualStatus, "indexed")
        self.assertEqual(photo.imageCategories, [])
        self.assertEqual(photo.imageTextStatus, "no-text")
        self.assertTrue(photo.previewAvailable)
        self.assertTrue(photo.pixel_hash)
        self.assertIs(classify.call_args.args[1], text.call_args.args[0])
        self.assertIs(text.call_args.args[0], visual.call_args.args[0])
        self.assertEqual(visual.call_args.args[0].size, (320, 240))
        public = photo.public()
        self.assertEqual(public["ocrSearchStatus"], "indexed")
        self.assertEqual(public["visualStatus"], "indexed")
        self.assertNotIn("ocrText", public)
        self.assertNotIn("visualEmbedding", public)
        self.assertNotIn("Alice", repr(public))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_failed_ocr_or_visual_indexing_preserves_previews_and_exact_duplicates(self):
        self.write_exact_pair()
        with patch("scanner.classify_image", return_value=([], [], "error")), \
                patch("scanner.extract_image_text", return_value=("", "error")), \
                patch("visual_search.embed_image", side_effect=RuntimeError("generated model failure")):
            result = scanner.scan_folder(self.root)
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(result.groups[0]["kind"], "exact")
        for photo in result.photos.values():
            self.assertTrue(photo.previewAvailable)
            self.assertTrue(photo.pixel_hash)
            self.assertEqual(photo.ocrSearchStatus, "error")
            self.assertEqual(photo.visualStatus, "error")
            self.assertEqual(photo.ocrText, "")
            self.assertIsNone(photo.visualEmbedding)

    def test_nonimage_files_do_not_run_ocr_or_visual_models(self):
        (self.root / "invoice.custom").write_bytes(b"Generated plain nonimage file")
        with patch("scanner.classify_image") as classify, patch("scanner.extract_image_text") as text, \
                patch("visual_search.embed_image") as visual:
            result = scanner.scan_folder(self.root)
        classify.assert_not_called()
        text.assert_not_called()
        visual.assert_not_called()
        photo = next(iter(result.photos.values()))
        self.assertEqual(photo.ocrSearchStatus, "not-image")
        self.assertEqual(photo.visualStatus, "not-image")
        self.assertEqual(photo.ocrText, "")
        self.assertIsNone(photo.visualEmbedding)


@unittest.skipUnless(os.name == "nt", "Native OCR content requires Windows")
class NativeOcrContentTests(FixtureCase):
    def test_native_single_name_and_number_are_searchable_without_text_category(self):
        capability = image_filters.ocr_capabilities()
        if not capability["available"]:
            self.skipTest(capability["reason"])
        font_path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"
        if not font_path.is_file():
            self.skipTest("Generated OCR fixture requires the system Arial font")
        image_filters.clear_ocr_cache()
        picture = Image.new("RGB", (600, 260), "white")
        draw = ImageDraw.Draw(picture)
        font = ImageFont.truetype(str(font_path), 64)
        draw.text((40, 35), "Alice", fill="black", font=font)
        draw.text((40, 135), "4281", fill="black", font=font)
        path = self.root / "generated-name-number.png"
        picture.save(path)
        before = path.read_bytes()
        with patch("visual_search.embed_image", return_value=None), \
                patch("visual_search.capabilities", return_value={"available": False, "reason": "Generated test"}):
            photo = scanner._inspect(path, self.root)
        self.assertIn("alice", photo.ocrText.casefold())
        self.assertIn("4281", photo.ocrText)
        self.assertEqual(photo.ocrSearchStatus, "indexed")
        self.assertEqual(photo.imageTextStatus, "no-text")
        self.assertNotIn("text", photo.imageCategories)
        self.assertEqual(path.read_bytes(), before)
        image_filters.clear_ocr_cache()
