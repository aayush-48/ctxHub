"""Download the NLI model files into a directory (used when building the container image).

python fetch_nli_model.py <hf repo> <onnx file in repo> <target dir>
"""

import shutil
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download

repo, onnx_file, target = sys.argv[1], sys.argv[2], Path(sys.argv[3])
target.mkdir(parents=True, exist_ok=True)
for src, dst in (
    (onnx_file, "model.onnx"),
    ("tokenizer.json", "tokenizer.json"),
    ("config.json", "config.json"),
):
    shutil.copy(hf_hub_download(repo, src), target / dst)
    print(f"{repo}/{src} -> {target / dst}")
