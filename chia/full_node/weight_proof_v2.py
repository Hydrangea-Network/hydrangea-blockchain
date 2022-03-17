import asyncio
import dataclasses
import logging
import math
import random
import time
from concurrent.futures import as_completed
from concurrent.futures.process import ProcessPoolExecutor
from typing import Dict, List, Optional, Tuple, Union
from chia.consensus.block_record import BlockRecord
from chia.consensus.blockchain_interface import BlockchainInterface
from chia.consensus.constants import ConsensusConstants
from chia.consensus.pot_iterations import calculate_iterations_quality, calculate_sp_iters, is_overflow_block
from chia.consensus.vdf_info_computation import get_signage_point_vdf_info
from chia.full_node.weight_proof_common import (
    _validate_recent_blocks,
    _validate_summaries_weight,
    blue_boxed_end_of_slot,
    bytes_to_vars,
    _sample_sub_epoch,
    get_prev_two_slots_height,
)
from chia.types.blockchain_format.classgroup import ClassgroupElement, CompressedClassgroupElement
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.blockchain_format.slots import ChallengeChainSubSlot, RewardChainSubSlot, ChallengeBlockInfo
from chia.types.blockchain_format.sub_epoch_summary import SubEpochSummary
from chia.types.blockchain_format.vdf import (
    VDFInfo,
    compress_output,
    verify_compressed_vdf,
)
from chia.types.end_of_slot_bundle import EndOfSubSlotBundle
from chia.types.header_block import HeaderBlock
from chia.types.weight_proof import (
    RecentChainData,
    SubEpochChallengeSegmentV2,
    SubEpochData,
    SubEpochSegmentsV2,
    SubSlotDataV2,
    WeightProofV2,
)
from chia.util.block_cache import BlockCache
from chia.util.hash import std_hash
from chia.util.ints import uint8, uint32, uint64, uint128
from chia.util.streamable import recurse_jsonify

log = logging.getLogger(__name__)


class WeightProofHandlerV2:

    LAMBDA_L = 100
    C = 0.5
    MAX_SAMPLES = 140

    def __init__(
        self,
        constants: ConsensusConstants,
        blockchain: BlockchainInterface,
    ):
        self.tip: Optional[bytes32] = None
        self.proof: Optional[WeightProofV2] = None
        self.constants = constants
        self.blockchain = blockchain
        self.lock = asyncio.Lock()

    async def get_proof_of_weight(self, tip: bytes32, seed: bytes32) -> Optional[WeightProofV2]:

        tip_rec = self.blockchain.try_block_record(tip)
        if tip_rec is None:
            log.error("unknown tip")
            return None

        if tip_rec.height < self.constants.WEIGHT_PROOF_RECENT_BLOCKS:
            log.debug("need at least 3 sub epochs for weight proof")
            return None

        async with self.lock:
            wp = await self._create_proof_of_weight(tip, seed)
            if wp is None:
                return None
            self.proof = wp
            self.tip = tip
            return wp

    def get_fork_point_no_validations(self, weight_proof: WeightProofV2) -> Tuple[bool, uint32]:
        log.debug("get fork point skip validations")
        if len(weight_proof.sub_epochs) == 0:
            return False, uint32(0)
        summaries, sub_epoch_weight_list = _validate_sub_epoch_summaries(self.constants, weight_proof)
        if summaries is None:
            log.warning("weight proof failed to validate sub epoch summaries")
            return False, uint32(0)
        return True, self.get_fork_point(summaries)

    async def validate_weight_proof(
        self, weight_proof: WeightProofV2, seed: bytes32
    ) -> Tuple[bool, uint32, List[SubEpochSummary]]:
        if len(weight_proof.sub_epochs) == 0:
            return False, uint32(0), []

        peak_height = weight_proof.recent_chain_data[-1].reward_chain_block.height
        log.info(f"validate weight proof peak height {peak_height}")

        summaries, sub_epoch_weight_list = _validate_sub_epoch_summaries(self.constants, weight_proof)
        if summaries is None or sub_epoch_weight_list is None:
            log.error("weight proof failed sub epoch data validation")
            return False, uint32(0), []
        rng = random.Random(seed)
        if not validate_sub_epoch_sampling(rng, sub_epoch_weight_list, weight_proof):
            log.error("failed weight proof sub epoch sample validation")
            return False, uint32(0), []

        constants_bytes, summary_bytes, wp_segment_bytes, wp_recent_chain_bytes = vars_to_bytes(
            self.constants, summaries, weight_proof
        )

        with ProcessPoolExecutor() as executor:
            recent_blocks_validation_task = asyncio.get_running_loop().run_in_executor(
                executor, _validate_recent_blocks, constants_bytes, wp_recent_chain_bytes, summary_bytes
            )
            if not _validate_sub_epoch_segments(
                constants_bytes, rng, weight_proof.sub_epoch_segments, summary_bytes, executor
            ):
                log.error("failed validating weight proof sub epoch segments")
                return False, uint32(0), []

        if not await recent_blocks_validation_task:
            log.error("failed validating weight proof recent blocks")
            return False, uint32(0), []

        return True, self.get_fork_point(summaries), summaries

    def get_fork_point(self, received_summaries: List[SubEpochSummary]) -> uint32:
        # iterate through sub epoch summaries to find fork point
        fork_point_index = 0
        ses_heights = self.blockchain.get_ses_heights()
        for idx, summary_height in enumerate(ses_heights):
            log.debug(f"check summary {idx} height {summary_height}")
            local_ses = self.blockchain.get_ses(summary_height)
            if local_ses is None or local_ses.get_hash() != received_summaries[idx].get_hash():
                break
            fork_point_index = idx

        if fork_point_index > 2:
            # Two summeries can have different blocks and still be identical
            # This gets resolved after one full sub epoch
            height = ses_heights[fork_point_index - 2]
        else:
            height = uint32(0)

        return height

    async def get_sub_epoch_data(self, tip_height: uint32, summary_heights: List[uint32]) -> List[SubEpochData]:
        sub_epoch_data: List[SubEpochData] = []
        for sub_epoch_n, ses_height in enumerate(summary_heights):
            if ses_height > tip_height:
                break
            ses = self.blockchain.get_ses(ses_height)
            log.debug(f"handle sub epoch summary {sub_epoch_n} at height: {ses_height}  ")
            sub_epoch_data.append(_create_sub_epoch_data(ses))
        return sub_epoch_data

    async def _create_proof_of_weight(self, tip: bytes32, seed: bytes32) -> Optional[WeightProofV2]:
        """
        Creates a weight proof object
        """
        start = time.time()
        sub_epoch_segments: List[SubEpochChallengeSegmentV2] = []
        tip_rec = self.blockchain.try_block_record(tip)
        if tip_rec is None:
            log.error("failed not tip in cache")
            return None
        log.info(f"create weight proof peak {tip} {tip_rec.height}")
        recent_chain_task = self._get_recent_chain(tip_rec.height)
        summary_heights = self.blockchain.get_ses_heights()
        sub_epoch_data = await self.get_sub_epoch_data(tip_rec.height, summary_heights)
        rng = random.Random(seed)
        last_ses_block, prev_prev_ses_block = await self.get_last_l_weight(summary_heights, tip_rec.height)
        if last_ses_block is None or prev_prev_ses_block is None:
            log.error("failed getting chain last L")
            return None
        last_l_weight = last_ses_block.weight - prev_prev_ses_block.weight
        log.debug(f"total weight {last_ses_block.weight} prev weight {prev_prev_ses_block.weight}")
        weight_to_check = _get_weights_for_sampling(rng, last_ses_block.weight, last_l_weight)
        if weight_to_check is None:
            log.error("failed getting weight list for samples")
            return None

        ses_blocks = await self.blockchain.get_block_records_at(summary_heights)
        if ses_blocks is None:
            log.error("failed pulling ses blocks from database")
            return None

        # set prev_ses to genesis
        prev_ses_block = await self.blockchain.get_block_record_from_db(self.blockchain.height_to_hash(uint32(0)))
        if prev_ses_block is None:
            return None

        sample_n = 0
        for sub_epoch_n, ses_height in enumerate(summary_heights):
            if ses_height > tip_rec.height:
                break

            # if we have enough sub_epoch samples, dont sample
            if sample_n >= self.MAX_SAMPLES:
                log.info(f"reached sampled sub epoch cap {sample_n}")
                break
            # sample sub epoch
            # next sub block
            ses_block = ses_blocks[sub_epoch_n]
            if ses_block is None or ses_block.sub_epoch_summary_included is None:
                log.error("error while building proof")
                return None

            if _sample_sub_epoch(prev_ses_block.weight, ses_block.weight, weight_to_check):
                sample_n += 1
                segments = await self.blockchain.get_sub_epoch_challenge_segments_v2(ses_block.header_hash)
                if segments is None:
                    segments = await self.__create_sub_epoch_segments(ses_block, prev_ses_block, uint32(sub_epoch_n))
                    if segments is None:
                        log.error(
                            f"failed while building segments for sub epoch {sub_epoch_n}, ses height {ses_height} "
                        )
                        return None
                    await self.blockchain.persist_sub_epoch_challenge_segments_v2(ses_block.header_hash, segments)
                # remove proofs from unsampled
                sampled_seg_index = rng.choice(range(len(segments)))
                segments = compress_segments(sampled_seg_index, segments)
                log.debug(f"sub epoch {sub_epoch_n} has {len(segments)} segments sampled {sampled_seg_index}")
                sub_epoch_segments.extend(segments)
            prev_ses_block = ses_block
        recent_chain = await recent_chain_task
        if recent_chain is None:
            return None
        log.info(f"time to create proof: {time.time() - start}")
        return WeightProofV2(sub_epoch_data, sub_epoch_segments, recent_chain)

    async def get_last_l_weight(self, summary_heights, peak):
        summaries_n = len(summary_heights)
        for idx, height in enumerate(reversed(summary_heights)):
            if height <= peak:
                if summaries_n - idx < 3:
                    log.warning("chain to short not enough sub epochs ")
                    return None, None
                last_ses_block = await self.blockchain.get_block_record_from_db(
                    self.blockchain.height_to_hash(uint32(summary_heights[summaries_n - idx - 1]))
                )
                prev_prev_ses_block = await self.blockchain.get_block_record_from_db(
                    self.blockchain.height_to_hash(uint32(summary_heights[summaries_n - idx - 3]))
                )
                return last_ses_block, prev_prev_ses_block
        return None, None

    async def _get_recent_chain(self, tip_height: uint32) -> Optional[List[HeaderBlock]]:
        recent_chain: List[HeaderBlock] = []
        ses_heights = self.blockchain.get_ses_heights()
        min_height = 0
        count_ses = 0
        for ses_height in reversed(ses_heights):
            if ses_height <= tip_height:
                count_ses += 1
            if count_ses == 2:
                min_height = ses_height - 1
                break
        log.debug(f"start {min_height} end {tip_height}")
        headers = await self.blockchain.get_header_blocks_in_range(min_height, tip_height, tx_filter=False)
        blocks = await self.blockchain.get_block_records_in_range(min_height, tip_height)
        ses_count = 0
        curr_height = tip_height
        blocks_n = 0
        while ses_count < 2:
            if curr_height == 0:
                break
            # add to needed reward chain recent blocks
            header_block = headers[self.blockchain.height_to_hash(curr_height)]
            block_rec = blocks[header_block.header_hash]
            if header_block is None:
                log.error("creating recent chain failed")
                return None
            recent_chain.insert(0, header_block)
            if block_rec.sub_epoch_summary_included:
                ses_count += 1
            curr_height = uint32(curr_height - 1)
            blocks_n += 1

        header_block = headers[self.blockchain.height_to_hash(curr_height)]
        recent_chain.insert(0, header_block)

        log.info(
            f"recent chain, "
            f"start: {recent_chain[0].reward_chain_block.height} "
            f"end:  {recent_chain[-1].reward_chain_block.height} "
        )
        return recent_chain

    async def create_sub_epoch_segments(self):
        log.debug("check segments in db")
        """
        Creates a weight proof object
         """
        peak_height = self.blockchain.get_peak_height()
        if peak_height is None:
            log.error("no peak yet")
            return None

        summary_heights = self.blockchain.get_ses_heights()
        prev_ses_block = await self.blockchain.get_block_record_from_db(self.blockchain.height_to_hash(uint32(0)))
        if prev_ses_block is None:
            return None

        ses_blocks = await self.blockchain.get_block_records_at(summary_heights)
        if ses_blocks is None:
            return None

        for sub_epoch_n, ses_height in enumerate(summary_heights):
            log.debug(f"check db for sub epoch {sub_epoch_n}")
            if ses_height > peak_height:
                break
            ses_block = ses_blocks[sub_epoch_n]
            if ses_block is None or ses_block.sub_epoch_summary_included is None:
                log.error("error while building proof")
                return None
            await self.__create_persist_segment(prev_ses_block, ses_block, ses_height, sub_epoch_n)
            prev_ses_block = ses_block
            await asyncio.sleep(2)
        log.debug("done checking segments")
        return None

    async def __create_persist_segment(self, prev_ses_block, ses_block, ses_height, sub_epoch_n):
        segments = await self.blockchain.get_sub_epoch_challenge_segments_v2(ses_block.header_hash)
        if segments is None:
            segments = await self.__create_sub_epoch_segments(ses_block, prev_ses_block, uint32(sub_epoch_n))
            if segments is None:
                log.error(f"failed while building segments for sub epoch {sub_epoch_n}, ses height {ses_height} ")
                return None
            await self.blockchain.persist_sub_epoch_challenge_segments_v2(ses_block.header_hash, segments)

    async def create_prev_sub_epoch_segments(self):
        log.debug("create prev sub_epoch_segments")
        heights = self.blockchain.get_ses_heights()
        if len(heights) < 3:
            return
        count = len(heights) - 2
        ses_sub_block = self.blockchain.height_to_block_record(heights[-2])
        prev_ses_sub_block = self.blockchain.height_to_block_record(heights[-3])
        assert prev_ses_sub_block.sub_epoch_summary_included is not None
        segments = await self.__create_sub_epoch_segments(ses_sub_block, prev_ses_sub_block, uint32(count))
        assert segments is not None
        await self.blockchain.persist_sub_epoch_challenge_segments_v2(ses_sub_block.header_hash, segments)
        log.debug("sub_epoch_segments done")
        return

    async def __create_sub_epoch_segments(
        self, ses_block: BlockRecord, se_start: BlockRecord, sub_epoch_n: uint32
    ) -> Optional[List[SubEpochChallengeSegmentV2]]:
        segments: List[SubEpochChallengeSegmentV2] = []
        start_height = await get_prev_two_slots_height(self.blockchain, se_start)
        blocks = await self.blockchain.get_block_records_in_range(
            start_height, ses_block.height + self.constants.MAX_SUB_SLOT_BLOCKS
        )
        header_blocks = await self.blockchain.get_header_blocks_in_range(
            start_height, ses_block.height + self.constants.MAX_SUB_SLOT_BLOCKS, tx_filter=False
        )
        curr: Optional[HeaderBlock] = header_blocks[se_start.header_hash]
        assert curr is not None
        height = se_start.height
        first = True
        idx = 0
        with ProcessPoolExecutor() as executor:
            while curr.height < ses_block.height:
                if blocks[curr.header_hash].is_challenge_block(self.constants):
                    log.debug(f"challenge segment {idx}, starts at {curr.height} ")
                    seg, height = self._create_challenge_segment(
                        curr, sub_epoch_n, header_blocks, blocks, first, executor
                    )
                    if seg is None:
                        log.error(f"failed creating segment {curr.header_hash} ")
                        return None
                    segments.append(seg)
                    idx += 1
                    first = False
                else:
                    height = height + uint32(1)  # type: ignore
                curr = header_blocks[self.blockchain.height_to_hash(height)]
                if curr is None:
                    return None
        log.debug(f"next sub epoch starts at {height}")
        return segments

    def _create_challenge_segment(
        self,
        header_block: HeaderBlock,
        sub_epoch_n: uint32,
        header_blocks: Dict[bytes32, HeaderBlock],
        blocks: Dict[bytes32, BlockRecord],
        first_segment_in_sub_epoch: bool,
        executor: ProcessPoolExecutor,
    ) -> Tuple[Optional[SubEpochChallengeSegmentV2], uint32]:
        sub_slots: List[SubSlotDataV2] = []
        log.debug(f"create challenge segment block {header_block.header_hash} block height {header_block.height} ")
        # VDFs from sub slots before challenge block
        first_sub_slots, end_of_sub_slot_bundle = self.__first_sub_slot_vdfs(
            header_block, header_blocks, blocks, first_segment_in_sub_epoch, executor
        )
        if first_sub_slots is None:
            log.error("failed building first sub slots")
            return None, uint32(0)

        sub_slots.extend(first_sub_slots)
        sub_slots.append(handle_block_vdfs(executor, self.constants, header_block, blocks))

        # # VDFs from slot after challenge block to end of slot
        log.debug(f"create slot end vdf for block {header_block.header_hash} height {header_block.height}")
        challenge_slot_end_sub_slots, end_height = self.__slot_end_vdf(
            uint32(header_block.height + 1), header_blocks, blocks, executor
        )

        if challenge_slot_end_sub_slots is None:
            log.error("failed building slot end ")
            return None, uint32(0)
        sub_slots.extend(challenge_slot_end_sub_slots)
        if first_segment_in_sub_epoch and sub_epoch_n != 0:
            assert end_of_sub_slot_bundle
            assert end_of_sub_slot_bundle.infused_challenge_chain
            first_rc_end_of_slot_vdf = end_of_sub_slot_bundle.reward_chain.end_of_slot_vdf
            end_of_slot_info = end_of_sub_slot_bundle.challenge_chain.challenge_chain_end_of_slot_vdf
            end_of_slot_icc_challenge = (
                end_of_sub_slot_bundle.infused_challenge_chain.infused_challenge_chain_end_of_slot_vdf.challenge
            )

            prev_icc_ip_iters = None
            slots_to_check = 1
            if not header_block.first_in_sub_slot:
                slots_to_check = 2
            curr = blocks[header_block.prev_header_hash]
            while not curr.is_challenge_block(self.constants):
                if curr.first_in_sub_slot:
                    slots_to_check -= 1
                    if slots_to_check == 0:
                        break
                curr = blocks[curr.prev_hash]
                if curr.is_challenge_block(self.constants):
                    prev_icc_ip_iters = curr.ip_iters(self.constants)
            return (
                SubEpochChallengeSegmentV2(
                    sub_epoch_n,
                    sub_slots,
                    first_rc_end_of_slot_vdf,
                    end_of_slot_info,
                    end_of_slot_icc_challenge,
                    prev_icc_ip_iters,
                ),
                end_height,
            )
        return SubEpochChallengeSegmentV2(sub_epoch_n, sub_slots, None, None, None, None), end_height

    # returns a challenge chain vdf from slot start to signage point
    def __first_sub_slot_vdfs(
        self,
        header_block: HeaderBlock,
        header_blocks: Dict[bytes32, HeaderBlock],
        blocks: Dict[bytes32, BlockRecord],
        first_in_sub_epoch: bool,
        executor: ProcessPoolExecutor,
    ) -> Tuple[Optional[List[SubSlotDataV2]], Optional[EndOfSubSlotBundle]]:
        # combine cc vdfs of all reward blocks from the start of the sub slot to end
        # find slot start
        curr_sub_rec = blocks[header_block.header_hash]

        while curr_sub_rec.height != 0 and blocks[curr_sub_rec.prev_hash].deficit != 0:
            curr_sub_rec = blocks[curr_sub_rec.prev_hash]
        end_of_slot_bundle = None
        if first_in_sub_epoch and curr_sub_rec.height > 0:
            if curr_sub_rec.sub_epoch_summary_included is None:
                log.error("expected sub epoch summary")
                return None, None
            end_of_slot_bundle = header_blocks[curr_sub_rec.header_hash].finished_sub_slots[-1]

        sub_slots_data: List[SubSlotDataV2] = []
        tmp_sub_slots_data: List[SubSlotDataV2] = []
        curr = header_blocks[curr_sub_rec.header_hash]
        log.debug(f"challenge starts at {curr.height}")
        while curr.height < header_block.height:
            if curr is None:
                log.error("failed fetching block")
                return None, None
            if curr.first_in_sub_slot:
                # if not blue boxed
                if not blue_boxed_end_of_slot(curr.finished_sub_slots[0]):
                    sub_slots_data.extend(tmp_sub_slots_data)
                for idx, sub_slot in enumerate(curr.finished_sub_slots):
                    sub_slots_data.append(handle_finished_slots(sub_slot))
                tmp_sub_slots_data = []
            tmp_sub_slots_data.append(handle_block_vdfs(executor, self.constants, curr, blocks))
            curr = header_blocks[self.blockchain.height_to_hash(uint32(curr.height + 1))]

        if len(tmp_sub_slots_data) > 0:
            sub_slots_data.extend(tmp_sub_slots_data)

        for idx, sub_slot in enumerate(header_block.finished_sub_slots):
            sub_slots_data.append(handle_finished_slots(sub_slot))

        return sub_slots_data, end_of_slot_bundle

    def first_rc_end_of_slot_vdf(
        self,
        header_block,
        blocks: Dict[bytes32, BlockRecord],
        header_blocks: Dict[bytes32, HeaderBlock],
    ) -> Optional[VDFInfo]:
        curr = blocks[header_block.header_hash]
        while curr.height > 0 and not curr.sub_epoch_summary_included:
            curr = blocks[curr.prev_hash]
        return header_blocks[curr.header_hash].finished_sub_slots[-1].reward_chain.end_of_slot_vdf

    def __slot_end_vdf(
        self,
        start_height: uint32,
        header_blocks: Dict[bytes32, HeaderBlock],
        blocks: Dict[bytes32, BlockRecord],
        executor: ProcessPoolExecutor,
    ) -> Tuple[Optional[List[SubSlotDataV2]], uint32]:
        # gets all vdfs first sub slot after challenge block to last sub slot
        log.debug(f"slot end vdf start height {start_height}")
        curr = header_blocks[self.blockchain.height_to_hash(start_height)]
        sub_slots_data: List[SubSlotDataV2] = []
        tmp_sub_slots_data: List[SubSlotDataV2] = []
        while not blocks[curr.header_hash].is_challenge_block(self.constants):
            if curr.first_in_sub_slot:
                # add collected vdfs
                sub_slots_data.extend(tmp_sub_slots_data)
                for idx, sub_slot in enumerate(curr.finished_sub_slots):
                    sub_slots_data.append(handle_finished_slots(sub_slot))
                tmp_sub_slots_data = []
            # if overflow block and challenge slot ended break
            # find input
            tmp_sub_slots_data.append(handle_block_vdfs(executor, self.constants, curr, blocks))
            curr = header_blocks[self.blockchain.height_to_hash(uint32(curr.height + 1))]
            if blocks[curr.header_hash].deficit == self.constants.MIN_BLOCKS_PER_CHALLENGE_BLOCK:
                break

        if len(tmp_sub_slots_data) > 0:
            sub_slots_data.extend(tmp_sub_slots_data)
        log.debug(f"slot end vdf end height {curr.height} ")
        return sub_slots_data, curr.height


def vars_to_bytes(constants, summaries, weight_proof):
    constants_dict = recurse_jsonify(dataclasses.asdict(constants))
    wp_recent_chain_bytes = bytes(RecentChainData(weight_proof.recent_chain_data))
    wp_segment_bytes = bytes(SubEpochSegmentsV2(weight_proof.sub_epoch_segments))
    summary_bytes = []
    for summary in summaries:
        summary_bytes.append(bytes(summary))
    return constants_dict, summary_bytes, wp_segment_bytes, wp_recent_chain_bytes


def _get_weights_for_sampling(rng: random.Random, total_weight: uint128, last_l_weight) -> Optional[List[uint128]]:
    weight_to_check = []
    delta = last_l_weight / total_weight
    prob_of_adv_succeeding = 1 - math.log(WeightProofHandlerV2.C, delta)
    if prob_of_adv_succeeding <= 0:
        return None
    queries = -WeightProofHandlerV2.LAMBDA_L * math.log(2, prob_of_adv_succeeding)
    for i in range(int(queries) + 1):
        u = rng.random()
        q = 1 - delta**u
        # todo check division and type conversions
        weight = q * float(total_weight)
        weight_to_check.append(uint128(weight))
    weight_to_check.sort()
    return weight_to_check


# wp creation methods
def _create_sub_epoch_data(
    sub_epoch_summary: SubEpochSummary,
) -> SubEpochData:
    reward_chain_hash: bytes32 = sub_epoch_summary.reward_chain_hash
    #  Number of subblocks overflow in previous slot
    previous_sub_epoch_overflows: uint8 = sub_epoch_summary.num_blocks_overflow  # total in sub epoch - expected
    #  New work difficulty and iterations per sub-slot
    sub_slot_iters: Optional[uint64] = sub_epoch_summary.new_sub_slot_iters
    new_difficulty: Optional[uint64] = sub_epoch_summary.new_difficulty
    return SubEpochData(reward_chain_hash, previous_sub_epoch_overflows, sub_slot_iters, new_difficulty)


def handle_block_vdfs(
    executor: ProcessPoolExecutor,
    constants: ConsensusConstants,
    header_block: HeaderBlock,
    blocks: Dict[bytes32, BlockRecord],
):
    block_rec = blocks[header_block.header_hash]
    compressed_sp_output = None
    if header_block.challenge_chain_sp_proof is not None:
        assert header_block.reward_chain_block.challenge_chain_sp_vdf
        sp_input = ClassgroupElement.get_default_element()
        sp_iters = header_block.reward_chain_block.challenge_chain_sp_vdf.number_of_iterations
        if not header_block.challenge_chain_sp_proof.normalized_to_identity:
            (_, _, sp_input, _, _, sp_iters) = get_signage_point_vdf_info(
                constants,
                header_block.finished_sub_slots,
                block_rec.overflow,
                None if header_block.height == 0 else blocks[header_block.prev_header_hash],
                BlockCache(blocks),
                block_rec.sp_total_iters(constants),
                block_rec.sp_iters(constants),
            )
        compressed_sp_output = compress_output(
            constants.DISCRIMINANT_SIZE_BITS,
            header_block.reward_chain_block.challenge_chain_sp_vdf.challenge,
            sp_input,
            header_block.reward_chain_block.challenge_chain_sp_vdf.output,
            header_block.challenge_chain_sp_proof,
            sp_iters,
            executor,
        )
    cc_ip_input = ClassgroupElement.get_default_element()
    cc_ip_iters = block_rec.ip_iters(constants)
    prev_block = None
    if header_block.height > 0 and not block_rec.first_in_sub_slot:
        prev_block = blocks[header_block.prev_header_hash]
        if not header_block.challenge_chain_ip_proof.normalized_to_identity:
            cc_ip_input = prev_block.challenge_vdf_output
            cc_ip_iters = uint64(header_block.total_iters - prev_block.total_iters)
    compressed_cc_ip_output = compress_output(
        constants.DISCRIMINANT_SIZE_BITS,
        header_block.reward_chain_block.challenge_chain_ip_vdf.challenge,
        cc_ip_input,
        header_block.reward_chain_block.challenge_chain_ip_vdf.output,
        header_block.challenge_chain_ip_proof,
        cc_ip_iters,
        executor,
    )
    compressed_icc_ip_output = None
    if header_block.infused_challenge_chain_ip_proof is not None:
        icc_ip_iters = block_rec.ip_iters(constants)
        icc_ip_input = ClassgroupElement.get_default_element()
        if not block_rec.first_in_sub_slot:
            assert prev_block is not None
            icc_ip_iters = uint64(header_block.total_iters - prev_block.total_iters)
            if not prev_block.is_challenge_block(constants):
                assert prev_block.infused_challenge_vdf_output is not None
                icc_ip_input = prev_block.infused_challenge_vdf_output
        assert header_block.reward_chain_block.infused_challenge_chain_ip_vdf
        compressed_icc_ip_output = compress_output(
            constants.DISCRIMINANT_SIZE_BITS,
            header_block.reward_chain_block.infused_challenge_chain_ip_vdf.challenge,
            icc_ip_input,
            header_block.reward_chain_block.infused_challenge_chain_ip_vdf.output,
            header_block.infused_challenge_chain_ip_proof,
            icc_ip_iters,
            executor,
        )

    return SubSlotDataV2(
        header_block.reward_chain_block.proof_of_space if block_rec.is_challenge_block(constants) else None,
        header_block.challenge_chain_sp_proof,
        header_block.challenge_chain_ip_proof,
        header_block.reward_chain_block.signage_point_index,
        None,
        None if compressed_sp_output is None else CompressedClassgroupElement.from_hex(compressed_sp_output.result()),
        CompressedClassgroupElement.from_hex(compressed_cc_ip_output.result()),
        None,
        header_block.infused_challenge_chain_ip_proof,
        None
        if compressed_icc_ip_output is None
        else CompressedClassgroupElement.from_hex(compressed_icc_ip_output.result()),
        None,
        None,
        header_block.reward_chain_block.challenge_chain_sp_signature
        if block_rec.is_challenge_block(constants)
        else None,
        blocks[header_block.header_hash].ip_iters(constants),
        header_block.total_iters,
    )


def handle_finished_slots(end_of_slot: EndOfSubSlotBundle) -> SubSlotDataV2:
    icc_slot_end_output = None
    if end_of_slot.infused_challenge_chain is not None:
        icc_slot_end_output = end_of_slot.infused_challenge_chain.infused_challenge_chain_end_of_slot_vdf.output
    return SubSlotDataV2(
        None,
        None,
        None,
        None,
        end_of_slot.proofs.challenge_chain_slot_proof,
        None,
        None,
        end_of_slot.challenge_chain.challenge_chain_end_of_slot_vdf.output,
        None,
        None,
        end_of_slot.proofs.infused_challenge_chain_slot_proof,
        icc_slot_end_output,
        None,
        None,
        None,
    )


def compress_segments(
    full_segment_index, segments: List[SubEpochChallengeSegmentV2]
) -> List[SubEpochChallengeSegmentV2]:
    compressed_segments = []
    for idx, segment in enumerate(segments):
        if idx != full_segment_index:
            # remove all redundant values
            segment = compress_segment(segment)
        compressed_segments.append(segment)
    return compressed_segments


def compress_segment(segment: SubEpochChallengeSegmentV2) -> SubEpochChallengeSegmentV2:
    # find challenge slot
    comp_seg = SubEpochChallengeSegmentV2(
        segment.sub_epoch_n,
        [],
        segment.rc_slot_end_info,
        segment.cc_slot_end_info,
        segment.icc_sub_slot_hash,
        segment.prev_icc_ip_iters,
    )
    after_challenge = False
    for subslot_data in segment.sub_slot_data:
        new_slot = subslot_data
        if after_challenge:
            new_slot = dataclasses.replace(
                subslot_data,
                cc_signage_point=None,
                cc_infusion_point=None,
                cc_slot_end=None,
                icc_infusion_point=None,
                icc_slot_end=None,
            )
        if subslot_data.is_challenge():
            after_challenge = True
        comp_seg.sub_slot_data.append(new_slot)
    return comp_seg


# ///////////////////////
# wp validation methods
# //////////////////////
def _validate_sub_epoch_summaries(
    constants: ConsensusConstants,
    weight_proof: WeightProofV2,
) -> Tuple[Optional[List[SubEpochSummary]], Optional[List[uint128]]]:

    last_ses_hash, last_ses_sub_height, last_ses_sub_weight = _get_last_ses(constants, weight_proof.recent_chain_data)
    if last_ses_hash is None:
        log.warning("could not find last ses block")
        return None, None

    summaries, total, sub_epoch_weight_list = _map_sub_epoch_summaries(
        constants,
        weight_proof.sub_epochs,
    )

    log.info(f"validating {len(summaries)} sub epochs, sub epoch data weight {total}")
    # validate weight
    if not _validate_summaries_weight(constants, total, summaries, weight_proof):
        log.error("failed validating weight")
        return None, None

    # add last ses weight from recent chain
    sub_epoch_weight_list.append(last_ses_sub_weight)
    last_ses = summaries[-1]
    log.debug(f"last ses sub height {last_ses_sub_height}")
    # validate last ses_hash
    if last_ses.get_hash() != last_ses_hash:
        log.error(f"failed to validate ses hashes block height {last_ses_sub_height}")
        return None, None

    return summaries, sub_epoch_weight_list


def _map_sub_epoch_summaries(
    constants: ConsensusConstants,
    sub_epoch_data: List[SubEpochData],
) -> Tuple[List[SubEpochSummary], uint128, List[uint128]]:
    total_weight: uint128 = uint128(0)
    summaries: List[SubEpochSummary] = []
    sub_epoch_weight_list: List[uint128] = []
    ses_hash = constants.GENESIS_CHALLENGE
    curr_difficulty = constants.DIFFICULTY_STARTING
    for idx, data in enumerate(sub_epoch_data):
        ses = SubEpochSummary(
            ses_hash,
            data.reward_chain_hash,
            data.num_blocks_overflow,
            data.new_difficulty,
            data.new_sub_slot_iters,
        )

        if idx < len(sub_epoch_data) - 1:
            delta = sub_epoch_data[idx].num_blocks_overflow
            log.debug(f"sub epoch {idx} start weight is {total_weight + curr_difficulty} ")
            sub_epoch_weight_list.append(uint128(total_weight + curr_difficulty))
            total_weight = total_weight + uint128(  # type: ignore
                curr_difficulty * (constants.SUB_EPOCH_BLOCKS + sub_epoch_data[idx + 1].num_blocks_overflow - delta)
            )

        # if new epoch update diff and iters
        if data.new_difficulty is not None:
            curr_difficulty = data.new_difficulty

        # add to dict
        summaries.append(ses)
        ses_hash = std_hash(ses)
    # add last sub epoch weight
    sub_epoch_weight_list.append(uint128(total_weight + curr_difficulty))
    return summaries, total_weight, sub_epoch_weight_list


def _validate_sub_epoch_segments(
    constants_dict: Dict,
    rng: random.Random,
    sub_epoch_segments: List[SubEpochChallengeSegmentV2],
    summaries_bytes: List[bytes],
    executor: ProcessPoolExecutor,
) -> bool:
    segments_by_sub_epoch = map_segments_by_sub_epoch(sub_epoch_segments)
    ses_futures = []
    for sub_epoch_n, segments in segments_by_sub_epoch.items():
        sampled_seg_index = rng.choice(range(len(segments)))
        ses_futures.append(
            executor.submit(
                validate_sub_epoch,
                constants_dict,
                sampled_seg_index,
                bytes(SubEpochSegmentsV2(segments)),
                sub_epoch_n,
                summaries_bytes,
            )
        )
    for x in as_completed(ses_futures):
        log.info("sub epoch done")
        if x.result() is False:
            return False

    return True


def validate_sub_epoch(constants_dict, sampled_seg_index, segment_bytes, sub_epoch_n, summaries_bytes):
    log.info(f"validate sub epoch {sub_epoch_n}")
    prev_ses: Optional[SubEpochSummary] = None
    total_blocks, total_ip_iters = 0, 0
    total_ip_iters, total_slot_iters, total_slots = 0, 0, 0
    constants, summaries = bytes_to_vars(constants_dict, summaries_bytes)
    segments = SubEpochSegmentsV2.from_bytes(segment_bytes).challenge_segments
    # recreate RewardChainSubSlot for next ses rc_hash
    curr_difficulty, curr_ssi = _get_curr_diff_ssi(constants, sub_epoch_n, summaries)
    if sub_epoch_n == 0:
        rc_sub_slot_hash, cc_sub_slot_hash, icc_sub_slot_hash = (
            constants.GENESIS_CHALLENGE,
            constants.GENESIS_CHALLENGE,
            None,
        )
    else:
        prev_ses = summaries[sub_epoch_n - 1]
        rc_sub_slot_hash = __get_rc_sub_slot_hash(constants, segments[0], summaries, curr_ssi)
        if rc_sub_slot_hash is None:
            log.error("failed getting rc sub slot hash")
            return False
        assert segments[0].cc_slot_end_info
        cc_sub_slot_hash = segments[0].cc_slot_end_info.challenge
        icc_sub_slot_hash = segments[0].icc_sub_slot_hash
    if not summaries[sub_epoch_n].reward_chain_hash == rc_sub_slot_hash:
        log.error(f"failed reward_chain_hash validation sub_epoch {sub_epoch_n}")
        return False
    ip_iters = uint64(0)
    slot_after_challenge = False
    with ProcessPoolExecutor() as executor:
        for idx, segment in enumerate(segments):
            log.debug(f"validate segment {idx} sampled:{sampled_seg_index == idx}")
            res = _validate_segment(
                constants,
                segment,
                curr_ssi,
                curr_difficulty,
                prev_ses if idx == 0 else None,
                sampled_seg_index == idx,
                cc_sub_slot_hash,
                icc_sub_slot_hash,
                uint64(0) if slot_after_challenge else ip_iters,
                executor,
            )
            if res is None:
                log.error(f"failed to validate sub_epoch {segment.sub_epoch_n} segment {idx} slots")
                return False
            ip_iters, slot_iters, slots, cc_sub_slot_hash, icc_sub_slot_hash, slot_after_challenge = res
            log.debug(f"cc sub slot hash {cc_sub_slot_hash}")
            if prev_ses is not None and prev_ses.new_sub_slot_iters is not None:
                curr_ssi = prev_ses.new_sub_slot_iters
            if prev_ses is not None and prev_ses.new_difficulty is not None:
                curr_difficulty = prev_ses.new_difficulty

            total_blocks += 1
            total_slot_iters += slot_iters
            total_slots += slots
            total_ip_iters += ip_iters
            avg_ip_iters = total_ip_iters / total_blocks
            avg_slot_iters = total_slot_iters / total_slots
            if avg_slot_iters / avg_ip_iters < float(constants.WEIGHT_PROOF_THRESHOLD):
                log.error(f"bad avg challenge block positioning ratio: {avg_slot_iters / avg_ip_iters}")
                return False
    log.info(f"validated sub epoch {sub_epoch_n}")
    return True


def _validate_segment(
    constants: ConsensusConstants,
    segment: SubEpochChallengeSegmentV2,
    curr_ssi: uint64,
    curr_difficulty: uint64,
    ses: Optional[SubEpochSummary],
    sampled: bool,
    cc_challenge: bytes32,
    icc_challenge: Optional[bytes32],
    prev_challenge_ip_iters: uint64,
    executor: ProcessPoolExecutor,
) -> Optional[Tuple[uint64, uint64, int, bytes32, bytes32, bool]]:
    slot_iters, slots = uint64(0), 0

    output_cache: Dict[CompressedClassgroupElement, ClassgroupElement] = {}
    sub_slot_data = segment.sub_slot_data
    first_block = True
    prev_cc_challenge = None
    after_challenge_block = False
    slot_after_challenge_block = False
    deficit = 0
    for idx, ssd in enumerate(sub_slot_data):
        if ssd.is_challenge():
            assert ssd.ip_iters
            prev_challenge_ip_iters = ssd.ip_iters
            deficit = constants.MIN_BLOCKS_PER_CHALLENGE_BLOCK - 1
        # sampled validate vdfs
        if after_challenge_block and not ssd.is_end_of_slot():
            deficit -= 1

        if ssd.is_challenge():
            # validate challenge block vdfs and pospace
            assert cc_challenge is not None
            res = _validate_challenge_sub_slot_data(
                constants,
                idx,
                sub_slot_data,
                curr_difficulty,
                curr_ssi,
                cc_challenge,
                prev_cc_challenge,
                output_cache,
                sampled,
            )
            if res is None:
                log.error(f"failed to validate challenge slot {idx} vdfs")
                return None
            icc_challenge = res
            after_challenge_block = True
        elif sampled and after_challenge_block:
            # all vdfs from challenge block to end of segment
            assert icc_challenge
            if ssd.is_end_of_slot():
                if not validate_eos(cc_challenge, icc_challenge, constants, sub_slot_data, idx, curr_ssi, output_cache):
                    log.error(f"failed to validate end of sub slot data {idx} vdfs")
                    return None
            elif not _validate_sub_slot_data(
                constants,
                idx,
                sub_slot_data,
                curr_ssi,
                cc_challenge,
                prev_cc_challenge,
                icc_challenge,
                output_cache,
            ):
                log.error(f"failed to validate sub slot data {idx} vdfs")
                return None
        elif not after_challenge_block and not ssd.is_end_of_slot():
            # overflow blocks before challenge block, compute sp compressed output
            assert icc_challenge
            if not handle_overflows(
                cc_challenge, icc_challenge, constants, first_block, idx, output_cache, sub_slot_data
            ):
                log.error(f"failed to validate sub slot data {idx} vdfs (overflow)")
                return None
        if ssd.is_end_of_slot():
            if after_challenge_block:
                slot_after_challenge_block = True
            prev_cc_challenge = cc_challenge
            cc_challenge, icc_challenge = get_cc_sub_slot(
                cc_challenge,
                icc_challenge,
                curr_ssi,
                ses if idx == 0 else None,
                segment,
                idx,
                deficit,
                prev_challenge_ip_iters,
            )
            slot_iters = uint64(slot_iters + curr_ssi)
            slots = uint64(slots + 1)
            if ses is not None and ses.new_sub_slot_iters is not None:
                curr_ssi = ses.new_sub_slot_iters
            if ses is not None and ses.new_difficulty is not None:
                curr_difficulty = ses.new_difficulty

        else:
            first_block = False
    assert icc_challenge
    return prev_challenge_ip_iters, slot_iters, slots, cc_challenge, icc_challenge, slot_after_challenge_block


def get_cc_sub_slot(
    challenge: bytes32,
    icc_challenge: Optional[bytes32],
    curr_ssi: uint64,
    ses: Optional[SubEpochSummary],  # if end of sub epoch
    segment: SubEpochChallengeSegmentV2,
    index: int,
    prev_deficit: int,
    prev_challenge_ip_iters: uint64,
) -> Tuple[bytes32, Optional[bytes32]]:
    ssd = segment.sub_slot_data[index]
    icc_hash = icc_challenge
    if ssd.icc_slot_end_output is not None:
        icc_iters = curr_ssi
        if index == 0:
            if segment.cc_slot_end_info is None:
                icc_iters = uint64(curr_ssi - prev_challenge_ip_iters)
            elif segment.prev_icc_ip_iters is not None:
                icc_iters = uint64(curr_ssi - segment.prev_icc_ip_iters)
        else:
            for sub_slot in reversed(segment.sub_slot_data[:index]):
                if sub_slot.is_challenge():
                    assert sub_slot.ip_iters
                    icc_iters = uint64(curr_ssi - sub_slot.ip_iters)
                    break
                if sub_slot.is_end_of_slot():
                    break
        assert icc_challenge
        icc_hash = VDFInfo(icc_challenge, icc_iters, ssd.icc_slot_end_output).get_hash()
    assert ssd.cc_slot_end_output
    cc_sub_slot = ChallengeChainSubSlot(
        VDFInfo(challenge, curr_ssi, ssd.cc_slot_end_output),
        icc_hash if prev_deficit == 0 else None,
        None if ses is None else ses.get_hash(),
        None if ses is None else ses.new_sub_slot_iters,
        None if ses is None else ses.new_difficulty,
    )
    log.debug(f"cc sub slot {cc_sub_slot} {cc_sub_slot.get_hash()} icc hash {icc_hash}")
    return cc_sub_slot.get_hash(), icc_hash


def handle_overflows(
    cc_sub_slot_hash: bytes32,
    icc_sub_slot_hash: bytes32,
    constants: ConsensusConstants,
    first_block: bool,
    idx: int,
    long_outputs: Dict[CompressedClassgroupElement, ClassgroupElement],
    sub_slots_data: List[SubSlotDataV2],
):
    ssd = sub_slots_data[idx]
    ip_input = ClassgroupElement.get_default_element()
    iterations = ssd.ip_iters
    prev_ssd = None
    if not first_block:
        prev_ssd = sub_slots_data[idx - 1]
        assert ssd.cc_infusion_point
        if not ssd.cc_infusion_point.normalized_to_identity and not prev_ssd.is_end_of_slot():
            assert prev_ssd.cc_ip_vdf_output is not None
            assert prev_ssd.total_iters is not None
            assert ssd.total_iters is not None
            ip_input = long_outputs[prev_ssd.cc_ip_vdf_output]
            iterations = uint64(ssd.total_iters - prev_ssd.total_iters)
    assert ssd.cc_ip_vdf_output is not None
    assert ssd.cc_infusion_point is not None
    assert iterations is not None
    valid, output = verify_compressed_vdf(
        constants, cc_sub_slot_hash, ip_input, ssd.cc_ip_vdf_output, ssd.cc_infusion_point, iterations
    )
    if not valid:
        log.error(f"failed cc infusion point vdf validation {cc_sub_slot_hash}")
        return False
    long_outputs[ssd.cc_ip_vdf_output] = output
    if ssd.icc_infusion_point is not None:
        icc_ip_input = ClassgroupElement.get_default_element()
        if prev_ssd is not None:
            if prev_ssd.icc_infusion_point is not None and not prev_ssd.is_challenge():
                if not ssd.icc_infusion_point.normalized_to_identity:
                    if prev_ssd.icc_ip_vdf_output not in long_outputs:
                        return False
                    icc_ip_input = long_outputs[prev_ssd.icc_ip_vdf_output]
        assert ssd.icc_ip_vdf_output
        valid, output = verify_compressed_vdf(
            constants,
            icc_sub_slot_hash,
            icc_ip_input,
            ssd.icc_ip_vdf_output,
            ssd.icc_infusion_point,
            iterations,
        )
        if not valid:
            log.error(f"failed icc signage point vdf validation ")
            return False
        assert ssd.icc_ip_vdf_output
        long_outputs[ssd.icc_ip_vdf_output] = output

    return True


def validate_eos(
    cc_sub_slot_hash: bytes32,
    icc_challenge: bytes32,
    constants: ConsensusConstants,
    sub_slots_data: List[SubSlotDataV2],
    idx: int,
    ssi: uint64,
    long_outputs,
):
    cc_eos_iters = ssi
    cc_ip_input = ClassgroupElement.get_default_element()
    ssd = sub_slots_data[idx]
    prev_ssd = sub_slots_data[idx - 1]
    if not prev_ssd.is_end_of_slot():
        assert ssd.cc_slot_end
        if not ssd.cc_slot_end.normalized_to_identity:
            assert prev_ssd.cc_ip_vdf_output
            assert prev_ssd.ip_iters
            cc_ip_input = long_outputs[prev_ssd.cc_ip_vdf_output]
            cc_eos_iters = uint64(ssi - prev_ssd.ip_iters)
    assert ssd.cc_slot_end_output
    cc_slot_end_info = VDFInfo(cc_sub_slot_hash, cc_eos_iters, ssd.cc_slot_end_output)
    assert ssd.cc_slot_end
    if not ssd.cc_slot_end.is_valid(constants, cc_ip_input, cc_slot_end_info):
        log.error(f"failed cc slot end validation  {cc_slot_end_info} \n input {cc_ip_input}")
        return False

    icc_ip_input = ClassgroupElement.get_default_element()
    icc_eos_iters: uint64 = ssi
    if not prev_ssd.is_end_of_slot():
        if not ssd.cc_slot_end.normalized_to_identity:
            if prev_ssd.icc_ip_vdf_output is not None:
                icc_ip_input = long_outputs[prev_ssd.icc_ip_vdf_output]
            assert prev_ssd.ip_iters
            icc_eos_iters = uint64(ssi - prev_ssd.ip_iters)
        else:
            for sub_slot in reversed(sub_slots_data[:idx]):
                if sub_slot.is_challenge():
                    assert sub_slot.ip_iters
                    icc_eos_iters = uint64(ssi - sub_slot.ip_iters)
                    break
                if sub_slot.is_end_of_slot():
                    break

    assert ssd.icc_slot_end_output
    icc_slot_end_info = VDFInfo(icc_challenge, icc_eos_iters, ssd.icc_slot_end_output)
    assert ssd.icc_slot_end
    if not ssd.icc_slot_end.is_valid(constants, icc_ip_input, icc_slot_end_info):
        log.error(f"failed icc slot end validation  {icc_slot_end_info} \n input {cc_ip_input}")
        return False
    return True


def _validate_challenge_sub_slot_data(
    constants: ConsensusConstants,
    sub_slot_idx: int,
    sub_slots: List[SubSlotDataV2],
    curr_difficulty: uint64,
    ssi: uint64,
    challenge: bytes32,
    prev_challenge: Optional[bytes32],
    long_outputs,
    sampled: bool,
) -> Optional[bytes32]:
    sub_slot_data = sub_slots[sub_slot_idx]
    prev_ssd = None
    if sub_slot_idx > 0:
        prev_ssd = sub_slots[sub_slot_idx - 1]
    assert sub_slot_data.signage_point_index is not None
    sp_info = None
    if sub_slot_data.cc_signage_point:
        is_overflow = is_overflow_block(constants, sub_slot_data.signage_point_index)
        sp_iters = calculate_sp_iters(constants, ssi, sub_slot_data.signage_point_index)
        sp_challenge = challenge
        assert sub_slot_data.cc_sp_vdf_output
        cc_sp_input = ClassgroupElement.get_default_element()
        if is_overflow:
            assert prev_challenge
            sp_challenge = prev_challenge
        if sub_slot_idx > 0 and not sub_slot_data.cc_signage_point.normalized_to_identity:
            sp_total_iters = get_sp_total_iters(sp_iters, is_overflow, ssi, sub_slot_data)
            tmp_input, sp_iters = sub_slot_data_vdf_info(sub_slot_idx, sub_slots, is_overflow, sp_total_iters, sp_iters)
            if isinstance(tmp_input, CompressedClassgroupElement):
                cc_sp_input = long_outputs[tmp_input]
            elif isinstance(tmp_input, ClassgroupElement):
                cc_sp_input = tmp_input
        sp_valid, sp_output = verify_compressed_vdf(
            constants,
            sp_challenge,
            cc_sp_input,
            sub_slot_data.cc_sp_vdf_output,
            sub_slot_data.cc_signage_point,
            sp_iters,
        )
        if not sp_valid:
            log.error(f"failed cc signage point vdf validation ")
            return None
        long_outputs[sub_slot_data.cc_sp_vdf_output] = sp_output
        sp_iters = calculate_sp_iters(
            constants,
            ssi,
            sub_slot_data.signage_point_index,
        )
        assert sp_challenge
        sp_info = VDFInfo(sp_challenge, sp_iters, sp_output)

    cc_ip_input = ClassgroupElement.get_default_element()
    ip_vdf_iters = sub_slot_data.ip_iters
    assert sub_slot_data.cc_infusion_point
    if not sub_slot_data.cc_infusion_point.normalized_to_identity:
        if prev_ssd is not None and not prev_ssd.is_end_of_slot():
            assert prev_ssd.cc_ip_vdf_output
            cc_ip_input = long_outputs[prev_ssd.cc_ip_vdf_output]
            assert sub_slot_data
            assert sub_slot_data.total_iters
            assert prev_ssd.total_iters
            ip_vdf_iters = uint64(sub_slot_data.total_iters - prev_ssd.total_iters)
    assert ip_vdf_iters
    assert sub_slot_data.cc_ip_vdf_output
    ip_valid, ip_output = verify_compressed_vdf(
        constants,
        challenge,
        cc_ip_input,
        sub_slot_data.cc_ip_vdf_output,
        sub_slot_data.cc_infusion_point,
        ip_vdf_iters,
    )
    if not ip_valid:
        log.error(f"failed cc infusion point vdf validation ")
        return None
    long_outputs[sub_slot_data.cc_ip_vdf_output] = ip_output

    pospace_challenge = challenge
    assert sub_slot_data.signage_point_index is not None
    if sampled:
        if is_overflow_block(constants, sub_slot_data.signage_point_index):
            assert prev_challenge
            pospace_challenge = prev_challenge
        required_iters = __validate_pospace(
            constants, sub_slot_data, curr_difficulty, pospace_challenge, ssi, long_outputs
        )
        if required_iters is None:
            log.error(f"failed to get requierd iterations for challenge slot {sub_slot_idx} vdfs")
            return None

    assert sub_slot_data.proof_of_space
    assert sub_slot_data.ip_iters
    cbi = ChallengeBlockInfo(
        sub_slot_data.proof_of_space,
        sp_info,
        sub_slot_data.cc_sp_signature,
        VDFInfo(challenge, sub_slot_data.ip_iters, ip_output),
    )
    return cbi.get_hash()


def _validate_sub_slot_data(
    constants: ConsensusConstants,
    sub_slot_idx: int,
    sub_slots: List[SubSlotDataV2],
    ssi: uint64,
    cc_challenge: bytes32,
    prev_cc_sub_slot_hash: Optional[bytes32],
    icc_challenge: bytes32,
    long_outputs: Dict[CompressedClassgroupElement, ClassgroupElement],
) -> bool:
    sub_slot_data = sub_slots[sub_slot_idx]
    prev_ssd = sub_slots[sub_slot_idx - 1]
    # find next end of slot
    if sub_slot_data.cc_signage_point:
        assert sub_slot_data.cc_sp_vdf_output is not None
        assert sub_slot_data.signage_point_index is not None
        sp_iters = calculate_sp_iters(constants, ssi, sub_slot_data.signage_point_index)
        cc_sp_input = ClassgroupElement.get_default_element()
        iterations = sp_iters
        challenge = cc_challenge
        is_overflow = is_overflow_block(constants, sub_slot_data.signage_point_index)
        if is_overflow:
            assert prev_cc_sub_slot_hash is not None
            challenge = prev_cc_sub_slot_hash
        if not sub_slot_data.cc_signage_point.normalized_to_identity:
            sp_total_iters = get_sp_total_iters(sp_iters, is_overflow, ssi, sub_slot_data)
            tmp_input, iterations = sub_slot_data_vdf_info(
                sub_slot_idx, sub_slots, is_overflow, sp_total_iters, sp_iters
            )
            if isinstance(tmp_input, CompressedClassgroupElement):
                cc_sp_input = long_outputs[tmp_input]
            elif isinstance(tmp_input, ClassgroupElement):
                cc_sp_input = tmp_input
        sp_valid, sp_output = verify_compressed_vdf(
            constants,
            challenge,
            cc_sp_input,
            sub_slot_data.cc_sp_vdf_output,
            sub_slot_data.cc_signage_point,
            iterations,
        )
        if not sp_valid:
            log.error(f"failed cc signage point vdf validation ")
            return False
        assert sub_slot_data.cc_sp_vdf_output
        long_outputs[sub_slot_data.cc_sp_vdf_output] = sp_output
    cc_ip_input = ClassgroupElement.get_default_element()
    assert sub_slot_data.ip_iters
    ip_vdf_iters = sub_slot_data.ip_iters
    assert sub_slot_data.cc_infusion_point is not None
    if not prev_ssd.is_end_of_slot() and not sub_slot_data.cc_infusion_point.normalized_to_identity:
        assert prev_ssd.cc_ip_vdf_output
        assert sub_slot_data.total_iters
        assert prev_ssd.total_iters
        cc_ip_input = long_outputs[prev_ssd.cc_ip_vdf_output]
        ip_vdf_iters = uint64(sub_slot_data.total_iters - prev_ssd.total_iters)
    assert sub_slot_data.cc_ip_vdf_output is not None
    ip_valid, ip_output = verify_compressed_vdf(
        constants,
        cc_challenge,
        cc_ip_input,
        sub_slot_data.cc_ip_vdf_output,
        sub_slot_data.cc_infusion_point,
        ip_vdf_iters,
    )
    if not ip_valid:
        log.error(f"failed cc infusion point vdf validation {cc_challenge}")
        return False
    long_outputs[sub_slot_data.cc_ip_vdf_output] = ip_output

    if sub_slot_data.icc_infusion_point is not None:
        icc_ip_vdf_iters = sub_slot_data.ip_iters
        icc_ip_input = ClassgroupElement.get_default_element()
        assert prev_ssd is not None
        if not prev_ssd.is_end_of_slot():
            assert sub_slot_data.total_iters is not None
            assert prev_ssd.total_iters is not None
            icc_ip_vdf_iters = uint64(sub_slot_data.total_iters - prev_ssd.total_iters)
            if not prev_ssd.is_challenge():
                assert prev_ssd.icc_ip_vdf_output is not None
                icc_ip_input = long_outputs[prev_ssd.icc_ip_vdf_output]
        assert sub_slot_data.icc_ip_vdf_output is not None
        assert icc_challenge is not None
        icc_ip_valid, icc_ip_output = verify_compressed_vdf(
            constants,
            icc_challenge,
            icc_ip_input,
            sub_slot_data.icc_ip_vdf_output,
            sub_slot_data.icc_infusion_point,
            icc_ip_vdf_iters,
        )
        if not icc_ip_valid:
            log.error(f"failed icc infusion point vdf validation")
            return False
        long_outputs[sub_slot_data.icc_ip_vdf_output] = icc_ip_output

    return True


def normalized_can_skip(constants, sub_slot_data, sub_slot_idx, sub_slots):
    idx = sub_slot_idx
    while idx < len(sub_slots) - 1:
        curr_slot = sub_slots[idx]
        if curr_slot.is_end_of_slot():
            # dont validate intermediate vdfs if slot is blue boxed
            assert curr_slot.cc_slot_end
            assert curr_slot.icc_slot_end
            if (
                curr_slot.cc_slot_end.normalized_to_identity is True
                and curr_slot.icc_slot_end.normalized_to_identity is True
            ):
                log.debug(f"skip intermediate vdfs slot {sub_slot_idx}")
                return True
            else:
                break
        idx += 1
    assert sub_slot_data.signage_point_index is not None
    if is_overflow_block(constants, sub_slot_data.signage_point_index):
        idx = sub_slot_idx
        while idx > 0:
            curr_slot = sub_slots[idx]
            if curr_slot.is_end_of_slot():
                # dont validate intermediate vdfs if slot is blue boxed
                assert curr_slot.cc_slot_end
                assert curr_slot.icc_slot_end
                if (
                    curr_slot.cc_slot_end.normalized_to_identity is True
                    and curr_slot.icc_slot_end.normalized_to_identity is True
                ):
                    log.debug(f"skip intermediate vdfs slot {sub_slot_idx}")
                    return True
                else:
                    break
            idx -= 1
    return False


def sub_slot_data_vdf_info(
    sub_slot_idx: int,
    sub_slots: List[SubSlotDataV2],
    is_overflow: bool,
    sp_total_iters: uint128,
    sp_iters: uint64,
) -> Tuple[Union[CompressedClassgroupElement, ClassgroupElement], uint64]:
    ssd: Optional[SubSlotDataV2] = None
    slots_n = 1
    if is_overflow:
        slots_n = 2
    slots_seen = 0
    reached_start = False
    for ssd_idx in reversed(range(0, sub_slot_idx)):
        ssd = sub_slots[ssd_idx]
        assert ssd is not None
        if ssd.is_end_of_slot():
            slots_seen += 1
            if slots_seen == slots_n:
                break
        else:
            assert ssd.total_iters
            if not (ssd.total_iters > sp_total_iters):
                break
        if ssd_idx == 0:
            reached_start = True
    assert ssd is not None
    if not ssd.is_end_of_slot() and not reached_start:
        assert ssd.total_iters
        if ssd.total_iters < sp_total_iters:
            assert ssd.cc_ip_vdf_output
            return ssd.cc_ip_vdf_output, uint64(sp_total_iters - ssd.total_iters)
    return ClassgroupElement.get_default_element(), sp_iters


def __validate_pospace(
    constants: ConsensusConstants,
    ssd: SubSlotDataV2,
    curr_diff: uint64,
    cc_sub_slot_hash: bytes32,
    ssi: uint64,
    long_outputs: Dict[CompressedClassgroupElement, ClassgroupElement],
) -> Optional[uint64]:
    assert ssd.signage_point_index is not None
    sp_iters = calculate_sp_iters(constants, ssi, ssd.signage_point_index)
    if sp_iters == uint64(0):
        cc_sp_hash = cc_sub_slot_hash
    else:
        assert ssd.cc_sp_vdf_output
        cc_sp_hash = long_outputs[ssd.cc_sp_vdf_output].get_hash()

    # validate proof of space
    assert ssd.proof_of_space is not None
    q_str = ssd.proof_of_space.verify_and_get_quality_string(
        constants,
        cc_sub_slot_hash,
        cc_sp_hash,
    )
    if q_str is None:
        log.error(f"could not validate proof of space ")
        return None
    return calculate_iterations_quality(
        constants.DIFFICULTY_CONSTANT_FACTOR,
        q_str,
        ssd.proof_of_space.size,
        curr_diff,
        cc_sp_hash,
    )


def __get_rc_sub_slot_hash(
    constants: ConsensusConstants,
    segment: SubEpochChallengeSegmentV2,
    summaries: List[SubEpochSummary],
    prev_ssi: uint64,
) -> Optional[bytes32]:
    slots = segment.sub_slot_data
    ses = summaries[uint32(segment.sub_epoch_n - 1)]
    # find first block sub epoch
    first_idx = None
    first = None
    for idx, curr in enumerate(segment.sub_slot_data):
        if not curr.is_end_of_slot():
            first_idx = idx
            first = curr
            break

    if first_idx is None or first is None:
        log.error("could not find first block")
        return None

    # number of slots to look for
    slots_n = 1
    assert first.signage_point_index is not None
    overflow = is_overflow_block(constants, first.signage_point_index)
    new_diff: Optional[uint64] = None if ses is None else ses.new_difficulty
    new_ssi: Optional[uint64] = None if ses is None else ses.new_sub_slot_iters
    ses_hash: Optional[bytes32] = None if ses is None else ses.get_hash()

    if overflow and first_idx >= 2:
        if slots[first_idx - 2].is_end_of_slot() is False:
            slots_n = 2
        elif slots[first_idx - 1].is_end_of_slot():
            ses_hash = None
            new_ssi = None
            new_diff = None

    challenge_slot = None
    idx = first_idx - 1
    for sub_slot in reversed(slots[:first_idx]):
        if sub_slot.is_end_of_slot():
            slots_n -= 1
            if slots_n == 0:
                challenge_slot = sub_slot
                break
        idx -= 1

    if slots_n > 0:
        log.error("not enough slots")
        return None

    assert challenge_slot is not None
    assert segment.cc_slot_end_info is not None
    assert segment.rc_slot_end_info is not None

    icc_iters = prev_ssi
    if segment.prev_icc_ip_iters is not None:
        icc_iters = uint64(prev_ssi - segment.prev_icc_ip_iters)
    assert challenge_slot.icc_slot_end_output
    assert segment.icc_sub_slot_hash
    icc_info = VDFInfo(segment.icc_sub_slot_hash, icc_iters, challenge_slot.icc_slot_end_output)
    cc_sub_slot = ChallengeChainSubSlot(segment.cc_slot_end_info, icc_info.get_hash(), ses_hash, new_ssi, new_diff)
    rc_sub_slot = RewardChainSubSlot(
        segment.rc_slot_end_info,
        cc_sub_slot.get_hash(),
        icc_info.get_hash(),
        constants.MIN_BLOCKS_PER_CHALLENGE_BLOCK,
    )

    log.debug(f"sub epoch start, icc info {icc_info}")
    log.debug(f"sub epoch start, cc sub slot {cc_sub_slot}")
    log.debug(f"sub epoch start, rc sub slot {rc_sub_slot}")
    return rc_sub_slot.get_hash()


def _get_curr_diff_ssi(constants: ConsensusConstants, idx, summaries):
    curr_difficulty = constants.DIFFICULTY_STARTING
    curr_ssi = constants.SUB_SLOT_ITERS_STARTING
    if idx == 0:
        # genesis
        return curr_difficulty, curr_ssi
    for ses in reversed(summaries[0 : idx - 1]):
        if ses.new_sub_slot_iters is not None:
            curr_ssi = ses.new_sub_slot_iters
            curr_difficulty = ses.new_difficulty
            break

    return curr_difficulty, curr_ssi


def _get_last_ses(
    constants: ConsensusConstants, recent_reward_chain: List[HeaderBlock]
) -> Tuple[Optional[bytes32], uint32, uint128]:
    for idx, block in enumerate(reversed(recent_reward_chain)):
        if (block.reward_chain_block.height % constants.SUB_EPOCH_BLOCKS) == 0:
            idx = len(recent_reward_chain) - 1 - idx  # reverse
            # find first block after sub slot end
            while idx < len(recent_reward_chain):
                curr = recent_reward_chain[idx]
                if len(curr.finished_sub_slots) > 0:
                    for slot in curr.finished_sub_slots:
                        if slot.challenge_chain.subepoch_summary_hash is not None:
                            return (
                                slot.challenge_chain.subepoch_summary_hash,
                                curr.reward_chain_block.height,
                                curr.reward_chain_block.weight,
                            )
                idx += 1
    return None, uint32(0), uint128(0)


def get_sp_total_iters(sp_iters: uint64, is_overflow: bool, ssi: uint64, sub_slot_data: SubSlotDataV2) -> uint128:
    assert sub_slot_data.total_iters is not None
    assert sub_slot_data.signage_point_index is not None
    assert sub_slot_data.ip_iters
    ip_iters: uint64 = sub_slot_data.ip_iters
    sp_sub_slot_total_iters = uint128(sub_slot_data.total_iters - ip_iters)
    if is_overflow:
        sp_sub_slot_total_iters = uint128(sp_sub_slot_total_iters - ssi)
    return uint128(sp_sub_slot_total_iters + sp_iters)


def validate_sub_epoch_sampling(rng: random.Random, sub_epoch_weight_list: List[uint128], weight_proof: WeightProofV2):
    total_weight = sub_epoch_weight_list[-1]
    last_l_weight = sub_epoch_weight_list[-1] - sub_epoch_weight_list[-3]
    log.debug(f"total weight {total_weight} prev weight {sub_epoch_weight_list[-2]}")
    weight_to_check = _get_weights_for_sampling(rng, total_weight, last_l_weight)
    if weight_to_check is None:
        log.error("failed getting weight list for samples")
        return False
    sampled_sub_epochs: dict[int, bool] = {}
    for idx in range(1, len(sub_epoch_weight_list)):
        if _sample_sub_epoch(sub_epoch_weight_list[idx - 1], sub_epoch_weight_list[idx], weight_to_check):
            sampled_sub_epochs[idx - 1] = True
            if len(sampled_sub_epochs) == WeightProofHandlerV2.MAX_SAMPLES:
                break
    curr_sub_epoch_n = -1
    for sub_epoch_segment in weight_proof.sub_epoch_segments:
        if curr_sub_epoch_n < sub_epoch_segment.sub_epoch_n:
            if sub_epoch_segment.sub_epoch_n in sampled_sub_epochs:
                del sampled_sub_epochs[sub_epoch_segment.sub_epoch_n]
        curr_sub_epoch_n = sub_epoch_segment.sub_epoch_n
    if len(sampled_sub_epochs) > 0:
        return False
    return True


def map_segments_by_sub_epoch(
    sub_epoch_segments: List[SubEpochChallengeSegmentV2],
) -> Dict[int, List[SubEpochChallengeSegmentV2]]:
    segments: Dict[int, List[SubEpochChallengeSegmentV2]] = {}
    curr_sub_epoch_n = -1
    for idx, segment in enumerate(sub_epoch_segments):
        if curr_sub_epoch_n < segment.sub_epoch_n:
            curr_sub_epoch_n = segment.sub_epoch_n
            segments[curr_sub_epoch_n] = []
        segments[curr_sub_epoch_n].append(segment)
    return segments
