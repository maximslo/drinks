# Face recognition module — call recognize(path) per incoming photo.
#
# Setup (run once, or after updating identities.json):
#   python facial/database.py   → builds facial/database.npz (one centroid per person)
#
# Usage in watcher/API:
#   from facial.recognize import recognize
#   results = recognize("data/attachments/photo.heic")
#   # → [{"name": "joe", "confidence": 0.91, "bbox": [x1, y1, x2, y2]}, ...]
#   # confidence < THRESHOLD returns name = "unknown"
#
# Tune THRESHOLD (0.0–1.0): higher = stricter matching, fewer false positives
#
# TODO: wire into watcher — call recognize() on each new attachment, store
#       tagged names in drinks.db (e.g. a photo_faces table: photo_id, name, confidence)

from insightface.app import FaceAnalysis
from PIL import Image
import pillow_heif
import cv2
import numpy as np
import os

pillow_heif.register_heif_opener()

DATABASE_FILE = "facial/database.npz"
THRESHOLD = 0.55  # cosine similarity — below this is "unknown"

app = FaceAnalysis(name="buffalo_l")
app.prepare(ctx_id=0)

_db = np.load(DATABASE_FILE, allow_pickle=True)
_centroids = _db["centroids"]  # (N, embedding_dim)
_names = _db["names"]


def _read_image(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in (".heic", ".heics"):
            img = Image.open(path).convert("RGB")
            return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        return cv2.imread(path)
    except Exception:
        return None


def recognize(path):
    """
    Detect all faces in an image and return their identities.

    Returns a list of dicts, one per face:
      {"name": str, "confidence": float, "bbox": [x1, y1, x2, y2]}
    """
    img = _read_image(path)
    if img is None:
        return []

    results = []
    for face in app.get(img):
        emb = face.embedding / np.linalg.norm(face.embedding)
        sims = _centroids @ emb
        best_idx = int(np.argmax(sims))
        best_sim = float(sims[best_idx])
        results.append({
            "name": _names[best_idx] if best_sim >= THRESHOLD else "unknown",
            "confidence": best_sim,
            "bbox": face.bbox.tolist(),
        })

    return results


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python facial/recognize.py <image_path>")
        sys.exit(1)
    results = recognize(sys.argv[1])
    if not results:
        print("No faces detected")
    for r in results:
        print(f"  {r['name']}  (confidence: {r['confidence']:.3f})  bbox: {[round(x) for x in r['bbox']]}")
