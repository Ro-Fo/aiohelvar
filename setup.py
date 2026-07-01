import setuptools

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setuptools.setup(
    name="aiohelvar",
    version="0.10.0",
    author="Tom Playford",
    description="Async HelvarNet communication and control library.",
    long_description=long_description,
    license="Apache 2.0",
    long_description_content_type="text/markdown",
    url="https://github.com/tomplayford/aiohelvar",
    download_url="https://github.com/tomplayford/aiohelvar/archive/refs/tags/V0.10.0.tar.gz",
    project_urls={
        "Bug Tracker": "https://github.com/tomplayford/aiohelvar/issues",
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent",
    ],
    packages=["aiohelvar", "aiohelvar.parser"],
    python_requires=">=3.8",
    install_requires=[],
    entry_points={
        "console_scripts": [
            # Read-only diagnostics and a configurable mock router. These also
            # work via `python -m aiohelvar diagnose` / `... mock`.
            "aiohelvar-diagnose=aiohelvar.__main__:main",
            "aiohelvar-mock=aiohelvar.__main__:main",
        ],
    },
)
