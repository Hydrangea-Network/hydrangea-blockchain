import os

from setuptools import setup

dependencies = [
    "aiofiles==0.7.0",  # Async IO for files
    "blspy==1.0.15",  # Signature library
    "chiavdf==1.0.6",  # timelord and vdf verification
    "chiabip158==1.1",  # bip158-style wallet filters
    "chiapos==1.0.11",  # proof of space
    "clvm==0.9.7",
    "clvm_tools==0.4.5",  # Currying, Program.to, other conveniences
    "chia_rs==0.1.10",
    "clvm-tools-rs==0.1.19",  # Rust implementation of clvm_tools' compiler
    "aiohttp==3.8.1",  # HTTP server for full node rpc
    "aiosqlite==0.17.0",  # asyncio wrapper for sqlite, to store blocks
    "bitstring==3.1.9",  # Binary data management library
    "colorama==0.4.5",  # Colorizes terminal output
    "colorlog==6.6.0",  # Adds color to logs
    "concurrent-log-handler==0.9.19",  # Concurrently log and rotate logs
    "cryptography==36.0.2",  # Python cryptography library for TLS - keyring conflict
    "filelock==3.7.1",  # For reading and writing config multiprocess and multithread safely  (non-reentrant locks)
    "keyring==23.6.0",  # Store keys in MacOS Keychain, Windows Credential Locker
    "keyrings.cryptfile==1.3.4",  # Secure storage for keys on Linux (Will be replaced)
    #  "keyrings.cryptfile==1.3.8",  # Secure storage for keys on Linux (Will be replaced)
    #  See https://github.com/frispete/keyrings.cryptfile/issues/15
    "PyYAML==6.0",  # Used for config file format
    "setproctitle==1.2.3",  # Gives the hydrangea processes readable names
    "sortedcontainers==2.4.0",  # For maintaining sorted mempools
    # TODO: when moving to click 8 remove the pinning of black noted below
    "click==7.1.2",  # For the CLI
    "dnspython==2.2.0",  # Query DNS seeds
    "watchdog==2.1.9",  # Filesystem event watching - watches keyring.yaml
    "dnslib==0.9.17",  # dns lib
    "typing-extensions==4.3.0",  # typing backports like Protocol and TypedDict
    "zstd==1.5.2.6",
    "packaging==21.3",
    "psutil==5.9.1",
]

upnp_dependencies = [
    "miniupnpc==2.2.2",  # Allows users to open ports on their router
]

dev_dependencies = [
    "build",
    "coverage",
    "diff-cover",
    "pre-commit",
    "py3createtorrent",
    "pylint",
    "pytest",
    "pytest-asyncio>=0.18.1",  # require attribute 'fixture'
    "pytest-cov",
    "pytest-monitor; sys_platform == 'linux'",
    "pytest-xdist",
    "twine",
    "isort",
    "flake8",
    "mypy",
    # TODO: black 22.1.0 requires click>=8, remove this pin after updating to click 8
    "black==21.12b0",
    "aiohttp_cors",  # For blackd
    "ipython",  # For asyncio debugging
    "pyinstaller==5.3",
    "types-aiofiles",
    "types-click~=7.1",
    "types-cryptography",
    "types-pkg_resources",
    "types-pyyaml",
    "types-setuptools",
]

kwargs = dict(
    name="hydrangea-blockchain",
    author="Hydrangea Network",
    author_email="hello@hydrangea.website",
    description="Hydrangea blockchain full node, farmer, timelord, and wallet.",
    url="https://hydrangea.website/",
    license="Apache License",
    python_requires=">=3.7, <4",
    keywords="hydrangea blockchain node",
    install_requires=dependencies,
    extras_require=dict(
        uvloop=["uvloop"],
        dev=dev_dependencies,
        upnp=upnp_dependencies,
    ),
    packages=[
        "build_scripts",
        "hydrangea",
        "hydrangea.cmds",
        "hydrangea.clvm",
        "hydrangea.consensus",
        "hydrangea.daemon",
        "hydrangea.data_layer",
        "hydrangea.full_node",
        "hydrangea.timelord",
        "hydrangea.farmer",
        "hydrangea.harvester",
        "hydrangea.introducer",
        "hydrangea.plot_sync",
        "hydrangea.plotters",
        "hydrangea.plotting",
        "hydrangea.pools",
        "hydrangea.protocols",
        "hydrangea.rpc",
        "hydrangea.seeder",
        "hydrangea.server",
        "hydrangea.simulator",
        "hydrangea.types.blockchain_format",
        "hydrangea.types",
        "hydrangea.util",
        "hydrangea.wallet",
        "hydrangea.wallet.db_wallet",
        "hydrangea.wallet.puzzles",
        "hydrangea.wallet.cat_wallet",
        "hydrangea.wallet.did_wallet",
        "hydrangea.wallet.nft_wallet",
        "hydrangea.wallet.settings",
        "hydrangea.wallet.trading",
        "hydrangea.wallet.util",
        "hydrangea.ssl",
        "mozilla-ca",
    ],
    entry_points={
        "console_scripts": [
            "hydrangea = hydrangea.cmds.hydrangea:main",
            "hydrangea_daemon = hydrangea.daemon.server:main",
            "hydrangea_wallet = hydrangea.server.start_wallet:main",
            "hydrangea_full_node = hydrangea.server.start_full_node:main",
            "hydrangea_harvester = hydrangea.server.start_harvester:main",
            "hydrangea_farmer = hydrangea.server.start_farmer:main",
            "hydrangea_introducer = hydrangea.server.start_introducer:main",
            "hydrangea_crawler = hydrangea.seeder.start_crawler:main",
            "hydrangea_seeder = hydrangea.seeder.dns_server:main",
            "hydrangea_timelord = hydrangea.server.start_timelord:main",
            "hydrangea_timelord_launcher = hydrangea.timelord.timelord_launcher:main",
            "hydrangea_full_node_simulator = hydrangea.simulator.start_simulator:main",
            "hydrangea_data_layer = hydrangea.server.start_data_layer:main",
            "hydrangea_data_layer_http = hydrangea.data_layer.data_layer_server:main",
        ]
    },
    package_data={
        "hydrangea": ["pyinstaller.spec"],
        "": ["*.clvm", "*.clvm.hex", "*.clib", "*.clinc", "*.clsp", "py.typed"],
        "hydrangea.util": ["initial-*.yaml", "english.txt"],
        "hydrangea.ssl": ["hydrangea_ca.crt", "hydrangea_ca.key", "dst_root_ca.pem"],
        "mozilla-ca": ["cacert.pem"],
    },
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    zip_safe=False,
    project_urls={
        "Source": "https://github.com/Hydrangea-Network/hydrangea-blockchain/",
        "Changelog": "https://github.com/Hydrangea-Network/hydrangea-blockchain/blob/main/CHANGELOG.md",
    },
)


if len(os.environ.get("HYDRANGEA_SKIP_SETUP", "")) < 1:
    setup(**kwargs)  # type: ignore
