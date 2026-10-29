from pathlib import Path

from chemfit.fitter_callbacks import SaveMetaData

from chemfit_dashboard.example import build_example_context


def main() -> None:
    context = build_example_context()
    SaveMetaData(Path(__file__).parent)(0, [context])


if __name__ == "__main__":
    main()
