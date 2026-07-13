# Repository guidance

## Scope

This repository contains ZED dataset tooling alongside large local recording data.

- Project files are under `scripts/`, `configs/`, `docs/`, and `tests/`.
- Treat recording directories and `*_exports/` as local data, not source code.
- Do not modify or delete SVO/SVO2 recordings unless the user explicitly requests it.
- Do not commit recordings, extracted images, depth maps, labels, or generated exports.
- Do not start a bulk extraction unless the user explicitly requests it and confirms the configuration.

## Implementation conventions

- Keep source session directories read-only; write generated files to sibling `<session>_exports/` directories.
- Preserve synchronization between left, right, and depth outputs through the shared frame ID.
- Preserve manifest-based reproducibility and resume behavior when changing extraction logic.
- Keep the extractor independent of OpenCV; use the ZED Python API, NumPy, and Pillow.
- Keep `AGENTS.md` concise and put user-facing details in `README.md` or `docs/`.

## Commands

Run unit tests:

```bash
python3 -m unittest discover -s tests -v
```

Check the CLI:

```bash
python3 scripts/svo_extract.py --help
```

Inspect recordings without extracting them:

```bash
python3 scripts/svo_extract.py inspect .
```

## Verification

After changing code:

1. Run the unit tests.
2. Run `python3 -m py_compile scripts/svo_extract.py tests/test_svo_extract.py`.
3. Run `git diff --check`.
4. Before committing, inspect `git diff --cached --name-only` and confirm that it contains no data files.

## Real SVO2 integration tests

- Real extraction requires the ZED SDK and NVIDIA GPU access.
- Prefer a single explicit frame and temporary output for smoke tests.
- Do not leave smoke-test exports in the dataset workspace.
- Do not run integration tests that download models or change the system environment without approval.

## Git operations

- The repository uses a deny-by-default `.gitignore`; retain that protection when adding project paths.
- Stage intended project files explicitly rather than using `git add -A`.
- Do not commit, push, rewrite history, or create a pull request unless the user asks for it.
