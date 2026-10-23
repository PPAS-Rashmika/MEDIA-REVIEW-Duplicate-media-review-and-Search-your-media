"""Private, offline CLIP image/content embeddings for local media search.

All model inputs stay in this process. Importing this module and checking
capabilities never load models or make network requests. On Windows the native
inference library is imported before WinRT OCR to avoid a runtime initialization
conflict; neither CPU session nor image/text processing starts at import. The two CPU sessions
are created only when a scan embeds an image or an explicit search embeds text.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import stat
import sys
import threading

from PIL import Image, ImageOps

MODEL_NAME = "Xenova/clip-vit-base-patch32"
MODEL_REVISION = "d15189d7028b43f1d3e65039190477f6af591c2a"
MODEL_FILES = {
    "onnx/vision_model_quantized.onnx": (89117001, "583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299"),
    "onnx/text_model_quantized.onnx": (64504507, "73baab855d406190da9faa498cfedf65f15cf309f4cc7385b7b032e6d08e5c3a"),
    "tokenizer.json": (2224119, "f7f3b7af117d467b58374797691a6438d3e6b9e9cef800dfd5dced7f697a90cd"),
    "preprocessor_config.json": (520, "6f638fb9401a6d6296feff533ee7efe657b787c49f954f82f5906b36ef2a1b1f"),
    "tokenizer_config.json": (775, "60ba2912bc6344c94bc16bbdec27fa1209409167b6f2fdf3cfe9e65462ea3967"),
    "config.json": (4524, "493ef57ff783e42d1530c91b53469b7fdf8db8a9c1408e86998fcb7899a4f495"),
    "README.md": (1404, "19c2ef35fa89966c7fa7aa30666b4e3aeef4b075461626be55e6037133cc6aa4"),
}
EMBEDDING_SIZE = 512
MAX_TEXT_LENGTH = 77
_MODEL_SUBDIRECTORY = "clip-vit-base-patch32"
_LOCK = threading.RLock()
_VISION_SESSION = None
_TEXT_SESSION = None
_TOKENIZER = None
_NUMPY = None
_ONNX = None
_PREPROCESSOR = None
_VERIFIED_DIRECTORY = None
_LAST_ERROR = ""
_IMAGE_CACHE: OrderedDict[str, tuple[float, ...]] = OrderedDict()
_TEXT_CACHE: OrderedDict[str, tuple[float, ...]] = OrderedDict()


def _prepare_windows_runtime() -> None:
    """Load the native inference library before WinRT initializes an apartment.

    On Windows, importing ONNX Runtime 1.31 after creating a PyWinRT OCR engine
    can terminate the process inside native library initialization. Importing
    it first avoids that conflict. This does not load model assets or create
    an inference session, and an installation without this optional dependency
    can still use filename and OCR search.
    """
    global _ONNX, _LAST_ERROR
    if sys.platform != "win32":
        return
    try:
        if importlib.util.find_spec("onnxruntime") is None:
            return
        import onnxruntime
        onnxruntime.disable_telemetry_events()
        _ONNX = onnxruntime
    except (ImportError, OSError, RuntimeError, ValueError):
        _LAST_ERROR = "Offline visual search dependencies could not initialize. Text and filename search remain available."


_prepare_windows_runtime()


def models_directory() -> Path:
    """Locate companion assets without using environment variables or the CWD."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "models" / _MODEL_SUBDIRECTORY
    source = Path(__file__).resolve().parent
    release_assets = source.parent / "models" / _MODEL_SUBDIRECTORY
    if release_assets.is_dir():
        return release_assets
    return source / "models" / _MODEL_SUBDIRECTORY


def _is_reparse(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _asset(directory: Path, relative: str) -> Path:
    """Only fixed manifest paths are accepted; linked/reparse assets are rejected."""
    if relative not in MODEL_FILES:
        raise ValueError("Unknown visual search asset.")
    current = directory.parent.parent
    for component in (directory.parent.name, directory.name, *Path(relative).parts):
        current = current / component
        if _is_reparse(current):
            raise ValueError("Visual search model files cannot be symbolic links or reparse points.")
    return current


def capabilities() -> dict:
    """Cheap availability information; never instantiate the neural network."""
    result = {"available": False, "reason": "", "model": MODEL_NAME}
    with _LOCK:
        if _LAST_ERROR:
            result["reason"] = _LAST_ERROR
            return result
        for package in ("numpy", "onnxruntime", "tokenizers"):
            try:
                if importlib.util.find_spec(package) is None:
                    result["reason"] = "Visual search dependencies are unavailable. Reinstall the complete app package."
                    return result
            except (ImportError, ValueError):
                result["reason"] = "Visual search dependencies are unavailable. Reinstall the complete app package."
                return result
        directory = models_directory()
        try:
            # The model card is distributed for provenance, not needed for inference.
            for relative, (size, _) in MODEL_FILES.items():
                if relative == "README.md":
                    continue
                path = _asset(directory, relative)
                if not path.is_file() or path.stat().st_size != size:
                    result["reason"] = "Offline visual search models are missing or incomplete. Keep the models folder beside the app."
                    return result
        except (OSError, ValueError):
            result["reason"] = "Offline visual search models are missing, unreadable, or linked. Reinstall the complete models folder."
            return result
        result["available"] = True
        return result


def _verify_models(directory: Path) -> None:
    global _VERIFIED_DIRECTORY
    if _VERIFIED_DIRECTORY == directory:
        return
    for relative, (expected_size, expected_hash) in MODEL_FILES.items():
        if relative == "README.md":
            continue
        path = _asset(directory, relative)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            size = 0
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        if size != expected_size or digest.hexdigest() != expected_hash:
            raise ValueError("Offline visual search model integrity verification failed. Reinstall the models folder.")
    _VERIFIED_DIRECTORY = directory


def _runtime() -> tuple:
    global _NUMPY, _ONNX, _PREPROCESSOR
    if _NUMPY is None:
        available = capabilities()
        if not available["available"]:
            raise RuntimeError(available["reason"])
        directory = models_directory()
        _verify_models(directory)
        import numpy
        import onnxruntime
        onnxruntime.disable_telemetry_events()
        _NUMPY, _ONNX = numpy, onnxruntime
        _PREPROCESSOR = json.loads(_asset(directory, "preprocessor_config.json").read_text(encoding="utf-8"))
    return _NUMPY, _ONNX


def _session(kind: str):
    global _VISION_SESSION, _TEXT_SESSION, _TOKENIZER
    _, runtime = _runtime()
    session = _VISION_SESSION if kind == "vision" else _TEXT_SESSION
    if session is None:
        options = runtime.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.execution_mode = runtime.ExecutionMode.ORT_SEQUENTIAL
        options.log_severity_level = 3
        options.enable_profiling = False
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        model = _asset(models_directory(), f"onnx/{kind}_model_quantized.onnx")
        session = runtime.InferenceSession(str(model), sess_options=options, providers=["CPUExecutionProvider"])
        session.disable_fallback()
        if kind == "vision":
            _VISION_SESSION = session
        else:
            from tokenizers import Tokenizer
            tokenizer = Tokenizer.from_file(str(_asset(models_directory(), "tokenizer.json")))
            tokenizer.enable_truncation(max_length=MAX_TEXT_LENGTH)
            tokenizer.enable_padding(length=MAX_TEXT_LENGTH, pad_id=49407, pad_token="<|endoftext|>")
            _TOKENIZER = tokenizer
            _TEXT_SESSION = session
    return session


def _preprocess_image(image: Image.Image):
    """Apply the verified CLIP RGB / shortest edge / center crop / normalization."""
    numpy, _ = _runtime()
    settings = _PREPROCESSOR
    # The scanner already orients its decoded image. Avoid copying its full
    # resolution RGB pixels again when no additional conversion is needed.
    oriented = ImageOps.exif_transpose(image) if image.getexif().get(274, 1) != 1 else image
    rgb = oriented if oriented.mode == "RGB" else oriented.convert("RGB")
    width, height = rgb.size
    if not width or not height:
        raise ValueError("The image is empty.")
    edge = settings["size"]["shortest_edge"]
    ratio = edge / min(width, height)
    resized_width, resized_height = max(edge, int(width * ratio)), max(edge, int(height * ratio))
    crop_width, crop_height = settings["crop_size"]["width"], settings["crop_size"]["height"]
    left, top = (resized_width - crop_width) // 2, (resized_height - crop_height) // 2
    # Avoid allocating huge resized panoramas: sample the same central source
    # region directly. Ordinary photos take the standard resize then crop path.
    if resized_width * resized_height > 4_000_000:
        box = (left / ratio, top / ratio, (left + crop_width) / ratio, (top + crop_height) / ratio)
        cropped = rgb.resize((crop_width, crop_height), Image.Resampling.BICUBIC, box=box)
    else:
        resized = rgb.resize((resized_width, resized_height), Image.Resampling.BICUBIC)
        cropped = resized.crop((left, top, left + crop_width, top + crop_height))
    pixels = numpy.asarray(cropped, dtype=numpy.float32)
    pixels *= numpy.float32(settings["rescale_factor"])
    pixels = (pixels - numpy.asarray(settings["image_mean"], dtype=numpy.float32)) / numpy.asarray(settings["image_std"], dtype=numpy.float32)
    return numpy.ascontiguousarray(pixels.transpose(2, 0, 1)[None, ...], dtype=numpy.float32)


def _normalized_embedding(outputs: list, session) -> tuple[float, ...]:
    numpy, _ = _runtime()
    named = {item.name: value for item, value in zip(session.get_outputs(), outputs)}
    vector = named.get("image_embeds")
    if vector is None:
        vector = named.get("text_embeds")
    if vector is None:
        raise ValueError("The offline visual model returned an incompatible embedding.")
    vector = numpy.asarray(vector, dtype=numpy.float32).reshape(-1)
    if vector.size != EMBEDDING_SIZE or not numpy.isfinite(vector).all():
        raise ValueError("The offline visual model returned an invalid embedding.")
    magnitude = float(numpy.linalg.norm(vector))
    if not math.isfinite(magnitude) or magnitude <= 0:
        raise ValueError("The offline visual model returned an empty embedding.")
    return tuple(float(value) for value in vector / magnitude)


def _remember(cache: OrderedDict, key: str, vector: tuple[float, ...], limit: int) -> None:
    cache[key] = vector
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def _failure(error: Exception) -> None:
    global _LAST_ERROR
    if isinstance(error, (FileNotFoundError, ImportError, ModuleNotFoundError)):
        _LAST_ERROR = "Offline visual search files are unavailable. Reinstall the complete app and models folder."
    elif "integrity" in str(error).lower():
        _LAST_ERROR = "Offline visual search model integrity verification failed. Reinstall the models folder."
    else:
        _LAST_ERROR = "Offline visual search could not run on this computer. Text and filename search remain available."


def embed_image(image: Image.Image) -> tuple[float, ...] | None:
    """Embed decoded pixels during an explicit scan; gracefully degrade on failure."""
    with _LOCK:
        if _LAST_ERROR:
            return None
        try:
            pixels = _preprocess_image(image)
            key = hashlib.sha256(pixels.tobytes()).hexdigest()
            if key in _IMAGE_CACHE:
                _IMAGE_CACHE.move_to_end(key)
                return _IMAGE_CACHE[key]
            session = _session("vision")
            vector = _normalized_embedding(session.run(None, {"pixel_values": pixels}), session)
            _remember(_IMAGE_CACHE, key, vector, 512)
            return vector
        except Exception as error:
            _failure(error)
            return None


def embed_text(query: str) -> tuple[float, ...] | None:
    """Embed an explicit English object/scene query, without network activity."""
    normalized = " ".join(str(query).split())
    if not normalized:
        return None
    with _LOCK:
        if _LAST_ERROR:
            return None
        key = normalized.casefold()
        if key in _TEXT_CACHE:
            _TEXT_CACHE.move_to_end(key)
            return _TEXT_CACHE[key]
        try:
            numpy, _ = _runtime()
            session = _session("text")
            prompt = normalized if normalized.lower().startswith(("a photo of ", "a photograph of ", "an image of ")) else f"a photo of {normalized}"
            encoded = _TOKENIZER.encode(prompt)
            values = {
                "input_ids": numpy.asarray([encoded.ids], dtype=numpy.int64),
                "attention_mask": numpy.asarray([encoded.attention_mask], dtype=numpy.int64),
            }
            inputs = {item.name: values[item.name] for item in session.get_inputs()}
            vector = _normalized_embedding(session.run(None, inputs), session)
            _remember(_TEXT_CACHE, key, vector, 128)
            return vector
        except Exception as error:
            _failure(error)
            return None


def clear_caches() -> None:
    """Discard private temporary embedding caches when replacing a scan."""
    with _LOCK:
        _IMAGE_CACHE.clear()
        _TEXT_CACHE.clear()
