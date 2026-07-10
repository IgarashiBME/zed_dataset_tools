# ZED Dataset Tools

Tools for reproducible extraction and preparation of datasets from Stereolabs ZED camera recordings.

The current implementation samples selected frames from SVO2 recordings and exports synchronized left images, right images, and depth maps. Large recordings and generated datasets remain local and are excluded from Git.

## Features

- Discover SVO2 recordings using the existing dataset directory structure
- Reproducible stratified-random, uniform-random, interval, explicit, or existing-frame sampling
- Export rectified left and right images as JPEG or PNG
- Export depth as 16-bit PNG or NumPy NPY
- Optional depth preview images
- Seek, scan, and hybrid playback strategies
- Manifest-based interruption and resume
- Configurable progress reporting
- Output validation for missing, corrupt, or mismatched files
- Separate source recordings from sibling `<session>_exports` directories

## Requirements

- Linux with an NVIDIA GPU supported by the ZED SDK
- Stereolabs ZED SDK and its Python API (`pyzed`)
- Python 3.10 or later
- Dependencies listed in `requirements-extractor.txt`

Install the Python dependencies with:

```bash
python3 -m pip install -r requirements-extractor.txt
```

The ZED Python API is installed with the ZED SDK and is therefore not included in the requirements file.

## Expected data layout

The default configuration discovers recordings with the following structure:

```text
dataset-root/
└── 20260611-12Ehime/
    └── 20260611_104824/
        ├── recording.svo2
        ├── meta.json
        └── imu.csv
```

Generated files are written beside the source session directory:

```text
20260611-12Ehime/
├── 20260611_104824/
│   └── recording.svo2
└── 20260611_104824_exports/
    └── 500images_v1/
        ├── left/
        ├── right/
        ├── depth/
        ├── depth_preview/
        ├── manifest.csv
        └── config.yaml
```

The source session directory is treated as read-only.

## Quick start

Inspect available SVO2 recordings:

```bash
python3 scripts/svo_extract.py inspect .
```

Review and edit `configs/extract.example.yaml`, then create extraction manifests:

```bash
python3 scripts/svo_extract.py plan \
  --config configs/extract.example.yaml
```

Run extraction:

```bash
python3 scripts/svo_extract.py extract \
  --config configs/extract.example.yaml
```

Resume an interrupted extraction:

```bash
python3 scripts/svo_extract.py extract \
  --config configs/extract.example.yaml \
  --resume
```

Validate one completed export:

```bash
python3 scripts/svo_extract.py verify \
  20260611-12Ehime/20260611_104824_exports/500images_v1
```

## Tests

The unit tests do not require a GPU or an SVO2 file:

```bash
python3 -m unittest discover -s tests -v
```

Real SVO2 extraction requires access to the NVIDIA GPU through the ZED SDK.

## Documentation

- [Extractor usage](docs/svo2_extractor_usage.md)
- [Extractor design](docs/svo2_extractor_design.md)

## Repository safety

This repository uses a deny-by-default `.gitignore`: all top-level paths are ignored unless explicitly allowed. Only project code, configurations, documentation, tests, and dependency metadata should be committed. Before every commit, verify the staged file list with:

```bash
git diff --cached --name-only
```
