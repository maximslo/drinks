from sklearn.cluster import DBSCAN
import numpy as np
import json

EPS = 0.40
MIN_SAMPLES = 5
IDENTITIES_FILE = "facial/identities.json"
OUTPUT = "facial/database.npz"

X = np.load("embeddings.npy")

with open(IDENTITIES_FILE) as f:
    known = {int(k): v for k, v in json.load(f).items()}

print("Clustering...")
labels = DBSCAN(eps=EPS, min_samples=MIN_SAMPLES, metric="cosine").fit_predict(X)

names, centroids = [], []

for cluster_id, name in known.items():
    mask = labels == cluster_id
    if not mask.any():
        print(f"  WARNING: cluster {cluster_id} ({name}) not found — skipping")
        continue
    centroid = np.mean(X[mask], axis=0)
    centroid /= np.linalg.norm(centroid)
    names.append(name)
    centroids.append(centroid)

np.savez(OUTPUT, centroids=np.array(centroids), names=np.array(names))
print(f"Saved {len(names)} identities to {OUTPUT}")
