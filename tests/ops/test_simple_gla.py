# -*- coding: utf-8 -*-

import os

import pytest
import torch
import torch.nn.functional as F

from fla.ops.simple_gla import chunk_simple_gla
from fla.ops.simple_gla.fused_recurrent import fused_recurrent_simple_gla
from fla.ops.simple_gla.parallel import parallel_simple_gla


def get_abs_err(x, y):
    return (x-y).flatten().abs().max().item()


def get_err_ratio(x, y):
    err = (x-y).flatten().square().mean().sqrt().item()
    base = (x).flatten().square().mean().sqrt().item()
    return err / base


def assert_close(prefix, ref, tri, ratio):
    msg = f"{prefix} diff: {get_abs_err(ref, tri):.6f} ratio: {get_err_ratio(ref, tri):.6f}"
    print(msg)
    assert get_err_ratio(ref, tri) < ratio, msg


@pytest.mark.parametrize("B", [1])
@pytest.mark.parametrize("H", [1])
@pytest.mark.parametrize("T", [100, 512])
@pytest.mark.parametrize("D", [100, 256])
@pytest.mark.parametrize("dtype", [torch.bfloat16])
@pytest.mark.parametrize("head_first", [True, False])
def test_chunk(
    B: int,
    H: int,
    T: int,
    D: int,
    dtype: torch.dtype,
    head_first: bool
):
    torch.manual_seed(42)
    os.environ['TRITON_F32_DEFAULT'] = 'ieee'
    # [B, H, T, D]
    if head_first:
        q = torch.randn((B, H, T, D), dtype=dtype, device='cuda').requires_grad_(True)
        k = torch.randn((B, H, T, D), dtype=dtype, device='cuda').requires_grad_(True)
        v = torch.randn((B, H, T, D), dtype=dtype, device='cuda').requires_grad_(True)
        g = torch.randn((B, H, T), dtype=dtype, device='cuda')
    else:
        q = torch.randn((B, T, H, D), dtype=dtype, device='cuda').requires_grad_(True)
        k = torch.randn((B, T, H, D), dtype=dtype, device='cuda').requires_grad_(True)
        v = torch.randn((B, T, H, D), dtype=dtype, device='cuda').requires_grad_(True)
        g = torch.randn((B, T, H), dtype=dtype, device='cuda')
    h0 = torch.rand((B, H, D, D), dtype=torch.float32, device='cuda').requires_grad_(True)
    g = F.logsigmoid(g).requires_grad_(True)
    do = torch.randn_like(v)

    ref, ref_ht = fused_recurrent_simple_gla(q, k, v, g, initial_state=h0, output_final_state=True, head_first=head_first)
    # dht not supported yet in `fused_recurrent` kernel
    ref, _ = fused_recurrent_simple_gla(q, k, v, g, initial_state=h0, output_final_state=False, head_first=head_first)
    (ref * do).sum().backward()
    ref_dq, q.grad = q.grad.clone(), None
    ref_dk, k.grad = k.grad.clone(), None
    ref_dv, v.grad = v.grad.clone(), None
    ref_dg, g.grad = g.grad.clone(), None
    ref_dh0, h0.grad = h0.grad.clone(), None

    tri, tri_ht = chunk_simple_gla(q, k, v, g, initial_state=h0, output_final_state=True, head_first=head_first)
    ((tri * do).sum()).backward()
    tri_dq, q.grad = q.grad.clone(), None
    tri_dk, k.grad = k.grad.clone(), None
    tri_dv, v.grad = v.grad.clone(), None
    tri_dg, g.grad = g.grad.clone(), None
    tri_dh0, h0.grad = h0.grad.clone(), None

    assert_close("  o", ref, tri, 0.004)
    assert_close(" ht", ref_ht, tri_ht, 0.005)
    assert_close(" dq", ref_dq, tri_dq, 0.005)
    assert_close(" dk", ref_dk, tri_dk, 0.005)
    assert_close(" dv", ref_dv, tri_dv, 0.005)
    assert_close(" dg", ref_dg, tri_dg, 0.005)
    assert_close("dh0", ref_dh0, tri_dh0, 0.005)


@pytest.mark.parametrize("N", [4])
@pytest.mark.parametrize("T", [64, 128, 200, 250, 256, 300, 400, 512, 1000, 2048])
@pytest.mark.parametrize("H", [4])
@pytest.mark.parametrize("D", [100, 256])
@pytest.mark.parametrize("dtype", [torch.bfloat16, torch.float])
def test_chunk_varlen(
    N: int,
    T: int,
    H: int,
    D: int,
    dtype: torch.dtype,
):
    torch.manual_seed(42)
    os.environ['TRITON_F32_DEFAULT'] = 'ieee'

    # randomly split the sequence into N segments
    offsets = torch.cat([
        torch.tensor([0], dtype=torch.long),
        torch.arange(16, T)[torch.randperm(T - 1)[:N-1]],
        torch.tensor([T], dtype=torch.long)
    ], 0).cuda().sort()[0]
    # seq-first required for inputs with variable lengths
    q = torch.randn((1, T, H, D), dtype=dtype, device='cuda').requires_grad_()
    k = torch.randn((1, T, H, D), dtype=dtype, device='cuda').requires_grad_()
    v = torch.randn((1, T, H, D), dtype=dtype, device='cuda').requires_grad_()
    g = F.logsigmoid(torch.randn((1, T, H), dtype=dtype, device='cuda')).requires_grad_()
    h0 = torch.randn((N, H, D, D), dtype=torch.float32, device='cuda').requires_grad_()
    do = torch.randn_like(v)

    ref, ref_ht = fused_recurrent_simple_gla(
        q=q,
        k=k,
        v=v,
        g=g,
        initial_state=h0,
        output_final_state=True,
        offsets=offsets,
        head_first=False
    )
    # dht not supported yet in `fused_recurrent` kernel
    ref, _ = fused_recurrent_simple_gla(
        q=q,
        k=k,
        v=v,
        g=g,
        initial_state=h0,
        output_final_state=False,
        offsets=offsets,
        head_first=False
    )
    ((ref * do).sum()).backward()
    ref_dq, q.grad = q.grad.clone(), None
    ref_dk, k.grad = k.grad.clone(), None
    ref_dv, v.grad = v.grad.clone(), None
    ref_dg, g.grad = g.grad.clone(), None
    ref_dh0, h0.grad = h0.grad.clone(), None

    tri, tri_ht = chunk_simple_gla(
        q=q,
        k=k,
        v=v,
        g=g,
        initial_state=h0,
        output_final_state=True,
        offsets=offsets,
        head_first=False
    )
    ((tri * do).sum()).backward()
    tri_dq, q.grad = q.grad.clone(), None
    tri_dk, k.grad = k.grad.clone(), None
    tri_dv, v.grad = v.grad.clone(), None
    tri_dg, g.grad = g.grad.clone(), None
    tri_dh0, h0.grad = h0.grad.clone(), None

    assert_close("  o", ref, tri, 0.004)
    assert_close(" ht", ref_ht, tri_ht, 0.005)
    assert_close(" dq", ref_dq, tri_dq, 0.005)
    assert_close(" dk", ref_dk, tri_dk, 0.005)
    assert_close(" dv", ref_dv, tri_dv, 0.005)
    assert_close(" dg", ref_dg, tri_dg, 0.005)
    assert_close("dh0", ref_dh0, tri_dh0, 0.005)


@pytest.mark.parametrize("B", [1])
@pytest.mark.parametrize("H", [1])
@pytest.mark.parametrize("T", [300, 512])
@pytest.mark.parametrize("D", [32, 64, 100])
@pytest.mark.parametrize("dtype", [torch.float16, torch.float32])
def test_parallel(
    B: int,
    H: int,
    T: int,
    D: int,
    dtype: torch.dtype
):
    torch.manual_seed(42)
    os.environ['TRITON_F32_DEFAULT'] = 'ieee'
    q = torch.randn((B, H, T, D), dtype=dtype, device='cuda').requires_grad_(True)
    k = torch.randn((B, H, T, D), dtype=dtype, device='cuda').requires_grad_(True)
    v = torch.randn((B, H, T, D), dtype=dtype, device='cuda').requires_grad_(True)
    h0 = torch.zeros((B, H, D, D), dtype=torch.float32, device='cuda')
    g = F.logsigmoid(torch.randn((B, H, T), dtype=dtype, device='cuda'))
    g = (g / 16).requires_grad_(True)
    do = torch.randn_like(v)

    ref, _ = fused_recurrent_simple_gla(q, k, v, g, initial_state=h0)
    ref.backward(do)
    ref_dq, q.grad = q.grad.clone(), None
    ref_dk, k.grad = k.grad.clone(), None
    ref_dv, v.grad = v.grad.clone(), None
    ref_dg, g.grad = g.grad.clone(), None

    tri, _ = parallel_simple_gla(q, k, v, g)
    tri.backward(do)
    tri_dq, q.grad = q.grad.clone(), None
    tri_dk, k.grad = k.grad.clone(), None
    tri_dv, v.grad = v.grad.clone(), None
    tri_dg, g.grad = g.grad.clone(), None

    assert_close(" o", ref, tri, 0.005)
    assert_close("dq", ref_dq, tri_dq, 0.005)
    assert_close("dk", ref_dk, tri_dk, 0.005)
    assert_close("dv", ref_dv, tri_dv, 0.005)
    assert_close("dg", ref_dg, tri_dg, 0.005)


@pytest.mark.parametrize("vary_A", [True, False])
@pytest.mark.parametrize("dtype", [torch.float, torch.bfloat16])
def test_simple_gla_to_mamba2(vary_A, dtype):
    r"""
    Map Mamba-2's `mamba_chunk_scan_combined` kernel to FLA's `simple_gla` kernel

    Dependencies:
    $ pip install mamba-ssm==2.2.2 triton==2.3.1

    Reference: `ssd_minimal_discrete` and `test_correctness` in mamba repository:
    https://github.com/state-spaces/mamba/blob/v2.2.2/mamba_ssm/modules/ssd_minimal.py#L82
    """
    from mamba_ssm.modules.ssd_minimal import ssd_minimal_discrete
    from mamba_ssm.ops.triton.ssd_combined import mamba_chunk_scan_combined
    torch.manual_seed(42)

    # Dimensions, Denoted (B, T, Q, D, P) in Mamba2 paper
    batch, seq_len, chunk_size, dim, headdim = 2, 512, 8, 64, 16
    n_heads = dim // headdim  # (H) in the paper
    ngroups = n_heads  # (G) in the paper; NOTE: do not use group-query here
    dstate = 64  # (N) in the paper
    device = "cuda"
    atol = 5e-4 if dtype == torch.float else 1e-2

    x = 0.1 * torch.randn(batch, seq_len, n_heads, headdim, dtype=dtype, device=device)
    dt = torch.ones(batch, seq_len, n_heads, dtype=dtype, device=device)  # dt=1 can be ignored

    if vary_A:
        A = -0.1 * torch.rand(1, seq_len, n_heads, dtype=dtype, device=device)
    else:  # constant A for all position
        A = -0.1 * torch.rand(n_heads, dtype=dtype, device=device)

    B = 0.1 * torch.randn(batch, seq_len, ngroups, dstate, dtype=dtype, device=device)
    C = 0.1 * torch.randn(batch, seq_len, ngroups, dstate, dtype=dtype, device=device)

    y_ssd, final_ssd = ssd_minimal_discrete(x * dt.unsqueeze(-1), A * dt, B, C, chunk_size)

    if not vary_A:
        # NOTE: fused kernel does not support varying A with time
        y_fuse, final_fuse = mamba_chunk_scan_combined(x, dt, A, B, C, chunk_size, D=None, return_final_states=True)
        assert y_ssd.allclose(y_fuse, 0, atol), f"y diff: {torch.abs(y_ssd - y_fuse).max()}"
        # fused kernel upcasts state to float32
        # https://github.com/state-spaces/mamba/blob/v2.2.2/mamba_ssm/ops/triton/ssd_combined.py#L650
        final_fuse = final_fuse.to(dtype)
        assert final_ssd.allclose(final_fuse, 0, atol), f"final diff: {torch.abs(final_ssd - final_fuse).max()}"

    # mapping inputs Mamba2 -> FLA
    # C, B, X: [batch, seq, head, hidden] -> [batch, head, seq, hidden]
    # g: [batch, seq, head] -> [batch, head, seq]
    q = C.transpose(1, 2)
    k = B.transpose(1, 2)
    v = x.transpose(1, 2)
    g = (A * dt).transpose(1, 2)

    # mapping outputs Mamba2 -> FLA
    y_rearrange = y_ssd.transpose(1, 2)
    final_rearrange = final_ssd.transpose(2, 3)

    # comparing output results between FLA kernel and Mamba2 kernel
    outputs_gla_fuse, final_gla_fuse = chunk_simple_gla(q, k, v, g, scale=1.0, output_final_state=True)
    assert y_rearrange.allclose(outputs_gla_fuse, 0, atol), f"y diff: {torch.abs(y_rearrange - outputs_gla_fuse).max()}"
    final_gla_fuse = final_gla_fuse.to(dtype)  # states hard-coded to float32 in FLA kernel
    assert final_rearrange.allclose(final_gla_fuse, 0, atol), f"final diff: {torch.abs(final_ssd - final_gla_fuse).max()}"
