from pathlib import Path

from setuptools import find_packages, setup

HERE = Path(__file__).parent


def get_version():
    version_vars = {}
    exec((HERE / "googlenewsdecoder" / "__version__.py").read_text(encoding="utf-8"), version_vars)
    return version_vars["__version__"]


setup(
    name="googlenewsdecoder",
    version=get_version(),
    description="A Python package to decode Google News URLs to their original sources.",
    # Relative to this file, not the working directory: `pip install /path/to/checkout` from
    # anywhere else was reading README.md out of the caller's cwd, or failing to find it.
    long_description=(HERE / "README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    url="https://github.com/SSujitX/google-news-url-decoder",
    author="Sujit Biswas",
    author_email="ssujitxx@gmail.com",
    license="MIT",
    # Explicit, so that adding a top-level directory with an __init__.py in it never quietly
    # becomes part of what users install. `probes/` in particular is a live-network lab.
    packages=find_packages(include=["googlenewsdecoder", "googlenewsdecoder.*"]),
    # Two dependencies, each earning its place against a defect found by adversarial review:
    #   urllib3    -- BOUNDED decompression, which is the one thing httpx cannot do and the
    #                 reason it is the default: `read(amt, decode_content=True)` caps decoded
    #                 output. Also no cookie jar at all, which is what gets past Google's
    #                 consent wall, plus cross-origin credential stripping and SOCKS via the
    #                 extra. `requests` used to be here and is no longer a dependency.
    #   selectolax -- a real HTML parser. A regex agreed with it on 12 live pages and is still
    #                 defeated by an HTML comment, which is a worse failure than the one it fixed.
    install_requires=["urllib3>=2.7.0", "selectolax>=0.3.27"],
    extras_require={
        "socks": ["pysocks>=1.7.1"],   # socks5:// proxies, via urllib3.contrib.socks
        "async": ["httpx>=0.28.1"],    # GoogleDecoderAsync / decode_async
        "all": ["pysocks>=1.7.1", "httpx>=0.28.1"],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    zip_safe=False,
    keywords="google news decoder",
    project_urls={
        "Bug Tracker": "https://github.com/SSujitX/google-news-url-decoder/issues",
        "Documentation": "https://github.com/SSujitX/google-news-url-decoder#readme",
        "Source Code": "https://github.com/SSujitX/google-news-url-decoder",
    },
    # Raised from 3.9, which reached end of security support on 2025-10-31. Supporting a dead
    # version is not free: it forbids `X | None` annotations everywhere and forces the linter
    # to be configured around a floor nobody can still receive security fixes on.
    python_requires=">=3.10",
)
