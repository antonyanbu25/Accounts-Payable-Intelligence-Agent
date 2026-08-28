# Why this directory is committed to git

This is a small (≈1MB) pre-built Chroma index over `tax_docs/`, built locally
by `python ingest.py`. It is committed deliberately — this is the one
exception to "generated files don't belong in git" in this repo.

**Reason:** `ingest.py` needs `sentence-transformers`/`torch` (to embed the
documents) and `anthropic` (to extract structured metadata from each doc).
Installing *and loading* those on every Render deploy — just to rebuild an
index that hadn't changed — is what caused an out-of-memory crash on
Render's free (512Mi) tier. The deployed service (`main.py`) never imports
`torch`, `sentence_transformers`, or `anthropic` at all; it only reads this
index at query time via `chromadb`, which is lightweight. Committing the
built index removes the need to ever run the heavy ingestion step on Render.

**Regenerating it:** whenever `tax_docs/` changes,

```bash
pip install -r ../requirements-ingest.txt
python ingest.py
```

then re-commit this directory.

**Known residual risk:** this index was built on macOS (Apple Silicon,
arm64) and is read on Render's Linux x86_64 containers. Chroma's on-disk
format (SQLite metadata + hnswlib binary index files) is generally portable
across platforms, but this hasn't been independently verified beyond "the
deployed service starts and `/tax-lookup` returns correct results" — which
is the actual verification step taken here, not just an architecture
assumption. If it were ever to fail to load on a given platform, the fallback
is to run `ingest.py` directly on that platform once (e.g. in a one-off
Render shell) and commit the result from there instead.
