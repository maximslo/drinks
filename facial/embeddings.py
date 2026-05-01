from insightface.app import FaceAnalysis
from sklearn.cluster import DBSCAN
from PIL import Image
import pillow_heif
import cv2
import numpy as np
import os

pillow_heif.register_heif_opener()

PHOTOS_DIR = "data/attachments"
SKIP_EXTS = {".gif"}
EPS = 0.40        # cosine distance threshold — tune between 0.30 (strict) and 0.45 (loose)
MIN_SAMPLES = 5

app = FaceAnalysis(name="buffalo_l")
app.prepare(ctx_id=0)


def read_image(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in SKIP_EXTS:
        return None
    try:
        if ext in (".heic", ".heics"):
            img = Image.open(path).convert("RGB")
            return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        return cv2.imread(path)
    except Exception:
        return None


def get_all_embeddings(path):
    """Return one normalized embedding per face detected in the image."""
    img = read_image(path)
    if img is None:
        return []
    faces = app.get(img)
    result = []
    for face in faces:
        emb = face.embedding
        result.append(emb / np.linalg.norm(emb))
    return result


# Step 1: extract embeddings — one entry per face, so group photos contribute multiple
embeddings, image_paths = [], []
files = os.listdir(PHOTOS_DIR)

for i, file in enumerate(files, 1):
    path = os.path.join(PHOTOS_DIR, file)
    for emb in get_all_embeddings(path):
        embeddings.append(emb)
        image_paths.append(path)
    if i % 100 == 0:
        print(f"  {i}/{len(files)} files processed, {len(embeddings)} faces found so far")

print(f"Extracted {len(embeddings)} face embeddings from {len(files)} files")

X = np.array(embeddings)
np.save("embeddings.npy", X)
np.save("image_paths.npy", np.array(image_paths))

# Step 2: cluster
print("Clustering...")
labels = DBSCAN(eps=EPS, min_samples=MIN_SAMPLES, metric="cosine").fit_predict(X)

# Step 3: build identity database
database = {}
for cluster_id in set(labels):
    if cluster_id == -1:
        continue
    mask = labels == cluster_id
    cluster_embeddings = X[mask]
    # deduplicate paths — same photo can appear multiple times if it has multiple faces
    seen = set()
    unique_paths = []
    for p, m in zip(image_paths, mask):
        if m and p not in seen:
            seen.add(p)
            unique_paths.append(p)
    database[cluster_id] = {
        "centroid": np.mean(cluster_embeddings, axis=0),
        "embeddings": cluster_embeddings,
        "paths": unique_paths,
    }

print(f"Found {len(database)} identities, {np.sum(labels == -1)} unknowns out of {len(X)} faces")
