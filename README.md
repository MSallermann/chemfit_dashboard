<p align="center">
  <img src="res/chemfit_logo_portable.svg" alt="ChemFit logo" width="180">
</p>

# ChemFit Dashboard

A desktop application for exploring and visualizing evaluation results from [ChemFit](https://github.com/MSallermann/chemfit).

ChemFit can produce detailed records of an optimization, including intermediate results, component metadata, and errors. This dashboard makes those results much easier to inspect without digging through JSON by hand.

Built with Python and PySide6, the dashboard runs locally and works directly with serialized evaluation contexts from ChemFit 4.0.0. It is entirely read-only, so your original results are never modified.

![ChemFit Dashboard screenshot](res/screenshot.png)

## Features

- **Explore evaluations:** Browse individual evaluation contexts or whole collections of results. Inspect nested components, inputs, outputs, metadata, and errors.
- **Visualize component graphs:** View the structure of an evaluation as an interactive graph, including dependencies and parent-child relationships.
- **Track results across evaluations:** Follow the same component through multiple evaluations, even when parts of the execution tree are skipped or fail.
- **Plot numerical data:** Plot any numerical value across a collection of evaluations, compare multiple quantities, normalize curves, and export plots as PNG, SVG, or CSV.
- **Search contexts:** Search components, metadata, and values using plain text or regular expressions, either within one evaluation or across a whole collection.
- **Investigate failures:** Get an overview of failed or incomplete evaluations and jump directly to the relevant components.

The dashboard also includes multiple color themes, adjustable font sizes, and keyboard navigation.

## Installation

Requires Python 3.10 or newer.

```bash
git clone https://github.com/MSallermann/chemfit_dashboard.git
cd chemfit_dashboard
pip install -e .