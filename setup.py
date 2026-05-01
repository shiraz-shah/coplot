from __future__ import annotations

from setuptools import setup


setup(
    name="coplot",
    version="0.1.0",
    description="Local web workspace for LLM-assisted data science.",
    packages=["coplot"],
    package_data={"coplot": ["static/*.html", "static/*.css", "static/*.js"]},
    python_requires=">=3.9",
    entry_points={"console_scripts": ["coplot=coplot.server:main"]},
)
