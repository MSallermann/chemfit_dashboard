# Releasing ChemFit Dashboard

ChemFit Dashboard is released as a Python package.

## Prepare the release

1. Update `version` in `pyproject.toml`.
2. Add the release date and user-visible changes to `CHANGELOG.md`.
3. Run the automated checks:

   ```bash
   ruff check src tests benchmark
   ruff format --check src tests benchmark
   pyright
   pytest
   python -m build
   python -m twine check --strict dist/*
   ```

4. Install the wheel in a clean virtual environment:

   ```bash
   python -m venv /tmp/chemfit-dashboard-release
   wheel=$(find dist -name '*.whl' -print -quit)
   /tmp/chemfit-dashboard-release/bin/python -m pip install "${wheel}[gui]"
   /tmp/chemfit-dashboard-release/bin/chemfit-dashboard --help
   /tmp/chemfit-dashboard-release/bin/chemfit-extract --help
   ```

## Manual smoke test

Launch the clean installation with `examples/context_collection/` and verify:

- Opening files, folders, and JSONL trajectories works.
- Drag and drop adds valid records and rejects invalid files clearly.
- Hierarchy, Data Flow, Search, Plot, Extraction, and Overview populate.
- Plot series can be added, removed, sorted, and exported.
- Extraction conflicts show a modal and replace the stale preview with an error.
- CSV and JSONL exports complete and contain the expected records.
- Saved extraction configurations load in both the GUI and `chemfit-extract`.
- Settings and the previous session restore after restarting the dashboard.

## Publish

Commit the version and changelog, then create the matching tag:

```bash
git tag -a v0.1.0 -m "ChemFit Dashboard 0.1.0"
git push origin main v0.1.0
```

Create a GitHub release from the tag using the matching changelog section. To
publish on PyPI, upload the validated distributions from `dist/`:

```bash
python -m twine upload dist/*
```
