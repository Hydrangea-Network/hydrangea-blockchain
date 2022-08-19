from __future__ import annotations

from chia.util.ints import uint32, uint64

# 1 Hydrangea coin = 1,000,000,000,000 = 1 trillion mojo.
_mojo_per_hydrangea = 1000000000000
_prefarm = 21000000
_blocks_per_year = 1681920  # 32 * 6 * 24 * 365
_block_reward = 125 # 125 Hydrangea are awarded per block
_farmer_fraction = (10 / _block_reward)
_pool_fraction = (90 / _block_reward)
_community_fraction = (20 / _block_reward)
_staking_fraction = (5 / _block_reward)
_timelord_fraction = ((1 / 1000) / _block_reward)

def _calculate_reward(fraction, height: uint32) -> uint64:
    """
    Returns the block reward for a given reward fraction at a certain block height.
    These halving events will not be hit at the exact times
    (3 years, etc), due to fluctuations in difficulty. They will likely come early, if the network space and VDF
    rates increase continuously.
    """
    if height == 0:
        return uint64(int(fraction * _prefarm * _mojo_per_hydrangea))
    elif height < 3 * _blocks_per_year:
        return uint64(int(fraction * _block_reward * _mojo_per_hydrangea))
    elif height < 6 * _blocks_per_year:
        return uint64(int(fraction * (_block_reward / 2) * _mojo_per_hydrangea))
    elif height < 9 * _blocks_per_year:
        return uint64(int(fraction * (_block_reward / 4) * _mojo_per_hydrangea))
    elif height < 12 * _blocks_per_year:
        return uint64(int(fraction * (_block_reward / 8) * _mojo_per_hydrangea))
    else:
        return uint64(int(fraction * (_block_reward / 16) * _mojo_per_hydrangea))


def calculate_pool_reward(height: uint32) -> uint64:
    """
    Returns the pool reward at a certain block height. If the farmer
    is solo farming, they act as the pool, and therefore earn the entire block reward.
    """
    return _calculate_reward(_pool_fraction, height)

def calculate_community_reward(height: uint32) -> uint64:
    """
    Returns the community reward at a certain block height.
    """
    return _calculate_reward(_community_fraction, height)

def calculate_staking_reward(height: uint32) -> uint64:
    """
    Returns the staking reward at a certain block height.
    """
    return _calculate_reward(_staking_fraction, height)

def calculate_base_farmer_reward(height: uint32) -> uint64:
    """
    Returns the base farmer reward at a certain block height.
    Returns the coinbase reward at a certain block height.
    """
    return _calculate_reward(_farmer_fraction, height)

def calculate_base_timelord_fee(height: uint32) -> uint64:
    """
    Returns the base timelord reward at a certain block height.
    """
    return _calculate_reward(_timelord_fraction, height)