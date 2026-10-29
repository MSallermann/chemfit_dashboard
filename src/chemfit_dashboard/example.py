"""ChemFit-backed example context used by documentation and tests."""

from __future__ import annotations

import math
from contextlib import suppress
from typing import TYPE_CHECKING

from chemfit import component, join, pipeline
from chemfit.fitter import FitContext
from chemfit.fitter_callbacks import SaveMetaData

if TYPE_CHECKING:
    from pathlib import Path

PARAMETER_BATCHES = (
    (
        {"epsilon": 0.16, "sigma": 3.2},
        {"epsilon": 0.20, "sigma": 3.4},
    ),
    (
        {"epsilon": 0.22, "sigma": 3.1},
        {"epsilon": 0.18, "sigma": 3.6},
    ),
    (
        {"epsilon": 0.19, "sigma": 3.3},
        {"epsilon": 0.21, "sigma": 3.5},
    ),
)


@component()
def validate_parameters(parameters: dict[str, float]) -> dict[str, float]:
    return dict(parameters)


@component()
def derive_state(parameters: dict[str, float]) -> dict[str, float]:
    state = dict(parameters)
    state.update(
        cutoff=2.5 * parameters["sigma"],
        density=0.75 / parameters["sigma"] ** 3,
        sample_distance=1.12 * parameters["sigma"],
        temperature=1.15 * parameters["epsilon"],
    )
    return state


@component()
def attach_targets(state: dict[str, float]) -> dict[str, float]:
    state = dict(state)
    state.update(
        target_energy=-0.17,
        target_pressure=0.025,
        target_rdf_peak=1.84,
        target_compressibility=0.58,
    )
    return state


@component()
def repulsive_energy(state: dict[str, float]) -> float:
    ratio = state["sigma"] / state["sample_distance"]
    return 4 * state["epsilon"] * ratio**12


@component()
def attractive_energy(state: dict[str, float]) -> float:
    ratio = state["sigma"] / state["sample_distance"]
    return -4 * state["epsilon"] * ratio**6


@component()
def cutoff_correction(state: dict[str, float]) -> float:
    return (
        -(8 / 3)
        * math.pi
        * state["density"]
        * state["epsilon"]
        * state["sigma"] ** 3
        / state["cutoff"]
    )


@component()
def assemble_pair_terms(terms: tuple[float, ...]) -> dict[str, float]:
    repulsive, attractive, correction = terms
    return {
        "repulsive_energy": repulsive,
        "attractive_energy": attractive,
        "cutoff_correction": correction,
        "energy": repulsive + attractive + correction,
    }


@component()
def virial_pressure(state: dict[str, float]) -> float:
    return state["density"] * state["temperature"] * (1 + 0.35 * state["epsilon"])


@component()
def compressibility(state: dict[str, float]) -> float:
    return 1 / (1 + state["density"] * state["sigma"] ** 3)


@component()
def heat_capacity(state: dict[str, float]) -> float:
    return 1.5 + 0.2 * state["temperature"] / state["epsilon"]


@component()
def assemble_thermodynamics(values: tuple[float, ...]) -> dict[str, float]:
    pressure, response, heat_capacity_value = values
    return {
        "pressure": pressure,
        "compressibility": response,
        "heat_capacity": heat_capacity_value,
    }


@component()
def radial_distribution_peak(state: dict[str, float]) -> float:
    return 1.0 + state["sigma"] / 4.1


@component()
def coordination_number(state: dict[str, float]) -> float:
    return 8.0 + 3.0 * state["density"] * state["sigma"] ** 3


@component()
def bond_order(state: dict[str, float]) -> float:
    return math.exp(-state["temperature"] / (5 * state["epsilon"]))


@component()
def assemble_structure(values: tuple[float, ...]) -> dict[str, float]:
    rdf_peak, coordination, order = values
    return {
        "rdf_peak": rdf_peak,
        "coordination_number": coordination,
        "bond_order": order,
    }


@component()
def retain_prepared_state(state: dict[str, float]) -> dict[str, float]:
    return dict(state)


@component()
def merge_observables(
    results: tuple[dict[str, float], ...],
) -> dict[str, float]:
    merged: dict[str, float] = {}
    for result in results:
        merged.update(result)
    return merged


@component()
def energy_error(observables: dict[str, float]) -> float:
    return (observables["energy"] - observables["target_energy"]) ** 2


@component()
def pressure_error(observables: dict[str, float]) -> float:
    return 4 * (observables["pressure"] - observables["target_pressure"]) ** 2


@component()
def structure_error(observables: dict[str, float]) -> float:
    return 0.5 * (observables["rdf_peak"] - observables["target_rdf_peak"]) ** 2


@component()
def failing_structure_error(observables: dict[str, float]) -> float:  # noqa: ARG001
    message = "intentional failure in the structure loss term"
    raise RuntimeError(message)


@component()
def response_error(observables: dict[str, float]) -> float:
    return (observables["compressibility"] - observables["target_compressibility"]) ** 2


@component()
def aggregate_loss(loss_terms: tuple[float | Exception, ...]) -> float:
    values: list[float] = []
    for term in loss_terms:
        if isinstance(term, Exception):
            raise term
        values.append(term)
    return sum(values)


def build_example_objective(*, fail: bool = False):
    preparation = (
        pipeline(validate_parameters, derive_state)
        .then(attach_targets)
        .with_meta(stage="parameter preparation", units="reduced Lennard-Jones")
    )
    pair_model = pipeline(
        join(repulsive_energy, attractive_energy, cutoff_correction).with_meta(
            model="pair potential terms"
        ),
        assemble_pair_terms,
    ).with_meta(stage="pair potential")
    thermodynamic_model = pipeline(
        join(virial_pressure, compressibility, heat_capacity).with_meta(
            model="thermodynamic observables"
        ),
        assemble_thermodynamics,
    ).with_meta(stage="thermodynamics")
    structural_model = pipeline(
        join(radial_distribution_peak, coordination_number, bond_order).with_meta(
            model="structural observables"
        ),
        assemble_structure,
    ).with_meta(stage="structure analysis")
    simulation = pipeline(
        join(
            pair_model,
            thermodynamic_model,
            structural_model,
            retain_prepared_state,
        ).with_meta(execution="parallel model branches"),
        merge_observables,
    ).with_meta(stage="simulation suite")
    scoring = pipeline(
        join(
            energy_error,
            pressure_error,
            failing_structure_error if fail else structure_error,
            response_error,
        ).with_meta(metric="weighted residual terms"),
        aggregate_loss,
    ).with_meta(stage="loss aggregation")
    return (
        pipeline(preparation, simulation)
        .then(scoring)
        .with_meta(
            example="nested Lennard-Jones calibration",
            dataset={"name": "synthetic argon", "state_points": 4},
            units={
                "energy": "eV",
                "pressure": "reduced",
                "distance": "angstrom",
            },
            example_state="failed evaluation" if fail else "complete evaluation",
        )
    )


def build_example_context(
    parameters: dict[str, float] | None = None,
) -> FitContext:
    context = FitContext()
    build_example_objective()(parameters or {"epsilon": 0.2, "sigma": 3.4}, context)
    return context


def build_failed_example_context(
    parameters: dict[str, float] | None = None,
) -> FitContext:
    """Return the context captured when an evaluation component raises."""
    context = FitContext()
    with suppress(RuntimeError):
        build_example_objective(fail=True)(
            parameters or {"epsilon": 0.2, "sigma": 3.4}, context
        )
    return context


def write_example_collection(output_directory: Path) -> None:
    for step, parameter_batch in enumerate(PARAMETER_BATCHES):
        contexts = [build_example_context(parameters) for parameters in parameter_batch]
        if step == len(PARAMETER_BATCHES) - 1:
            contexts[-1] = build_failed_example_context(parameter_batch[-1])
        SaveMetaData(output_directory)(step, contexts)
