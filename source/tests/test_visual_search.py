"""Visual search safety/preprocessing checks use generated images and tiny assets."""
from __future__ import annotations

import hashlib
import importlib.util
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import visual_search


@unittest.skipUnless(os.name == "nt" and importlib.util.find_spec("clr"), "Windows desktop runtime is unavailable")
class NativeRuntimeOrderTests(unittest.TestCase):
    def test_native_ocr_then_image_and_query_inference_do_not_exit_the_process(self):
        # This must run in a fresh process: an earlier NumPy/ORT import in this
        # test runner would hide the native initialization-order failure.
        if not visual_search.capabilities()["available"]:
            self.skipTest("Verified companion visual search models are unavailable")
        from image_filters import ocr_capabilities
        if not ocr_capabilities()["available"]:
            self.skipTest("Windows OCR language is unavailable")
        source = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(prefix="native-search-test-", dir=Path(__file__).resolve().parent) as directory:
            script = """
import math
import sys
from pathlib import Path
import clr
print('desktop initialized', flush=True)
import scanner
import visual_search
assert visual_search._VISION_SESSION is None
assert visual_search._TEXT_SESSION is None
assert visual_search._VERIFIED_DIRECTORY is None
from image_filters import ocr_capabilities
assert ocr_capabilities()['available']
print('OCR initialized without loading a model', flush=True)
from PIL import Image
folder = Path(sys.argv[1])
Image.new('RGB', (48, 32), 'orange').save(folder / 'generated.png')
result = scanner.scan_folder(folder)
assert len(result.photos) == 1
photo = next(iter(result.photos.values()))
assert photo.visualStatus == 'indexed'
assert len(photo.visualEmbedding) == 512
query = visual_search.embed_text('an orange square')
assert query is not None and len(query) == 512
assert all(math.isfinite(value) for value in (*photo.visualEmbedding, *query))
assert abs(sum(value * value for value in query) - 1) < 0.00001
assert 'ocrText' not in photo.public() and 'visualEmbedding' not in photo.public()
print('NATIVE_SEARCH_OK', flush=True)
"""
            result = subprocess.run([sys.executable, "-B", "-u", "-c", script, directory],
                                    cwd=source, capture_output=True, text=True, timeout=120,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("NATIVE_SEARCH_OK", result.stdout)


PREPROCESSOR = {
    "size": {"shortest_edge": 224}, "crop_size": {"width": 224, "height": 224},
    "image_mean": [0.48145466, 0.4578275, 0.40821073],
    "image_std": [0.26862954, 0.26130258, 0.27577711],
    "rescale_factor": 1 / 255,
}


class VisualSearchSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="visual-search-test-", dir=Path(__file__).resolve().parent)
        self.root = Path(self.directory.name) / "models" / "clip-vit-base-patch32"
        self.root.mkdir(parents=True)
        self.saved_error = visual_search._LAST_ERROR
        self.saved_verified = visual_search._VERIFIED_DIRECTORY
        visual_search._LAST_ERROR = ""
        visual_search._VERIFIED_DIRECTORY = None
        visual_search.clear_caches()

    def tearDown(self):
        visual_search._LAST_ERROR = self.saved_error
        visual_search._VERIFIED_DIRECTORY = self.saved_verified
        visual_search.clear_caches()
        Path(self.directory.name).resolve().relative_to(Path(__file__).resolve().parent)
        self.directory.cleanup()

    def test_missing_models_are_unavailable_without_loading_a_session(self):
        with patch.object(visual_search, "models_directory", return_value=self.root), \
                patch.object(visual_search.importlib.util, "find_spec", return_value=object()), \
                patch.object(visual_search, "_runtime", side_effect=AssertionError("No model load expected")):
            status = visual_search.capabilities()
        self.assertFalse(status["available"])
        self.assertIn("models", status["reason"])
        self.assertNotIn(str(self.root), status["reason"])

    def test_valid_asset_presence_is_cheap_but_hash_verification_rejects_tampering(self):
        original = b"generated trusted model fixture"
        replacement = b"generated changed model fixture"
        self.assertEqual(len(original), len(replacement))
        path = self.root / "fixture.onnx"
        path.write_bytes(replacement)
        manifest = {"fixture.onnx": (len(original), hashlib.sha256(original).hexdigest())}
        with patch.object(visual_search, "MODEL_FILES", manifest), \
                patch.object(visual_search, "models_directory", return_value=self.root), \
                patch.object(visual_search.importlib.util, "find_spec", return_value=object()):
            self.assertTrue(visual_search.capabilities()["available"])
            with self.assertRaisesRegex(ValueError, "integrity"):
                visual_search._verify_models(self.root)
        self.assertIsNone(visual_search._VERIFIED_DIRECTORY)

    def test_linked_model_directory_is_rejected_before_loading(self):
        path = self.root / "fixture.onnx"
        path.write_bytes(b"fixture")
        manifest = {"fixture.onnx": (7, hashlib.sha256(b"fixture").hexdigest())}
        with patch.object(visual_search, "MODEL_FILES", manifest), \
                patch.object(visual_search, "models_directory", return_value=self.root), \
                patch.object(visual_search.importlib.util, "find_spec", return_value=object()), \
                patch.object(visual_search, "_is_reparse", side_effect=lambda value: value.name == "models"):
            self.assertFalse(visual_search.capabilities()["available"])
            with self.assertRaisesRegex(ValueError, "reparse"):
                visual_search._verify_models(self.root)

    def test_runtime_failure_never_substitutes_a_fake_embedding(self):
        with patch.object(visual_search, "_runtime", side_effect=RuntimeError("Model unavailable")):
            self.assertIsNone(visual_search.embed_text("beach"))
        self.assertFalse(visual_search.capabilities()["available"])
        self.assertIsNone(visual_search.embed_image(Image.new("RGB", (20, 20))))

    def test_empty_query_does_not_initialize_models_and_scan_cache_can_be_cleared(self):
        visual_search._IMAGE_CACHE["fixture"] = (1.0,) * 512
        visual_search._TEXT_CACHE["fixture"] = (1.0,) * 512
        with patch.object(visual_search, "_runtime", side_effect=AssertionError("No model load expected")):
            self.assertIsNone(visual_search.embed_text(" \n\t "))
        visual_search.clear_caches()
        self.assertFalse(visual_search._IMAGE_CACHE)
        self.assertFalse(visual_search._TEXT_CACHE)

    def test_setup_preserves_verified_original_model_license(self):
        from tools.fetch_search_models import CLIP_LICENSE_SHA256, prepare_license
        prepare_license(self.root)
        copied = (self.root / "CLIP-LICENSE.txt").read_bytes()
        self.assertEqual(hashlib.sha256(copied).hexdigest(), CLIP_LICENSE_SHA256)
        self.assertIn(b"Copyright (c) 2021 OpenAI", copied)
        self.assertTrue((self.root / "CLIP-NOTICE.md").is_file())

    def test_setup_rejects_modified_original_model_license(self):
        from tools import fetch_search_models
        fake_source = Path(self.directory.name) / "source"
        notices = fake_source / "licenses" / "models"
        notices.mkdir(parents=True)
        (notices / "CLIP-LICENSE.txt").write_bytes(b"generated modified license fixture")
        with patch.object(fetch_search_models, "SOURCE", fake_source):
            with self.assertRaisesRegex(ValueError, "license is missing or modified"):
                fetch_search_models.prepare_license(self.root)
        self.assertFalse((self.root / "CLIP-LICENSE.txt").exists())


@unittest.skipUnless(importlib.util.find_spec("numpy"), "NumPy is not installed")
class VisualSearchPreprocessingTests(unittest.TestCase):
    def setUp(self):
        import numpy
        self.numpy = numpy
        self.runtime = patch.object(visual_search, "_runtime", return_value=(numpy, object()))
        self.config = patch.object(visual_search, "_PREPROCESSOR", PREPROCESSOR)
        self.runtime.start()
        self.config.start()

    def tearDown(self):
        self.runtime.stop()
        self.config.stop()

    def test_landscape_center_crop_discards_colored_edges_and_normalizes_rgb(self):
        image = Image.new("RGB", (448, 224), "red")
        image.paste(Image.new("RGB", (224, 224), "green"), (112, 0))
        pixels = visual_search._preprocess_image(image)
        self.assertEqual(pixels.shape, (1, 3, 224, 224))
        self.assertEqual(pixels.dtype, self.numpy.float32)
        self.assertTrue(pixels.flags.c_contiguous)
        expected = self.numpy.asarray([(0 - PREPROCESSOR["image_mean"][0]) / PREPROCESSOR["image_std"][0],
                                     (128 / 255 - PREPROCESSOR["image_mean"][1]) / PREPROCESSOR["image_std"][1],
                                     (0 - PREPROCESSOR["image_mean"][2]) / PREPROCESSOR["image_std"][2]], dtype=self.numpy.float32)
        self.numpy.testing.assert_allclose(pixels[0, :, 112, 112], expected, atol=1e-6)
        self.assertTrue(self.numpy.all(pixels[0, 1] == pixels[0, 1, 112, 112]))

    def test_extreme_panorama_is_bounded_and_grayscale_is_rgb(self):
        pixels = visual_search._preprocess_image(Image.new("L", (20000, 1), 200))
        self.assertEqual(pixels.shape, (1, 3, 224, 224))
        self.assertTrue(self.numpy.isfinite(pixels).all())

    def test_normalization_rejects_wrong_dimensions_and_nonfinite_outputs(self):
        class Output:
            name = "image_embeds"

        class Session:
            def get_outputs(self):
                return [Output()]

        session = Session()
        good = self.numpy.ones((1, 512), dtype=self.numpy.float32)
        vector = visual_search._normalized_embedding([good], session)
        self.assertEqual(len(vector), 512)
        self.assertAlmostEqual(sum(value * value for value in vector), 1, places=6)
        for invalid in (self.numpy.ones((1, 511)), self.numpy.zeros((1, 512)),
                        self.numpy.full((1, 512), math.nan)):
            with self.subTest(shape=invalid.shape, value=invalid[0, 0]):
                with self.assertRaises(ValueError):
                    visual_search._normalized_embedding([invalid], session)


if __name__ == "__main__":
    unittest.main()
