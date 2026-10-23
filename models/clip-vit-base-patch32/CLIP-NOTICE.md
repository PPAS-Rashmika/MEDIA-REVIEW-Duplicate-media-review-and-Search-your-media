# Offline visual search model notice

Media Review uses the OpenAI CLIP ViT-B/32 model to compare local image content
with English object or scene descriptions. CLIP is distributed under the MIT
license, copyright (c) 2021 OpenAI. The complete original notice is preserved in
`CLIP-LICENSE.txt` and is copied beside every prepared model set.

Original project: https://github.com/openai/CLIP

Original checkpoint: https://huggingface.co/openai/clip-vit-base-patch32

Original license source, pinned revision:
https://github.com/openai/CLIP/blob/a1d071733d7111c9c014f024669f959182114e33f/LICENSE

License SHA-256:
`987e63b32f6c89ff5160e429458a872ff048e6860b590a3912e938f9da8f14db`

The distributed ONNX graphs and tokenizer/preprocessor configuration are the
quantized export published by Xenova for Transformers.js:
https://huggingface.co/Xenova/clip-vit-base-patch32/tree/d15189d7028b43f1d3e65039190477f6af591c2a

Only the two quantized encoder graphs and fixed JSON configuration files are
used. Media Review executes these graphs locally with ONNX Runtime; it does
not execute model repository code, use pickle checkpoints, or contact a hosted
inference service. Exact file sizes and SHA-256 values are recorded in the
setup helper and generated model manifest.

Model similarities are approximate relevance estimates, not probabilities.
English object and scene queries work best. The model does not identify people
by name or replace reading text with OCR.

Keep this notice and `CLIP-LICENSE.txt` with redistributed model assets. Other
application dependencies retain their separate licenses in the source
package's `licenses/dependencies` folder and `THIRD_PARTY_NOTICES.md`.
