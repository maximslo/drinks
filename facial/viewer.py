import numpy as np
import os
import base64
import io
import json
from PIL import Image
import pillow_heif
from sklearn.cluster import DBSCAN

pillow_heif.register_heif_opener()

EPS = 0.40
MIN_SAMPLES = 5
THUMB_SIZE = (120, 120)
MAX_PER_CLUSTER = 20
OUTPUT = "facial/clusters.html"
IDENTITIES_FILE = "facial/identities.json"

X = np.load("embeddings.npy")
image_paths = np.load("image_paths.npy")

labels = DBSCAN(eps=EPS, min_samples=MIN_SAMPLES, metric="cosine").fit_predict(X)

known = {}
if os.path.exists(IDENTITIES_FILE):
    with open(IDENTITIES_FILE) as f:
        known = {int(k): v for k, v in json.load(f).items()}

# group paths by cluster, sorted by size descending
clusters = {}
for path, label in zip(image_paths, labels):
    clusters.setdefault(label, set()).add(path)
clusters = {k: list(v) for k, v in clusters.items()}
clusters = dict(sorted(clusters.items(), key=lambda x: len(x[1]), reverse=True))


def to_thumbnail_b64(path):
    try:
        img = Image.open(path).convert("RGB")
        img.thumbnail(THUMB_SIZE)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


html_parts = ["""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Face Clusters</title>
<style>
  body { font-family: sans-serif; background: #111; color: #eee; padding: 20px; }
  h2 { margin: 24px 0 8px; font-size: 15px; }
  h2 .id { color: #555; font-size: 12px; margin-left: 8px; }
  h2.named { color: #7cf; }
  h2.unnamed { color: #fa8; }
  h2.unknown { color: #555; }
  .cluster { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 16px; border-bottom: 1px solid #222; padding-bottom: 16px; }
  img { width: 120px; height: 120px; object-fit: cover; border-radius: 4px; }
  .more { display: flex; align-items: center; color: #555; font-size: 13px; padding: 0 8px; }
</style>
</head>
<body>
"""]

for label, paths in clusters.items():
    if label == -1:
        continue  # unknowns at the bottom
    name = known.get(label)
    label_str = name.upper() if name else f"? (identity {label})"
    css = "named" if name else "unnamed"
    html_parts.append(f'<h2 class="{css}">{label_str}<span class="id">cluster {label} &mdash; {len(paths)} photos</span></h2>\n<div class="cluster">\n')
    for path in paths[:MAX_PER_CLUSTER]:
        b64 = to_thumbnail_b64(path)
        if b64:
            html_parts.append(f'  <img src="data:image/jpeg;base64,{b64}" title="{os.path.basename(path)}">\n')
    if len(paths) > MAX_PER_CLUSTER:
        html_parts.append(f'  <div class="more">+{len(paths)-MAX_PER_CLUSTER} more</div>\n')
    html_parts.append("</div>\n")

# unknowns last
unknown_paths = clusters.get(-1, [])
if unknown_paths:
    html_parts.append(f'<h2 class="unknown">Unknown / noise &mdash; {len(unknown_paths)} photos</h2>\n<div class="cluster">\n')
    for path in unknown_paths[:MAX_PER_CLUSTER]:
        b64 = to_thumbnail_b64(path)
        if b64:
            html_parts.append(f'  <img src="data:image/jpeg;base64,{b64}" title="{os.path.basename(path)}">\n')
    if len(unknown_paths) > MAX_PER_CLUSTER:
        html_parts.append(f'  <div class="more">+{len(unknown_paths)-MAX_PER_CLUSTER} more</div>\n')
    html_parts.append("</div>\n")

html_parts.append("</body></html>")

with open(OUTPUT, "w") as f:
    f.writelines(html_parts)

named = sum(1 for k in clusters if k != -1 and k in known)
unnamed = sum(1 for k in clusters if k != -1 and k not in known)
print(f"Wrote {OUTPUT}")
print(f"  {named} named, {unnamed} unnamed clusters — open the HTML to identify them, then add to identities.json")
print(f"  {len(unknown_paths)} faces still unknown")
