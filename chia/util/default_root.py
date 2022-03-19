import os
from pathlib import Path

DEFAULT_ROOT_PATH = Path(os.path.expanduser(os.getenv("HYDRANGEA_ROOT", "~/.hydrangea/mainnet"))).resolve()
STANDALONE_ROOT_PATH = Path(
    os.path.expanduser(os.getenv("HYDRANGEA_STANDALONE_WALLET_ROOT", "~/.hydrangea/standalone_wallet"))
).resolve()

DEFAULT_KEYS_ROOT_PATH = Path(os.path.expanduser(os.getenv("HYDRANGEA_KEYS_ROOT", "~/.hydrangea_keys"))).resolve()
