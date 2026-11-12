# Tagging backends and output profiles

LMZ includes PixAI Tagger v1.0 (recommended) and WD ViT Tagger v3 (alternative).
Settings > General selects the model, device, local folder or model repository,
maximum tags per category, and category-specific thresholds and destinations.
New settings recommend PixAI; settings written before backend selection keep WD.

Install PixAI dependencies with `python -m pip install ".[pixai]"`. The official
PixAI Transformers pipeline executes the Python model code bundled with the model
repository or local folder. Compatible torchvision/PyTorch binaries are required.
WD continues to use ONNX Runtime. Neither adapter sends images to a service.
Selecting CUDA explicitly for PixAI fails if PyTorch cannot access CUDA; Auto can
select CPU. WD preserves its CPU fallback behavior and reports provider warnings.
Provider labels describe the active execution provider, not per-operation GPU use.

## Data flow

An adapter decodes its native output into `Prediction(name, category, score?)`.
An output profile maps source categories and labels into LMZ groups. Image and
sampled-video-frame inference use the same interface. A new architecture needs an
adapter; changing names or categories in a compatible architecture needs a profile.
The built-in registry describes supported models for the settings UI, so the UI
does not have a separate hardcoded model list.

Example app settings fragment (illustrative; label normalization is optional):

```yaml
tagging:
  backend: pixai
  model_repo: ''  # empty selects pixai-labs/pixai-tagger-v1.0
  model_path: ''  # optional local folder; takes precedence over the repository
  profiles:
    pixai:
      thresholds:
        style: 0.5
      category_map:
        style: null  # hide this output group
        copyright: copyright
      label_map:
        rating:
          rating:s: sensitive
```

Profiles are independent per backend. Destination category names are lowercase
letters/numbers with optional underscores or hyphens. An unspecified category
keeps its source name, including categories unknown to LMZ. The category table
edits thresholds and destinations; label maps are configured in app settings.
No cross-model label normalization is enabled by default.

## Persistence and compatibility

The existing `tag_media` entry point and cache path remain compatible with callers.
Version 2 caches add the backend, source predictions and grouped output while
retaining `rating`, `character_tags` and `tags` for existing consumers. Source
predictions retain their original names/categories when a profile renames or hides
them. Only candidates passing inference thresholds, limited per source category,
are stored. Lowering a threshold requires inference again. Video caches retain
source predictions per sampled frame.

Notes retain `wd_rating`, `wd_character_tags` and `wd_tags` as compatibility fields.
Additional groups use `tag_groups: {copyright: [...], style: [...]}`. They remain
separate in the existing metadata index and inspector; they support promotion,
removal and metadata maintenance. An explicitly empty note group suppresses the
cached group, preserving manual removals. Legacy caches and notes remain readable.
Existing generated notes are updated when retagged, not migrated in bulk.

Scores stay in the cache. Their display and interpretation remain separate
product decisions; scores from different models are not assumed comparable.

Inference and model switching share a lock. Switching releases the previous model
before loading another, and failed initialization is not cached. Normal process
shutdown also calls `unload_models`. Cached models are scoped by model, device,
local path and model-cache root.

## Adding an adapter

1. Implement the `TaggerBackend` protocol in `backend/tagging/contracts.py`.
2. Add a lazy constructor and descriptor to `backend/tagging/registry.py`.
3. Extend the backend setting discriminator and frontend backend type.
4. Verify native preprocessing/output decoding, cache isolation, failure recovery,
   category preservation and video-frame behavior with focused tests.

This registry supports built-in adapters. It does not execute user-installed
plugins or infer how arbitrary model formats should be loaded.
