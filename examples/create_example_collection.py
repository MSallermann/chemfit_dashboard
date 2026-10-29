from pathlib import Path

from chemfit_dashboard.example import write_example_collection


def main() -> None:
    write_example_collection(Path(__file__).parent / "context_collection")


if __name__ == "__main__":
    main()
