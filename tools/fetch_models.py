"""Download the pinned models."""
import sys
import urllib.request
from pathlib import Path

from peekaboo.vision import MODELS, verify_models

target = Path(sys.argv[1])
target.mkdir(exist_ok=True)
for name, (_, url) in MODELS.items():
    if not (target / name).exists():
        print("fetching", name)
        urllib.request.urlretrieve(url, target / name)
verify_models(target)
print("models verified")
