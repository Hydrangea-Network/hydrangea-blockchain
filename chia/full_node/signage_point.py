from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from chia.types.blockchain_format.vdf import VDFInfo, VDFProof
from chia.util.streamable import Streamable, streamable
from chia.types.blockchain_format.sized_bytes import bytes32


@streamable
@dataclass(frozen=True)
class SignagePoint(Streamable):
    cc_vdf: Optional[VDFInfo]
    cc_proof: Optional[VDFProof]
    rc_vdf: Optional[VDFInfo]
    rc_proof: Optional[VDFProof]
    timelord_puzzle_hash: Optional[bytes32]
