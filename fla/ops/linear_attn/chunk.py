# -*- coding: utf-8 -*-
# Copyright (c) 2023, Yu Zhang, Songlin Yang

from typing import Optional, Tuple

import torch
import triton
import triton.language as tl

from fla.ops.linear_attn.utils import normalize_output
from fla.utils import autocast_custom_bwd, autocast_custom_fwd, contiguous


@triton.jit
def chunk_linear_attn_fwd_kernel_h(
    k,
    v,
    h,
    h0,
    ht,
    s_k_h,
    s_k_t,
    s_k_d,
    s_v_h,
    s_v_t,
    s_v_d,
    s_h_h,
    s_h_t,
    T: tl.constexpr,
    K: tl.constexpr,
    V: tl.constexpr,
    BT: tl.constexpr,
    BK: tl.constexpr,
    BV: tl.constexpr,
    NT: tl.constexpr,
    USE_INITIAL_STATE: tl.constexpr,
    STORE_FINAL_STATE: tl.constexpr
):
    i_k, i_v, i_bh = tl.program_id(0), tl.program_id(1), tl.program_id(2)

    # [BK, BV]
    b_h = tl.zeros([BK, BV], dtype=tl.float32)

    if USE_INITIAL_STATE:
        p_h0 = tl.make_block_ptr(h0 + i_bh * K * V, (K, V), (V, 1), (i_k * BK, i_v * BV), (BK, BV), (1, 0))
        b_h = tl.load(p_h0, boundary_check=(0, 1)).to(tl.float32)

    for i_t in range(NT):
        p_k = tl.make_block_ptr(k + i_bh * s_k_h, (K, T), (s_k_d, s_k_t), (i_k * BK, i_t * BT), (BK, BT), (0, 1))
        p_v = tl.make_block_ptr(v + i_bh * s_v_h, (T, V), (s_v_t, s_v_d), (i_t * BT, i_v * BV), (BT, BV), (1, 0))
        p_h = tl.make_block_ptr(h + i_bh * s_h_h + i_t * K * V, (K, V), (s_h_t, 1), (i_k * BK, i_v * BV), (BK, BV), (1, 0))

        tl.store(p_h, b_h.to(p_h.dtype.element_ty), boundary_check=(0, 1))
        # [BK, BT]
        b_k = tl.load(p_k, boundary_check=(0, 1))
        # [BT, BV]
        b_v = tl.load(p_v, boundary_check=(0, 1))
        # [BK, BV]
        b_h += tl.dot(b_k, b_v, allow_tf32=False)

    if STORE_FINAL_STATE:
        p_ht = tl.make_block_ptr(ht + i_bh * K * V, (K, V), (V, 1), (i_k * BK, i_v * BV), (BK, BV), (1, 0))
        tl.store(p_ht, b_h.to(p_ht.dtype.element_ty), boundary_check=(0, 1))


@triton.jit
def chunk_linear_attn_fwd_kernel_o(
    q,
    k,
    v,
    h,
    o,
    s_k_h,
    s_k_t,
    s_k_d,
    s_v_h,
    s_v_t,
    s_v_d,
    s_h_h,
    s_h_t,
    scale,
    T: tl.constexpr,
    K: tl.constexpr,
    V: tl.constexpr,
    BT: tl.constexpr,
    BK: tl.constexpr,
    BV: tl.constexpr
):
    i_v, i_t, i_bh = tl.program_id(0), tl.program_id(1), tl.program_id(2)

    o_i = tl.arange(0, BT)
    m_s = o_i[:, None] >= o_i[None, :]

    b_o = tl.zeros([BT, BV], dtype=tl.float32)
    b_s = tl.zeros([BT, BT], dtype=tl.float32)
    for i_k in range(tl.cdiv(K, BK)):
        p_q = tl.make_block_ptr(q + i_bh * s_k_h, (T, K), (s_k_t, s_k_d), (i_t * BT, i_k * BK), (BT, BK), (1, 0))
        p_k = tl.make_block_ptr(k + i_bh * s_k_h, (K, T), (s_k_d, s_k_t), (i_k * BK, i_t * BT), (BK, BT), (0, 1))
        p_h = tl.make_block_ptr(h + i_bh * s_h_h + i_t * K * V, (K, V), (s_h_t, 1), (i_k * BK, i_v * BV), (BK, BV), (1, 0))
        # [BT, BK]
        b_q = tl.load(p_q, boundary_check=(0, 1))
        # [BK, BT]
        b_k = tl.load(p_k, boundary_check=(0, 1))
        # [BK, BV]
        b_h = tl.load(p_h, boundary_check=(0, 1))
        b_o += tl.dot(b_q, b_h, allow_tf32=False)
        b_s += tl.dot(b_q, b_k, allow_tf32=False)
    b_s = tl.where(m_s, b_s, 0)

    p_v = tl.make_block_ptr(v + i_bh * s_v_h, (T, V), (s_v_t, s_v_d), (i_t * BT, i_v * BV), (BT, BV), (1, 0))
    p_o = tl.make_block_ptr(o + i_bh * s_v_h, (T, V), (s_v_t, s_v_d), (i_t * BT, i_v * BV), (BT, BV), (1, 0))

    b_v = tl.load(p_v, boundary_check=(0, 1))
    b_o = (b_o + tl.dot(b_s.to(b_v.dtype), b_v, allow_tf32=False)) * scale

    tl.store(p_o, b_o.to(p_o.dtype.element_ty), boundary_check=(0, 1))


@triton.jit
def chunk_linear_attn_bwd_kernel_dh(
    q,
    do,
    dh,
    s_k_h,
    s_k_t,
    s_k_d,
    s_v_h,
    s_v_t,
    s_v_d,
    s_h_h,
    s_h_t,
    scale,
    T: tl.constexpr,
    K: tl.constexpr,
    V: tl.constexpr,
    BT: tl.constexpr,
    BK: tl.constexpr,
    BV: tl.constexpr,
    NT: tl.constexpr
):
    i_k, i_v, i_bh = tl.program_id(0), tl.program_id(1), tl.program_id(2)

    # [BK, BV]
    b_dh = tl.zeros([BK, BV], dtype=tl.float32)
    for i_t in range(NT - 1, -1, -1):
        p_q = tl.make_block_ptr(q + i_bh * s_k_h, (K, T), (s_k_d, s_k_t), (i_k * BK, i_t * BT), (BK, BT), (0, 1))
        p_do = tl.make_block_ptr(do + i_bh * s_v_h, (T, V), (s_v_t, s_v_d), (i_t * BT, i_v * BV), (BT, BV), (1, 0))
        p_dh = tl.make_block_ptr(dh + i_bh * s_h_h + i_t * K * V, (K, V), (s_h_t, 1), (i_k * BK, i_v * BV), (BK, BV), (1, 0))

        tl.store(p_dh, b_dh.to(p_dh.dtype.element_ty), boundary_check=(0, 1))
        # [BK, BT]
        b_q = tl.load(p_q, boundary_check=(0, 1))
        b_q = (b_q * scale).to(b_q.dtype)
        # [BT, V]
        b_do = tl.load(p_do, boundary_check=(0, 1))
        # [BK, BV]
        b_dh += tl.dot(b_q, b_do.to(b_q.dtype), allow_tf32=False)


@triton.jit
def chunk_linear_attn_bwd_kernel_dqkv(
    q,
    k,
    v,
    h,
    do,
    dh,
    dq,
    dk,
    dv,
    s_k_h,
    s_k_t,
    s_k_d,
    s_v_h,
    s_v_t,
    s_v_d,
    s_h_h,
    s_h_t,
    scale,
    T: tl.constexpr,
    K: tl.constexpr,
    V: tl.constexpr,
    BT: tl.constexpr,
    BK: tl.constexpr,
    BV: tl.constexpr,
    NT: tl.constexpr
):
    i_k, i_t, i_bh = tl.program_id(0), tl.program_id(1), tl.program_id(2)
    n_bh = tl.num_programs(2)
    o_i = tl.arange(0, BT)

    p_q = tl.make_block_ptr(q + i_bh * s_k_h, (K, T), (s_k_d, s_k_t), (i_k * BK, i_t * BT), (BK, BT), (0, 1))
    p_k = tl.make_block_ptr(k + i_bh * s_k_h, (T, K), (s_k_t, s_k_d), (i_t * BT, i_k * BK), (BT, BK), (1, 0))

    b_q = tl.load(p_q, boundary_check=(0, 1))
    b_k = tl.load(p_k, boundary_check=(0, 1))
    b_s = tl.dot(b_k, b_q, allow_tf32=False) * scale
    b_s = tl.where(o_i[:, None] <= o_i[None, :], b_s, 0)

    b_dq = tl.zeros([BT, BK], dtype=tl.float32)
    b_dk = tl.zeros([BT, BK], dtype=tl.float32)
    b_ds = tl.zeros([BT, BT], dtype=tl.float32)
    for i_v in range(tl.cdiv(V, BV)):
        p_v = tl.make_block_ptr(v + i_bh * s_v_h, (T, V), (s_v_t, s_v_d), (i_t * BT, i_v * BV), (BT, BV), (1, 0))
        p_h = tl.make_block_ptr(h + i_bh * s_h_h, (V, NT * K), (1, s_h_t), (i_v * BV, i_t * K + i_k * BK), (BV, BK), (0, 1))
        p_do = tl.make_block_ptr(do + i_bh * s_v_h, (T, V), (s_v_t, s_v_d), (i_t * BT, i_v * BV), (BT, BV), (1, 0))
        p_dh = tl.make_block_ptr(dh + i_bh * s_h_h, (NT * K, V), (s_h_t, 1), (i_t * K + i_k * BK, i_v * BV), (BK, BV), (1, 0))
        p_dv = tl.make_block_ptr(dv + (i_k*n_bh+i_bh)*s_v_h, (T, V), (s_v_t, s_v_d), (i_t * BT, i_v * BV), (BT, BV), (1, 0))
        # [BT, BV]
        b_v = tl.load(p_v, boundary_check=(0, 1))
        b_do = tl.load(p_do, boundary_check=(0, 1))
        # [BV, BK]
        b_h = tl.load(p_h, boundary_check=(0, 1))
        # [BK, BV]
        b_dh = tl.load(p_dh, boundary_check=(0, 1))
        # [BT, BT]
        b_ds += tl.dot(b_do, tl.trans(b_v), allow_tf32=False)
        # [BT, BK]
        b_dq += tl.dot(b_do, b_h, allow_tf32=False) * scale
        b_dk += tl.dot(b_v, tl.trans(b_dh), allow_tf32=False)
        # [BT, BV]
        b_dv = tl.dot(b_k, b_dh, allow_tf32=False) + tl.dot(b_s.to(b_q.dtype), b_do, allow_tf32=False)
        tl.store(p_dv, b_dv.to(p_dv.dtype.element_ty), boundary_check=(0, 1))
    # [BT, BT]
    b_ds = tl.where(o_i[:, None] >= o_i[None, :], b_ds * scale, 0).to(b_q.dtype)
    # [BT, BK]
    b_dq += tl.dot(b_ds, b_k, allow_tf32=False)
    b_dk += tl.trans(tl.dot(b_q, b_ds, allow_tf32=False))

    p_dq = tl.make_block_ptr(dq + i_bh * s_k_h, (T, K), (s_k_t, s_k_d), (i_t * BT, i_k * BK), (BT, BK), (1, 0))
    p_dk = tl.make_block_ptr(dk + i_bh * s_k_h, (T, K), (s_k_t, s_k_d), (i_t * BT, i_k * BK), (BT, BK), (1, 0))
    tl.store(p_dq, b_dq.to(p_dq.dtype.element_ty), boundary_check=(0, 1))
    tl.store(p_dk, b_dk.to(p_dk.dtype.element_ty), boundary_check=(0, 1))


class ChunkLinearAttentionFunction(torch.autograd.Function):

    @staticmethod
    @contiguous
    @autocast_custom_fwd
    def forward(ctx, q, k, v, scale, initial_state, output_final_state):
        B, H, T, K, V = *q.shape, v.shape[-1]
        BT = 64
        BK, BV = min(64, triton.next_power_of_2(K)), min(64, triton.next_power_of_2(V))
        NT, NK, NV = triton.cdiv(T, BT), triton.cdiv(K, BK), triton.cdiv(V, BV)
        num_stages = 1
        num_warps = 4 if BK == 64 else 2
        ctx.scale = scale

        final_state = None
        if output_final_state:
            final_state = q.new_empty(B, H, K, V, dtype=torch.float32, requires_grad=False)

        h = q.new_empty(B, H, NT * K, V)
        grid = (NK, NV, B * H)
        chunk_linear_attn_fwd_kernel_h[grid](
            k, v, h, initial_state, final_state,
            q.stride(1), q.stride(2), q.stride(3),
            v.stride(1), v.stride(2), v.stride(3),
            h.stride(1), h.stride(2),
            T=T, K=K, V=V, BT=BT, BK=BK, BV=BV, NT=NT,
            USE_INITIAL_STATE=initial_state is not None,
            STORE_FINAL_STATE=output_final_state,
            num_warps=num_warps,
            num_stages=num_stages
        )
        grid = (NV, NT, B * H)
        o = torch.empty_like(v)
        chunk_linear_attn_fwd_kernel_o[grid](
            q, k, v, h, o,
            q.stride(1), q.stride(2), q.stride(3),
            v.stride(1), v.stride(2), v.stride(3),
            h.stride(1), h.stride(2),
            scale,
            T=T, K=K, V=V, BT=BT, BK=BK, BV=BV,
            num_warps=num_warps,
            num_stages=num_stages
        )
        ctx.save_for_backward(q, k, v, h)
        return o.to(q.dtype), final_state

    @staticmethod
    @contiguous
    @autocast_custom_bwd
    def backward(ctx, do, dht=None):
        q, k, v, h = ctx.saved_tensors

        B, H, T, K, V = *q.shape, v.shape[-1]
        BT = 64
        BK, BV = min(64, triton.next_power_of_2(K)), min(32 if q.dtype == torch.float32 else 64, triton.next_power_of_2(V))
        NT, NK, NV = triton.cdiv(T, BT), triton.cdiv(K, BK), triton.cdiv(V, BV)
        num_stages = 1
        num_warps = 4 if BK == 64 else 2
        scale = ctx.scale

        dh = q.new_empty(B, H, NT * K, V)
        grid = (NK, NV, B * H)
        chunk_linear_attn_bwd_kernel_dh[grid](
            q, do, dh,
            q.stride(1), q.stride(2), q.stride(3),
            v.stride(1), v.stride(2), v.stride(3),
            dh.stride(1), dh.stride(2),
            scale,
            T=T, K=K, V=V, BT=BT, BK=BK, BV=BV, NT=NT,
            num_warps=num_warps,
            num_stages=num_stages
        )

        grid = (NK, NT, B * H)
        dq = torch.empty_like(q)
        dk = torch.empty_like(k)
        dv = v.new_empty(NK, *v.shape)
        num_stages = 1
        num_warps = 4 if BK == 64 else 2
        chunk_linear_attn_bwd_kernel_dqkv[grid](
            q, k, v, h, do, dh, dq, dk, dv,
            q.stride(1), q.stride(2), q.stride(3),
            v.stride(1), v.stride(2), v.stride(3),
            dh.stride(1), dh.stride(2),
            scale,
            T=T, K=K, V=V, BT=BT, BK=BK, BV=BV, NT=NT,
            num_warps=num_warps,
            num_stages=num_stages
        )
        dv = dv.sum(0)
        return dq.to(q.dtype), dk.to(k.dtype), dv.to(v.dtype), None, None, None


def chunk_linear_attn(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    scale: Optional[float] = None,
    initial_state: torch.Tensor = None,
    output_final_state: bool = False,
    normalize: bool = True,
    head_first: bool = True
) -> Tuple[torch.Tensor, torch.Tensor]:
    r"""
    Args:
        q (torch.Tensor):
            queries of shape `[B, H, T, K]` if `head_first=True` else `[B, T, H, K]`
        k (torch.Tensor):
            keys of shape `[B, H, T, K]` if `head_first=True` else `[B, T, H, K]`
        v (torch.Tensor):
            values of shape `[B, H, T, V]` if `head_first=True` else `[B, T, H, V]`
        scale (Optional[int]):
            Scale factor for the linear attention scores.
            If not provided, it will default to `1 / sqrt(K)`. Default: `None`.
        initial_state (Optional[torch.Tensor]):
            Initial state of shape `[B, H, K, V]`. Default: `None`.
        output_final_state (Optional[bool]):
            Whether to output the final state of shape `[B, H, K, V]`. Default: `False`.
        normalize (bool):
            Whether to normalize the output. Default: `True`.
        head_first (Optional[bool]):
            Whether the inputs are in the head-first format. Default: `True`.

    Returns:
        o (torch.Tensor):
            Outputs of shape `[B, H, T, V]` if `head_first=True` else `[B, T, H, V]`
        final_state (torch.Tensor):
            Final state of shape `[B, H, K, V]` if `output_final_state=True` else `None`
    """
    if scale is None:
        scale = q.shape[-1] ** -0.5
    if not head_first:
        q, k, v = map(lambda x: x.transpose(1, 2), (q, k, v))
    o, final_state = ChunkLinearAttentionFunction.apply(q, k, v, scale, initial_state, output_final_state)
    if normalize:
        o = normalize_output(q * scale, k, o)
    if not head_first:
        o = o.transpose(1, 2)
    return o, final_state
