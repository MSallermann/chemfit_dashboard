from chemfit_dashboard.cli import build_parser


def test_each_cli_path_is_an_independent_location():
    arguments = build_parser().parse_args(
        ["context_collection", "runs/initial_params_01/meta_data"]
    )

    assert arguments.paths == [
        "context_collection",
        "runs/initial_params_01/meta_data",
    ]
