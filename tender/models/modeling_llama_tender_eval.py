# coding=utf-8
# Copyright 2022 EleutherAI and the HuggingFace Inc. team. All rights reserved.
#
# This code is based on EleutherAI's GPT-NeoX library and the GPT-NeoX
# and OPT implementations in this library. It has been modified from its
# original forms to accommodate minor architectural differences compared
# to GPT-NeoX and OPT used by the Meta AI team that trained the model.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" PyTorch LLaMA model."""
import math
from typing import List, Optional, Tuple, Union

import torch
import torch.nn.functional as F
import torch.utils.checkpoint
from torch import nn
from torch.nn import BCEWithLogitsLoss, CrossEntropyLoss, MSELoss

from transformers.activations import ACT2FN
from transformers.modeling_outputs import BaseModelOutputWithPast, CausalLMOutputWithPast, SequenceClassifierOutputWithPast
from transformers.modeling_utils import PreTrainedModel
from transformers.pytorch_utils import ALL_LAYERNORM_LAYERS
from transformers.utils import (
    add_start_docstrings,
    add_start_docstrings_to_model_forward,
    logging,
    replace_return_docstrings,
)
from transformers.models.llama.configuration_llama import LlamaConfig

# if is_flash_attn_available():
#     from flash_attn import flash_attn_func, flash_attn_varlen_func
#     from flash_attn.bert_padding import index_first_axis, pad_input, unpad_input  # noqa


logger = logging.get_logger(__name__)

_CONFIG_FOR_DOC = "LlamaConfig"

# linear-symmetric integer quantization (round to nearest, ties to even)
def sym_quant(fp, scale, q_bits):
    fp = fp.to(torch.float32)
    scale = scale.to(torch.float32) # in case of overflow / underflow

    if(type(scale) == torch.Tensor):
        assert torch.sum(scale == 0.0).item() == 0.0, "Zero is given as a scale factor"
        scale = torch.clamp(scale.abs(), min = 1e-9)
    else:
        assert scale != 0.0, "Zero is given as a scale factor"

    q_max = (2 ** (q_bits - 1) - 1)
    q_int = torch.round(fp / scale).clamp(min = -(q_max + 1), max = q_max)
    q_int = q_int

    return q_int

def sym_dequant(q_int, scale, dtype):

    fp = q_int * scale
    fp = fp.to(dtype)

    return fp

def quant_bfloat(t):
    assert t.dtype == torch.float32 # should not convert between bf16 and fp16

    import mx
    specs = {"bfloat16": 16, "round" : "even"}
    mx_specs = mx.finalize_mx_specs(specs)
    
    return mx.quantize_bfloat(t, mx_specs)

def _get_unpad_data(padding_mask):
    seqlens_in_batch = padding_mask.sum(dim=-1, dtype=torch.int32)
    indices = torch.nonzero(padding_mask.flatten(), as_tuple=False).flatten()
    max_seqlen_in_batch = seqlens_in_batch.max().item()
    cu_seqlens = F.pad(torch.cumsum(seqlens_in_batch, dim=0, dtype=torch.torch.int32), (1, 0))
    return (
        indices,
        cu_seqlens,
        max_seqlen_in_batch,
    )


# Copied from transformers.models.bart.modeling_bart._make_causal_mask
def _make_causal_mask(
    input_ids_shape: torch.Size, dtype: torch.dtype, device: torch.device, past_key_values_length: int = 0
):
    """
    Make causal mask used for bi-directional self-attention.
    """
    bsz, tgt_len = input_ids_shape
    mask = torch.full((tgt_len, tgt_len), torch.finfo(dtype).min, device=device)
    mask_cond = torch.arange(mask.size(-1), device=device)
    mask.masked_fill_(mask_cond < (mask_cond + 1).view(mask.size(-1), 1), 0)
    mask = mask.to(dtype)

    if past_key_values_length > 0:
        mask = torch.cat([torch.zeros(tgt_len, past_key_values_length, dtype=dtype, device=device), mask], dim=-1)
    return mask[None, None, :, :].expand(bsz, 1, tgt_len, tgt_len + past_key_values_length)


# Copied from transformers.models.bart.modeling_bart._expand_mask
def _expand_mask(mask: torch.Tensor, dtype: torch.dtype, tgt_len: Optional[int] = None):
    """
    Expands attention_mask from `[bsz, seq_len]` to `[bsz, 1, tgt_seq_len, src_seq_len]`.
    """
    bsz, src_len = mask.size()
    tgt_len = tgt_len if tgt_len is not None else src_len

    expanded_mask = mask[:, None, None, :].expand(bsz, 1, tgt_len, src_len).to(dtype)

    inverted_mask = 1.0 - expanded_mask

    return inverted_mask.masked_fill(inverted_mask.to(torch.bool), torch.finfo(dtype).min)


class LlamaRMSNorm(nn.Module):
    def __init__(self, hidden_size, eps=1e-6):
        """
        LlamaRMSNorm is equivalent to T5LayerNorm
        """
        super().__init__()
        self.weight = nn.Parameter(torch.ones(hidden_size))
        self.variance_epsilon = eps

    def forward(self, hidden_states):
        input_dtype = hidden_states.dtype
        hidden_states = hidden_states.to(torch.float32)
        variance = hidden_states.pow(2).mean(-1, keepdim=True)
        hidden_states = hidden_states * torch.rsqrt(variance + self.variance_epsilon)
        return self.weight * hidden_states.to(input_dtype)


ALL_LAYERNORM_LAYERS.append(LlamaRMSNorm)


class LlamaRotaryEmbedding(nn.Module):
    def __init__(self, dim, max_position_embeddings=2048, base=10000, device=None):
        super().__init__()

        self.dim = dim
        self.max_position_embeddings = max_position_embeddings
        self.base = base
        inv_freq = 1.0 / (self.base ** (torch.arange(0, self.dim, 2).float().to(device) / self.dim))
        self.register_buffer("inv_freq", inv_freq, persistent=False)

        # Build here to make `torch.jit.trace` work.
        self._set_cos_sin_cache(
            seq_len=max_position_embeddings, device=self.inv_freq.device, dtype=torch.get_default_dtype()
        )

    def _set_cos_sin_cache(self, seq_len, device, dtype):
        self.max_seq_len_cached = seq_len
        t = torch.arange(self.max_seq_len_cached, device=device, dtype=self.inv_freq.dtype)

        freqs = torch.einsum("i,j->ij", t, self.inv_freq)
        # Different from paper, but it uses a different permutation in order to obtain the same calculation
        emb = torch.cat((freqs, freqs), dim=-1)
        self.register_buffer("cos_cached", emb.cos()[None, None, :, :].to(dtype), persistent=False)
        self.register_buffer("sin_cached", emb.sin()[None, None, :, :].to(dtype), persistent=False)

    def forward(self, x, seq_len=None):
        # x: [bs, num_attention_heads, seq_len, head_size]
        if seq_len > self.max_seq_len_cached:
            self._set_cos_sin_cache(seq_len=seq_len, device=x.device, dtype=x.dtype)

        return (
            self.cos_cached[:, :, :seq_len, ...].to(dtype=x.dtype),
            self.sin_cached[:, :, :seq_len, ...].to(dtype=x.dtype),
        )


class LlamaLinearScalingRotaryEmbedding(LlamaRotaryEmbedding):
    """LlamaRotaryEmbedding extended with linear scaling. Credits to the Reddit user /u/kaiokendev"""

    def __init__(self, dim, max_position_embeddings=2048, base=10000, device=None, scaling_factor=1.0):
        self.scaling_factor = scaling_factor
        super().__init__(dim, max_position_embeddings, base, device)

    def _set_cos_sin_cache(self, seq_len, device, dtype):
        self.max_seq_len_cached = seq_len
        t = torch.arange(self.max_seq_len_cached, device=device, dtype=self.inv_freq.dtype)
        t = t / self.scaling_factor

        freqs = torch.einsum("i,j->ij", t, self.inv_freq)
        # Different from paper, but it uses a different permutation in order to obtain the same calculation
        emb = torch.cat((freqs, freqs), dim=-1)
        self.register_buffer("cos_cached", emb.cos()[None, None, :, :].to(dtype), persistent=False)
        self.register_buffer("sin_cached", emb.sin()[None, None, :, :].to(dtype), persistent=False)


class LlamaDynamicNTKScalingRotaryEmbedding(LlamaRotaryEmbedding):
    """LlamaRotaryEmbedding extended with Dynamic NTK scaling. Credits to the Reddit users /u/bloc97 and /u/emozilla"""

    def __init__(self, dim, max_position_embeddings=2048, base=10000, device=None, scaling_factor=1.0):
        self.scaling_factor = scaling_factor
        super().__init__(dim, max_position_embeddings, base, device)

    def _set_cos_sin_cache(self, seq_len, device, dtype):
        self.max_seq_len_cached = seq_len

        if seq_len > self.max_position_embeddings:
            base = self.base * (
                (self.scaling_factor * seq_len / self.max_position_embeddings) - (self.scaling_factor - 1)
            ) ** (self.dim / (self.dim - 2))
            inv_freq = 1.0 / (base ** (torch.arange(0, self.dim, 2).float().to(device) / self.dim))
            self.register_buffer("inv_freq", inv_freq, persistent=False)

        t = torch.arange(self.max_seq_len_cached, device=device, dtype=self.inv_freq.dtype)

        freqs = torch.einsum("i,j->ij", t, self.inv_freq)
        # Different from paper, but it uses a different permutation in order to obtain the same calculation
        emb = torch.cat((freqs, freqs), dim=-1)
        self.register_buffer("cos_cached", emb.cos()[None, None, :, :].to(dtype), persistent=False)
        self.register_buffer("sin_cached", emb.sin()[None, None, :, :].to(dtype), persistent=False)


def rotate_half(x):
    """Rotates half the hidden dims of the input."""
    x1 = x[..., : x.shape[-1] // 2]
    x2 = x[..., x.shape[-1] // 2 :]
    return torch.cat((-x2, x1), dim=-1)


def apply_rotary_pos_emb(q, k, cos, sin, position_ids):
    # The first two dimensions of cos and sin are always 1, so we can `squeeze` them.
    cos = cos.squeeze(1).squeeze(0)  # [seq_len, dim]
    sin = sin.squeeze(1).squeeze(0)  # [seq_len, dim]
    cos = cos[position_ids].unsqueeze(1)  # [bs, 1, seq_len, dim]
    sin = sin[position_ids].unsqueeze(1)  # [bs, 1, seq_len, dim]

    q_embed = (q * cos) + (rotate_half(q) * sin)
    k_embed = (k * cos) + (rotate_half(k) * sin)

    return q_embed, k_embed


class LlamaMLP(nn.Module):
    def __init__(self, config, layer_idx):
        super().__init__()
        self.config = config
        self.hidden_size = config.hidden_size
        self.intermediate_size = config.intermediate_size
        self.gate_proj = nn.Linear(self.hidden_size, self.intermediate_size, bias=False)
        self.up_proj = nn.Linear(self.hidden_size, self.intermediate_size, bias=False)
        self.down_proj = nn.Linear(self.intermediate_size, self.hidden_size, bias=False)
        self.act_fn = ACT2FN[config.hidden_act]
        self.embed_dim = config.hidden_size 
        self.ffn_dim = config.intermediate_size

        self.layer_idx = layer_idx
        
        # self.quant_out_bf16 = False
        # self.q_bits = 4
        # self.decomp_factor = 8
        # self.chunk_size = 256

        # calibrated metadata
        # self.h_ch_bias_cal = None
        # self.h_ch_bias = None

        # self.fc1_tmax_cal = None
        # self.fc2_tmax_cal = None

        # self.fc1_tmax = None
        # self.fc2_tmax = None

        # self.fc1_cmax_cal = None
        # self.fc2_cmax_cal = None

        # self.fc1_group_index = None
        # self.fc2_group_index = None

        
    def forward(self, x):
        if self.config.pretraining_tp > 1:
            slice = self.intermediate_size // self.config.pretraining_tp
            gate_proj_slices = self.gate_proj.weight.split(slice, dim=0)
            up_proj_slices = self.up_proj.weight.split(slice, dim=0)
            down_proj_slices = self.down_proj.weight.split(slice, dim=1)

            gate_proj = torch.cat(
                [F.linear(x, gate_proj_slices[i]) for i in range(self.config.pretraining_tp)], dim=-1
            )
            up_proj = torch.cat([F.linear(x, up_proj_slices[i]) for i in range(self.config.pretraining_tp)], dim=-1)

            intermediate_states = (self.act_fn(gate_proj) * up_proj).split(slice, dim=2)
            down_proj = [
                F.linear(intermediate_states[i], down_proj_slices[i]) for i in range(self.config.pretraining_tp)
            ]
            down_proj = sum(down_proj)
        else:
            down_proj = self.down_proj(self.act_fn(self.gate_proj(x)) * self.up_proj(x))

        return down_proj

    # def forward(self, x):

    #     # Weight Quant ================================
    #     # dtype = x.dtype
    #     # gate_weight = self.gate_proj.weight.data
    #     # up_weight = self.up_proj.weight.data
    #     # down_weight = self.down_proj.weight.data

    #     # q_max = (2**(self.q_bits-1))-1

    #     # scale = (torch.max(torch.abs(gate_weight.clone()), dim=-1, keepdim = True)[0]) / q_max
    #     # Wg = sym_dequant(sym_quant(gate_weight.clone(), scale, self.q_bits), scale, dtype)
    #     # Wg = Wg.transpose(-1,-2)

    #     # scale = (torch.max(torch.abs(up_weight.clone()), dim = -1, keepdim = True)[0]) / q_max
    #     # Wu = sym_dequant(sym_quant(up_weight.clone(), scale, self.q_bits), scale, dtype)
    #     # Wu = Wu.transpose(-1,-2)

    #     # scale = (torch.max(torch.abs(down_weight.clone()), dim = -1, keepdim = True)[0]) / q_max
    #     # Wd = sym_dequant(sym_quant(down_weight.clone(), scale, self.q_bits), scale, dtype)
    #     # Wd = Wd.transpose(-1,-2)
    #     #==============================================

    #     if self.config.pretraining_tp > 1:
    #         slice = self.intermediate_size // self.config.pretraining_tp
    #         gate_proj_slices = self.gate_proj.weight.split(slice, dim=0)
    #         up_proj_slices = self.up_proj.weight.split(slice, dim=0)
    #         down_proj_slices = self.down_proj.weight.split(slice, dim=1)

    #         gate_proj = torch.cat(
    #             [F.linear(x, gate_proj_slices[i]) for i in range(self.config.pretraining_tp)], dim=-1
    #         )
    #         up_proj = torch.cat([F.linear(x, up_proj_slices[i]) for i in range(self.config.pretraining_tp)], dim=-1)

    #         intermediate_states = (self.act_fn(gate_proj) * up_proj).split(slice, dim=2)
    #         down_proj = [
    #             F.linear(intermediate_states[i], down_proj_slices[i]) for i in range(self.config.pretraining_tp)
    #         ]
    #         down_proj = sum(down_proj)
    #     else:

    #         bsz, orig_tgt_len, hidden_dim = x.shape
    #         hidden_states = x.view(orig_tgt_len * bsz, hidden_dim)
    #         tgt_len, hidden_dim = hidden_states.size()
    #         data_type = hidden_states.dtype
    #         zero = torch.zeros((1,1), dtype = hidden_states.dtype, device = hidden_states.device)
    #         decomp_factor = self.decomp_factor

    #         # gate_out, up_out = self.gate_proj(x), self.up_proj(x)
    #         # Gate & Up proj ==============================
    #         chunks = int(math.ceil(tgt_len/self.chunk_size))
    #         assert(chunks != 0)

    #         padded_rows = chunks * self.chunk_size
    #         pad_num = padded_rows - tgt_len
            
    #         if pad_num > 0:
    #             padding = torch.zeros((pad_num, hidden_dim), dtype = hidden_states.dtype, device = hidden_states.device) 
    #             hidden_chunks = torch.cat((hidden_states, padding),dim=0)
    #         else:
    #             hidden_chunks = hidden_states

    #         hidden_chunks = hidden_chunks.reshape(chunks, self.chunk_size, hidden_dim)
    #         result = torch.zeros_like(hidden_chunks)
            
    #         # Act normalize
    #         if self.h_ch_bias is None:
    #             ch_max = torch.max(hidden_chunks, dim=1, keepdim=True)[0] # chunks, 1, hidden_dim
    #             ch_min = torch.min(hidden_chunks, dim=1, keepdim=True)[0]
    #             ch_bias = torch.div(ch_max+ch_min,2)
    #             self.h_ch_bias_cal = ch_bias
    #         else:
    #             ch_bias = self.h_ch_bias[:chunks]

    #         gate_ch_bias = torch.matmul(ch_bias, gate_weight.transpose(-1,-2)) # chunks, 1, out_dim
    #         up_ch_bias = torch.matmul(ch_bias, up_weight.transpose(-1,-2))
    #         hidden_chunks = hidden_chunks.clone() - ch_bias
        
    #         if self.fc1_tmax is None:
    #             h_tmax = torch.max(torch.abs(hidden_chunks), dim=-1)[0]  # chunks, chunk_size
    #             h_tmax = torch.max(h_tmax, dim=-1)[0] # chunks
    #             self.fc1_tmax_cal = h_tmax
    #         else:
    #             h_tmax = self.fc1_tmax[:chunks]

    #         thresholds=[]
    #         for i in range(decomp_factor):
    #             thresholds.append((h_tmax / (2 ** (decomp_factor - 1 - i))).unsqueeze(-1)) # chunks, 1

    #         if self.fc1_group_index is None:
    #             h_cmax = torch.max(torch.abs(hidden_chunks), dim = -2)[0] # chunks, hidden_dim
    #             self.fc1_cmax_cal = h_cmax
        
    #         count = []
    #         for i in range(decomp_factor):
    #             if self.fc1_group_index is None:
    #                 if i==0:
    #                     mask = (h_cmax <= thresholds[i]) #chunks, hidden_dim
    #                 else: 
    #                     mask = torch.logical_and((thresholds[i-1] < h_cmax), (h_cmax <= thresholds[i]))
    #             else:
    #                 mask = (self.fc1_group_index[:chunks] == i).to(hidden_states.device)
            
    #             count.append(torch.sum(mask).item())
    #             if count[i] == 0:
    #                 continue

    #             mask = mask.unsqueeze(1).repeat(1, self.chunk_size, 1) #chunks, chunk_size, hidden_dim
    #             scale = thresholds[i].unsqueeze(-1) / q_max #chunks, 1, 1
    #             decomp_fp = torch.where(mask, hidden_chunks, zero)
    #             decomp_i = sym_quant(decomp_fp, scale, self.q_bits)
    #             result += sym_dequant(decomp_i, scale, dtype)

    #         assert sum(count) == (self.embed_dim * chunks)
        
    #         gate_out = torch.matmul(result, Wg) + gate_ch_bias
    #         gate_out = gate_out.reshape(padded_rows, self.ffn_dim)[:bsz * orig_tgt_len]
    #         up_out = torch.matmul(result, Wu) + up_ch_bias
    #         up_out = up_out.reshape(padded_rows, self.ffn_dim)[:bsz * orig_tgt_len]

    #         if self.quant_out_bf16:
    #             gate_out = quant_bfloat(gate_out)
    #             up_out = quant_bfloat(up_out)
    #         #==============================================

    #         hidden_states = self.act_fn(gate_out) * up_out

    #         #ret = self.down_proj(hidden_states)
    #         #ret = ret.view(bsz, tgt_len, self.embed_dim)
    #         # Down proj ===================================
    #         tgt_len, hidden_dim = hidden_states.size()

    #         chunks = int(math.ceil(tgt_len/self.chunk_size))
    #         assert(chunks != 0)

    #         padded_rows = chunks * self.chunk_size
    #         pad_num = padded_rows - tgt_len

    #         if pad_num > 0:
    #             padding = torch.zeros((pad_num, hidden_dim), dtype = hidden_states.dtype, device = hidden_states.device) 
    #             hidden_chunks = torch.cat((hidden_states, padding),dim=0)
    #         else:
    #             hidden_chunks = hidden_states

    #         hidden_chunks = hidden_chunks.reshape(chunks, self.chunk_size, hidden_dim)
    #         result = torch.zeros_like(hidden_chunks)

    #         if self.fc2_tmax is None:
    #             h_tmax = torch.max(torch.abs(hidden_chunks), dim=-1)[0]  # chunks, chunk_size
    #             h_tmax = torch.max(h_tmax, dim=-1)[0] # chunks
    #             self.fc2_tmax_cal = h_tmax
    #         else:
    #             h_tmax = self.fc2_tmax[:chunks]

    #         thresholds=[]
    #         for i in range(decomp_factor):
    #             thresholds.append((h_tmax / (2 ** (decomp_factor - 1 - i))).unsqueeze(-1)) # chunks, 1

    #         if self.fc2_group_index is None:
    #             h_cmax = torch.max(torch.abs(hidden_chunks), dim = -2)[0] # chunks, hidden_dim
    #             self.fc2_cmax_cal = h_cmax
        
    #         count = []
    #         for i in range(decomp_factor):
    #             if self.fc2_group_index is None:
    #                 if i==0:
    #                     mask = (h_cmax <= thresholds[i]) #chunks, hidden_dim
    #                 else: 
    #                     mask = torch.logical_and((thresholds[i-1] < h_cmax), (h_cmax <= thresholds[i]))
    #             else:
    #                 mask = (self.fc2_group_index[:chunks] == i).to(hidden_states.device)
        
    #             count.append(torch.sum(mask).item())
    #             if count[i] == 0:
    #                 continue

    #             mask = mask.unsqueeze(1).repeat(1, self.chunk_size, 1) #chunks, chunk_size, hidden_dim
    #             scale = thresholds[i].unsqueeze(-1) / q_max
    #             decomp_fp = torch.where(mask, hidden_chunks, zero)
    #             decomp_i = sym_quant(decomp_fp, scale, self.q_bits)
    #             result += sym_dequant(decomp_i, scale, dtype)
        
    #         assert sum(count) == (hidden_dim * chunks)

    #         down_out = torch.matmul(result, Wd)
    #         down_out = down_out.reshape(padded_rows, self.embed_dim)[:bsz * orig_tgt_len]

    #         if self.quant_out_bf16:
    #             down_out = quant_bfloat(down_out)

    #         ret = down_out.view(bsz, orig_tgt_len, self.embed_dim)
    #         # =============================================

    #         return ret



def repeat_kv(hidden_states: torch.Tensor, n_rep: int) -> torch.Tensor:
    """
    This is the equivalent of torch.repeat_interleave(x, dim=1, repeats=n_rep). The hidden states go from (batch,
    num_key_value_heads, seqlen, head_dim) to (batch, num_attention_heads, seqlen, head_dim)
    """
    batch, num_key_value_heads, slen, head_dim = hidden_states.shape
    if n_rep == 1:
        return hidden_states
    hidden_states = hidden_states[:, :, None, :, :].expand(batch, num_key_value_heads, n_rep, slen, head_dim)
    return hidden_states.reshape(batch, num_key_value_heads * n_rep, slen, head_dim)


class LlamaAttention_Tender_Eval(nn.Module):
    """Multi-headed attention from 'Attention Is All You Need' paper"""

    def __init__(self, config: LlamaConfig, layer_idx):
        super().__init__()
        self.config = config
        self.hidden_size = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.head_dim = self.hidden_size // self.num_heads
        self.num_key_value_heads = config.num_key_value_heads
        self.num_key_value_groups = self.num_heads // self.num_key_value_heads
        self.max_position_embeddings = config.max_position_embeddings
        self.rope_theta = config.rope_theta

        if (self.head_dim * self.num_heads) != self.hidden_size:
            raise ValueError(
                f"hidden_size must be divisible by num_heads (got `hidden_size`: {self.hidden_size}"
                f" and `num_heads`: {self.num_heads})."
            )
        self.q_proj = nn.Linear(self.hidden_size, self.num_heads * self.head_dim, bias=config.attention_bias)
        self.k_proj = nn.Linear(self.hidden_size, self.num_key_value_heads * self.head_dim, bias=config.attention_bias)
        self.v_proj = nn.Linear(self.hidden_size, self.num_key_value_heads * self.head_dim, bias=config.attention_bias)
        self.o_proj = nn.Linear(self.num_heads * self.head_dim, self.hidden_size, bias=config.attention_bias)
        self._init_rope()

        self.layer_idx = layer_idx
        
        self.quant_mha = False
        self.quant_out_bf16 = False
        self.q_bits = 4
        self.decomp_factor = 8
        self.chunk_size = 256

        # calibrated metadata
        # channel bias
        self.h_ch_bias_cal = None
        self.k_ch_bias_cal = None
        self.q_ch_bias_cal = None

        self.h_ch_bias = None
        self.k_ch_bias = None
        self.q_ch_bias = None

        # tensor-max
        self.h_tmax_cal = None
        self.q_tmax_cal = None
        self.s_tmax_cal = None
        self.o_tmax_cal = None
        
        self.h_tmax = None
        self.q_tmax = None
        self.s_tmax = None
        self.o_tmax = None
        
        # channel-max & grp idx
        self.h_cmax_cal = None
        self.q_cmax_cal = None
        self.s_cmax_cal = None
        self.o_cmax_cal = None
        
        self.h_group_index = None
        self.q_group_index = None
        self.s_group_index = None
        self.o_group_index = None

        # scale
        self.k_scale = None
        self.k_scale_cal = None
        self.v_scale = None
        self.v_scale_cal = None

    def _init_rope(self):
        if self.config.rope_scaling is None:
            self.rotary_emb = LlamaRotaryEmbedding(
                self.head_dim,
                max_position_embeddings=self.max_position_embeddings,
                base=self.rope_theta,
            )
        else:
            scaling_type = self.config.rope_scaling["type"]
            scaling_factor = self.config.rope_scaling["factor"]
            if scaling_type == "linear":
                self.rotary_emb = LlamaLinearScalingRotaryEmbedding(
                    self.head_dim,
                    max_position_embeddings=self.max_position_embeddings,
                    scaling_factor=scaling_factor,
                    base=self.rope_theta,
                )
            elif scaling_type == "dynamic":
                self.rotary_emb = LlamaDynamicNTKScalingRotaryEmbedding(
                    self.head_dim,
                    max_position_embeddings=self.max_position_embeddings,
                    scaling_factor=scaling_factor,
                    base=self.rope_theta,
                )
            else:
                raise ValueError(f"Unknown RoPE scaling type {scaling_type}")

    def _shape(self, tensor: torch.Tensor, seq_len: int, bsz: int):
        return tensor.view(bsz, seq_len, self.num_heads, self.head_dim).transpose(1, 2).contiguous()
    
    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        output_attentions: bool = False,
        use_cache: bool = False,
        padding_mask: Optional[torch.LongTensor] = None,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
        bsz, q_len, dimensions = hidden_states.size()
        self.embed_dim = dimensions
        self.kv_dim = self.k_proj.weight.data.shape[0]

        dtype = hidden_states.dtype

        bsz, tgt_len, hidden_dim = hidden_states.shape
        orig_tgt_len = tgt_len
        hidden_states = hidden_states.view(tgt_len * bsz, hidden_dim)
        tgt_len, hidden_dim = hidden_states.size()
        zero = torch.zeros((1,1), dtype = hidden_states.dtype, device = hidden_states.device)
        decomp_factor = self.decomp_factor

        query_states = self.q_proj(hidden_states)
        key_states = self.k_proj(hidden_states)
        value_states = self.v_proj(hidden_states)

        query_states = query_states.view(bsz, q_len, self.num_heads, self.head_dim).transpose(1, 2)
        key_states = key_states.view(bsz, q_len, self.num_key_value_heads, self.head_dim).transpose(1, 2)
        value_states = value_states.view(bsz, q_len, self.num_key_value_heads, self.head_dim).transpose(1, 2)

        kv_seq_len = key_states.shape[-2]
        if past_key_value is not None:
            kv_seq_len += past_key_value[0].shape[-2]
        cos, sin = self.rotary_emb(value_states, seq_len=kv_seq_len)
        query_states, key_states = apply_rotary_pos_emb(query_states, key_states, cos, sin, position_ids)

        if past_key_value is not None:
            # reuse k, v, self_attention
            key_states = torch.cat([past_key_value[0], key_states], dim=2)
            value_states = torch.cat([past_key_value[1], value_states], dim=2)

        key_states = repeat_kv(key_states, self.num_key_value_groups)
        value_states = repeat_kv(value_states, self.num_key_value_groups)

        if self.quant_mha == False:
            attn_weights = torch.matmul(query_states, key_states.transpose(2,3)) / math.sqrt(self.head_dim)
        else:
            # Q x K^T============================
            bsz, num_heads, tgt_len, _ = query_states.shape
            _, _, src_len, _ = key_states.shape
            query_states = query_states.view(bsz*num_heads, tgt_len, self.head_dim)
            key_states = key_states.view(bsz*num_heads, src_len, self.head_dim)

            query_states = query_states / math.sqrt(self.head_dim)

            q_max = (2**(self.q_bits-1)-1)

            # key normalize
            orig_k = key_states.clone() 
            if self.k_ch_bias is None:
                ch_max = torch.max(key_states, dim=1, keepdim=True)[0]
                ch_min = torch.min(key_states, dim=1, keepdim=True)[0]
                k_ch_bias = torch.div(ch_max + ch_min, 2) # b*h, 1, head_dim
                self.k_ch_bias_cal = k_ch_bias
            else:
                k_ch_bias = self.k_ch_bias

            K = key_states - k_ch_bias
            k_ch_bias = k_ch_bias.unsqueeze(2) # b*h, 1, 1, head_dim

            # key - row-wise symmetric
            if self.k_scale is None:
                scale = (torch.max(torch.abs(K), dim=-1, keepdim=True)[0]) / q_max #b*h, src_len, 1
                self.k_scale_cal = scale
            else:
                scale = self.k_scale[:, :src_len]

            K = sym_quant(K, scale, self.q_bits)
            K = sym_dequant(K, scale, dtype)
            K = K.transpose(-1, -2)

            # chunking
            chunks = int(math.ceil(tgt_len/self.chunk_size))
            assert (chunks != 0)

            padded_rows = chunks * self.chunk_size
            pad_num = padded_rows - tgt_len
            
            if pad_num > 0:
                padding = torch.zeros((bsz*num_heads, pad_num, self.head_dim), dtype = K.dtype, device = K.device) 
                hidden_chunks = torch.cat((query_states, padding),dim=1)
            else:
                hidden_chunks = query_states 

            hidden_chunks = hidden_chunks.reshape(bsz * num_heads, chunks, self.chunk_size, self.head_dim)
            result = torch.zeros_like(hidden_chunks)

            # Acts normalize
            if self.q_ch_bias is None:
                ch_max = torch.max(hidden_chunks, dim = 2, keepdim=True)[0] #b*h, chunks, 1, head_dim
                ch_min = torch.min(hidden_chunks, dim = 2, keepdim=True)[0]
                q_ch_bias = torch.div(ch_max + ch_min, 2)
                self.q_ch_bias_cal = q_ch_bias
            else:
                q_ch_bias = self.q_ch_bias[:, :chunks]

            o1_ch_bias = torch.matmul(q_ch_bias, orig_k.unsqueeze(1).repeat(1,chunks,1,1).transpose(-1,-2)) #b*h, chunks, 1, src_len (overhead)
            o2_ch_bias = torch.matmul(hidden_chunks, k_ch_bias.transpose(-1, -2)) #b*h, chunks, chunk_size, 1 (overhead)
            o3_ch_bias = - torch.matmul(q_ch_bias, k_ch_bias.transpose(-1, -2)) #b*h, chunks, 1, 1 

            hidden_chunks -= q_ch_bias

            if self.q_tmax is None:
                q_tmax = torch.max(torch.abs(hidden_chunks), dim=-1)[0]  # b*h, chunks, chunk_size
                q_tmax = torch.max(q_tmax, dim=-1)[0] # b*h, chunks
                self.q_tmax_cal = q_tmax
            else:
                q_tmax = self.q_tmax[:, :chunks]

            thresholds = []
            decomp_factor = self.decomp_factor
            for i in range(decomp_factor):
                thresholds.append((q_tmax / (2 ** (decomp_factor - 1 - i))).unsqueeze(-1)) # b*h, chunks, 1

            if self.q_group_index is None:
                q_cmax = torch.max(torch.abs(hidden_chunks),dim=-2)[0] # b*h, chunks, head_dim
                self.q_cmax_cal = q_cmax
            
            count = []
            for i in range(decomp_factor):
                if self.q_group_index is None:
                    if i==0:
                        mask = (q_cmax <= thresholds[i]) # b*h, chunks, head_dim
                    else:
                        mask = torch.logical_and((thresholds[i-1] < q_cmax), (q_cmax <= thresholds[i]))
                else:
                    mask = (self.q_group_index[:, :chunks] == i)
            
                count.append(torch.sum(mask).item())
                if count[i] == 0:
                    continue
            
                mask = mask.unsqueeze(2).repeat(1, 1, self.chunk_size, 1) #b*h, chunks, chunk_size, head_dim
                scale = thresholds[i].unsqueeze(-1) / q_max  # b*h, chunks, 1, 1
                decomp_fp = torch.where(mask, hidden_chunks, zero)
                decomp_i = sym_quant(decomp_fp, scale, self.q_bits)
                result += sym_dequant(decomp_i, scale, dtype)
            
            assert sum(count) == (self.num_heads * self.head_dim * chunks)

            result = result.reshape(bsz * self.num_heads, padded_rows, self.head_dim)
            o1_ch_bias = o1_ch_bias.repeat(1, 1, self.chunk_size, 1).reshape(bsz * self.num_heads, padded_rows, src_len)
            o2_ch_bias = o2_ch_bias.reshape(bsz*self.num_heads, padded_rows, 1)
            o3_ch_bias = o3_ch_bias.repeat(1, 1, self.chunk_size, 1).reshape(bsz * self.num_heads, padded_rows, 1)
            
            attn_weights = torch.matmul(result, K) + o1_ch_bias + o2_ch_bias + o3_ch_bias # b*h, padded_rows, src_len
            attn_weights = attn_weights[:, :orig_tgt_len]

            attn_weights = attn_weights.reshape(bsz, self.num_heads, q_len, kv_seq_len)
            # ===================================

        if attn_weights.size() != (bsz, self.num_heads, q_len, kv_seq_len):
            raise ValueError(
                f"Attention weights should be of size {(bsz, self.num_heads, q_len, kv_seq_len)}, but is"
                f" {attn_weights.size()}"
            )

        if attention_mask is not None:
            if attention_mask.size() != (bsz, 1, q_len, kv_seq_len):
                raise ValueError(
                    f"Attention mask should be of size {(bsz, 1, q_len, kv_seq_len)}, but is {attention_mask.size()}"
                )
            attn_weights = attn_weights + attention_mask

        # upcast attention to fp32
        attn_weights = nn.functional.softmax(attn_weights, dim=-1, dtype=torch.float32).to(query_states.dtype)

        if self.quant_mha == False:
            attn_output = torch.matmul(attn_weights, value_states)
        else:
            # S x V ==============================
            bsz, num_heads, tgt_len, src_len = attn_weights.shape
            attn_weights = attn_weights.reshape(bsz * num_heads, tgt_len, src_len)
            q_max = (2 ** (self.q_bits - 1) - 1)
            prob_q_max = (2 ** (self.q_bits) - 1) #for score, quantize to uint

            if self.v_scale is None:
                scale = (torch.max(torch.abs(value_states), dim = -2, keepdim = True)[0]) / q_max #heads, 1, head_dim
                self.v_scale_cal = scale
            else:
                scale = self.v_scale

            V = sym_quant(value_states, scale, self.q_bits)
            V = sym_dequant(V, scale, dtype)

            chunks = int(math.ceil(tgt_len/self.chunk_size))
            assert (chunks != 0)

            padded_rows = chunks * self.chunk_size
            pad_num = padded_rows - tgt_len
            
            if pad_num > 0:
                padding = torch.zeros((bsz*num_heads, pad_num, src_len), dtype = hidden_states.dtype, device = hidden_states.device) 
                hidden_chunks = torch.cat((attn_weights, padding),dim=1)
            else:
                hidden_chunks = attn_weights 

            hidden_chunks = hidden_chunks.reshape(bsz * num_heads, chunks, self.chunk_size, src_len)
            result = torch.zeros_like(hidden_chunks)

            if self.s_tmax is None:
                s_tmax = torch.max(torch.abs(hidden_chunks), dim=-1)[0]  # b*h, chunks, chunk_size
                s_tmax = torch.max(s_tmax, dim=-1)[0] # b*h, chunks
                self.s_tmax_cal = s_tmax
            else:
                s_tmax = self.s_tmax[:, :chunks]

            thresholds=[]
            for i in range(decomp_factor):
                thresholds.append((s_tmax / (2 ** (decomp_factor - 1 - i))).unsqueeze(-1)) # b*h, chunks, 1

            if self.s_group_index is None:
                s_cmax = torch.max(torch.abs(hidden_chunks), dim = -2)[0] # b*h, chunks, src_len
                self.s_cmax_cal = s_cmax

            count = []
            for i in range(decomp_factor):
                if self.s_group_index is None:
                    if i==0:
                        mask = (s_cmax <= thresholds[i]) # b*h, chunks, src_len
                    elif i == decomp_factor - 1:
                        mask = (s_cmax > thresholds[i-1])
                    else:
                        mask = torch.logical_and((thresholds[i-1] < s_cmax), (s_cmax <= thresholds[i]))
                else:
                    mask = (self.s_group_index[:, :chunks, :src_len] == i) 
            
                count.append(torch.sum(mask).item())
                if count[i] == 0:
                    continue
            
                mask = mask.unsqueeze(2).repeat(1, 1, self.chunk_size, 1) # b*h, chunks, chunk_size, src_len
                scale = thresholds[i].unsqueeze(-1) / prob_q_max # b*h, chunks, 1, 1
                decomp_fp = torch.where(mask, hidden_chunks, zero)
                decomp_i = torch.round(decomp_fp / scale).clamp(min = 0, max = prob_q_max)
                result += sym_dequant(decomp_i, scale, dtype)
            
            assert sum(count) == (self.num_heads * src_len * chunks)

            result = result.to(attn_weights.dtype)
            result = result.reshape(bsz * self.num_heads, padded_rows, src_len)[:, :tgt_len, ...]

            attn_output = torch.matmul(result, V).reshape(bsz, self.num_heads, tgt_len, self.head_dim)

            if self.quant_out_bf16:
                attn_output = quant_bfloat(attn_output)
            #=====================================

        if attn_output.size() != (bsz, self.num_heads, q_len, self.head_dim):
            raise ValueError(
                f"`attn_output` should be of size {(bsz, self.num_heads, q_len, self.head_dim)}, but is"
                f" {attn_output.size()}"
            )

        attn_output = attn_output.transpose(1, 2).contiguous()

        attn_output = attn_output.reshape(bsz, q_len, self.hidden_size)

        if self.config.pretraining_tp > 1:
            attn_output = attn_output.split(self.hidden_size // self.config.pretraining_tp, dim=2)
            o_proj_slices = self.o_proj.weight.split(self.hidden_size // self.config.pretraining_tp, dim=1)
            attn_output = sum([F.linear(attn_output[i], o_proj_slices[i]) for i in range(self.config.pretraining_tp)])
        else:
            attn_output = self.o_proj(attn_output)
            # # O Proj ============================
            # bsz, tgt_len, hidden_dim = attn_output.shape
            # orig_tgt_len = tgt_len
            # attn_output = attn_output.view(tgt_len * bsz, hidden_dim)
            # tgt_len, hidden_dim = attn_output.size()

            # chunks = int(math.ceil(tgt_len/self.chunk_size))
            # assert(chunks != 0)

            # padded_rows = chunks * self.chunk_size
            # pad_num = padded_rows - tgt_len
            
            # if pad_num > 0:
            #     padding = torch.zeros((pad_num, hidden_dim), dtype = attn_output.dtype, device = attn_output.device) 
            #     hidden_chunks = torch.cat((attn_output, padding),dim=0)
            # else:
            #     hidden_chunks = attn_output

            # hidden_chunks = hidden_chunks.reshape(chunks, self.chunk_size, hidden_dim)
            # result = torch.zeros_like(hidden_chunks)

            # if self.o_tmax is None:
            #     h_tmax = torch.max(torch.abs(hidden_chunks), dim=-1)[0]  # chunks, chunk_size
            #     h_tmax = torch.max(h_tmax, dim=-1)[0] # chunks
            #     self.o_tmax_cal = h_tmax
            # else:
            #     h_tmax = self.o_tmax[:chunks]

            # thresholds=[]
            # for i in range(decomp_factor):
            #     thresholds.append((h_tmax/(2**(decomp_factor-1-i))).unsqueeze(-1)) # chunks, 1

            # if self.o_group_index is None:
            #     h_cmax = torch.max(torch.abs(hidden_chunks),dim=-2)[0] # chunks, hidden_dim
            #     self.o_cmax_cal = h_cmax
        
            # count = []
            # for i in range(decomp_factor):
            #     if self.o_group_index is None:
            #         if i==0:
            #             mask = (h_cmax <= thresholds[i]) #chunks, hidden_dim
            #         else: 
            #             mask = torch.logical_and((thresholds[i-1] < h_cmax), (h_cmax <= thresholds[i]))
            #     else:
            #         mask = (self.o_group_index[:chunks] == i).to(hidden_states.device)

            #     count.append(torch.sum(mask).item())
            #     if count[i] == 0:
            #         continue

            #     mask = mask.unsqueeze(1).repeat(1, self.chunk_size, 1) #chunks, chunk_size, hidden_dim
            #     scale = thresholds[i].unsqueeze(-1)/q_max #chunks, 1, 1
            #     decomp_fp = torch.where(mask, hidden_chunks, zero)
            #     decomp_i = sym_quant(decomp_fp, scale, self.q_bits)
            #     result += sym_dequant(decomp_i, scale, dtype)
                
            # assert sum(count) == (self.embed_dim * chunks)

            # out = torch.matmul(result, self.WO)
            # out = out.reshape(padded_rows, hidden_dim)[:bsz * orig_tgt_len]

            # attn_output = out.reshape(bsz, orig_tgt_len, hidden_dim)

            # if self.quant_out_bf16:
            #     attn_output = quant_bfloat(attn_output)
            #===================================

        if not output_attentions:
            attn_weights = None

        return attn_output, attn_weights, past_key_value


class LlamaFlashAttention2(LlamaAttention_Tender_Eval):
    """
    Llama flash attention module. This module inherits from `LlamaAttention` as the weights of the module stays
    untouched. The only required change would be on the forward pass where it needs to correctly call the public API of
    flash attention and deal with padding tokens in case the input contains any of them.
    """

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        output_attentions: bool = False,
        use_cache: bool = False,
        padding_mask: Optional[torch.LongTensor] = None,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
        # LlamaFlashAttention2 attention does not support output_attentions
        output_attentions = False

        bsz, q_len, _ = hidden_states.size()

        query_states = self.q_proj(hidden_states)
        key_states = self.k_proj(hidden_states)
        value_states = self.v_proj(hidden_states)

        # Flash attention requires the input to have the shape
        # batch_size x seq_length x head_dime x hidden_dim
        # therefore we just need to keep the original shape
        query_states = query_states.view(bsz, q_len, self.num_heads, self.head_dim).transpose(1, 2)
        key_states = key_states.view(bsz, q_len, self.num_key_value_heads, self.head_dim).transpose(1, 2)
        value_states = value_states.view(bsz, q_len, self.num_key_value_heads, self.head_dim).transpose(1, 2)

        kv_seq_len = key_states.shape[-2]
        if past_key_value is not None:
            kv_seq_len += past_key_value[0].shape[-2]

        cos, sin = self.rotary_emb(value_states, seq_len=kv_seq_len)

        query_states, key_states = apply_rotary_pos_emb(query_states, key_states, cos, sin, position_ids)

        if past_key_value is not None:
            # reuse k, v, self_attention
            key_states = torch.cat([past_key_value[0], key_states], dim=2)
            value_states = torch.cat([past_key_value[1], value_states], dim=2)

        past_key_value = (key_states, value_states) if use_cache else None

        query_states = query_states.transpose(1, 2)
        key_states = key_states.transpose(1, 2)
        value_states = value_states.transpose(1, 2)

        # TODO: llama does not have dropout in the config??
        # It is recommended to use dropout with FA according to the docs
        # when training.
        dropout_rate = 0.0  # if not self.training else self.attn_dropout

        # In PEFT, usually we cast the layer norms in float32 for training stability reasons
        # therefore the input hidden states gets silently casted in float32. Hence, we need
        # cast them back in float16 just to be sure everything works as expected.
        # This might slowdown training & inference so it is recommended to not cast the LayerNorms
        # in fp32. (LlamaRMSNorm handles it correctly)
        input_dtype = query_states.dtype
        if input_dtype == torch.float32:
            logger.warning_once(
                "The input hidden states seems to be silently casted in float32, this might be related to"
                " the fact you have upcasted embedding or layer norm layers in float32. We will cast back the input in"
                " float16."
            )

            query_states = query_states.to(torch.float16)
            key_states = key_states.to(torch.float16)
            value_states = value_states.to(torch.float16)

        attn_output = self._flash_attention_forward(
            query_states, key_states, value_states, padding_mask, q_len, dropout=dropout_rate
        )

        attn_output = attn_output.reshape(bsz, q_len, self.hidden_size).contiguous()
        attn_output = self.o_proj(attn_output)

        if not output_attentions:
            attn_weights = None

        return attn_output, attn_weights, past_key_value

    def _flash_attention_forward(
        self, query_states, key_states, value_states, padding_mask, query_length, dropout=0.0, softmax_scale=None
    ):
        """
        Calls the forward method of Flash Attention - if the input hidden states contain at least one padding token
        first unpad the input, then computes the attention scores and pad the final attention scores.

        Args:
            query_states (`torch.Tensor`):
                Input query states to be passed to Flash Attention API
            key_states (`torch.Tensor`):
                Input key states to be passed to Flash Attention API
            value_states (`torch.Tensor`):
                Input value states to be passed to Flash Attention API
            padding_mask (`torch.Tensor`):
                The padding mask - corresponds to a tensor of size `(batch_size, seq_len)` where 0 stands for the
                position of padding tokens and 1 for the position of non-padding tokens.
            dropout (`int`, *optional*):
                Attention dropout
            softmax_scale (`float`, *optional*):
                The scaling of QK^T before applying softmax. Default to 1 / sqrt(head_dim)
        """
        # Contains at least one padding token in the sequence
        if padding_mask is not None:
            batch_size = query_states.shape[0]
            query_states, key_states, value_states, indices_q, cu_seq_lens, max_seq_lens = self._upad_input(
                query_states, key_states, value_states, padding_mask, query_length
            )

            cu_seqlens_q, cu_seqlens_k = cu_seq_lens
            max_seqlen_in_batch_q, max_seqlen_in_batch_k = max_seq_lens

            attn_output_unpad = flash_attn_varlen_func(
                query_states,
                key_states,
                value_states,
                cu_seqlens_q=cu_seqlens_q,
                cu_seqlens_k=cu_seqlens_k,
                max_seqlen_q=max_seqlen_in_batch_q,
                max_seqlen_k=max_seqlen_in_batch_k,
                dropout_p=dropout,
                softmax_scale=softmax_scale,
                causal=True,
            )

            attn_output = pad_input(attn_output_unpad, indices_q, batch_size, query_length)
        else:
            attn_output = flash_attn_func(
                query_states, key_states, value_states, dropout, softmax_scale=softmax_scale, causal=True
            )

        return attn_output

    def _upad_input(self, query_layer, key_layer, value_layer, padding_mask, query_length):
        indices_k, cu_seqlens_k, max_seqlen_in_batch_k = _get_unpad_data(padding_mask)
        batch_size, kv_seq_len, num_key_value_heads, head_dim = key_layer.shape

        key_layer = index_first_axis(
            key_layer.reshape(batch_size * kv_seq_len, num_key_value_heads, head_dim), indices_k
        )
        value_layer = index_first_axis(
            value_layer.reshape(batch_size * kv_seq_len, num_key_value_heads, head_dim), indices_k
        )
        if query_length == kv_seq_len:
            query_layer = index_first_axis(
                query_layer.reshape(batch_size * kv_seq_len, self.num_heads, head_dim), indices_k
            )
            cu_seqlens_q = cu_seqlens_k
            max_seqlen_in_batch_q = max_seqlen_in_batch_k
            indices_q = indices_k
        elif query_length == 1:
            max_seqlen_in_batch_q = 1
            cu_seqlens_q = torch.arange(
                batch_size + 1, dtype=torch.int32, device=query_layer.device
            )  # There is a memcpy here, that is very bad.
            indices_q = cu_seqlens_q[:-1]
            query_layer = query_layer.squeeze(1)
        else:
            # The -q_len: slice assumes left padding.
            padding_mask = padding_mask[:, -query_length:]
            query_layer, indices_q, cu_seqlens_q, max_seqlen_in_batch_q = unpad_input(query_layer, padding_mask)

        return (
            query_layer,
            key_layer,
            value_layer,
            indices_q,
            (cu_seqlens_q, cu_seqlens_k),
            (max_seqlen_in_batch_q, max_seqlen_in_batch_k),
        )


class LlamaDecoderLayer(nn.Module):
    def __init__(self, config: LlamaConfig, layer_idx):
        super().__init__()
        self.hidden_size = config.hidden_size
        self.self_attn = (
            LlamaAttention_Tender_Eval(config=config, layer_idx=layer_idx)
            if not getattr(config, "_flash_attn_2_enabled", False)
            else LlamaFlashAttention2(config=config)
        )
        self.mlp = LlamaMLP(config, layer_idx)
        self.input_layernorm = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.post_attention_layernorm = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)

        self.layer_idx = layer_idx

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        output_attentions: Optional[bool] = False,
        use_cache: Optional[bool] = False,
        padding_mask: Optional[torch.LongTensor] = None,
    ) -> Tuple[torch.FloatTensor, Optional[Tuple[torch.FloatTensor, torch.FloatTensor]]]:
        """
        Args:
            hidden_states (`torch.FloatTensor`): input to the layer of shape `(batch, seq_len, embed_dim)`
            attention_mask (`torch.FloatTensor`, *optional*): attention mask of size
                `(batch, 1, tgt_len, src_len)` where padding elements are indicated by very large negative values.
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers. See `attentions` under
                returned tensors for more detail.
            use_cache (`bool`, *optional*):
                If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
                (see `past_key_values`).
            past_key_value (`Tuple(torch.FloatTensor)`, *optional*): cached past key and value projection states
        """
        residual = hidden_states

        hidden_states = self.input_layernorm(hidden_states)

        # Self Attention
        hidden_states, self_attn_weights, present_key_value = self.self_attn(
            hidden_states=hidden_states,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_value=past_key_value,
            output_attentions=output_attentions,
            use_cache=use_cache,
            padding_mask=padding_mask,
        )
        hidden_states = residual + hidden_states

        # Fully Connected
        residual = hidden_states

        hidden_states = self.post_attention_layernorm(hidden_states)
        hidden_states = self.mlp(hidden_states)

        hidden_states = residual + hidden_states

        outputs = (hidden_states,)

        if output_attentions:
            outputs += (self_attn_weights,)

        if use_cache:
            outputs += (present_key_value,)

        return outputs


LLAMA_START_DOCSTRING = r"""
    This model inherits from [`PreTrainedModel`]. Check the superclass documentation for the generic methods the
    library implements for all its model (such as downloading or saving, resizing the input embeddings, pruning heads
    etc.)

    This model is also a PyTorch [torch.nn.Module](https://pytorch.org/docs/stable/nn.html#torch.nn.Module) subclass.
    Use it as a regular PyTorch Module and refer to the PyTorch documentation for all matter related to general usage
    and behavior.

    Parameters:
        config ([`LlamaConfig`]):
            Model configuration class with all the parameters of the model. Initializing with a config file does not
            load the weights associated with the model, only the configuration. Check out the
            [`~PreTrainedModel.from_pretrained`] method to load the model weights.
"""


@add_start_docstrings(
    "The bare LLaMA Model outputting raw hidden-states without any specific head on top.",
    LLAMA_START_DOCSTRING,
)
class LlamaPreTrainedModel(PreTrainedModel):
    config_class = LlamaConfig
    base_model_prefix = "model"
    supports_gradient_checkpointing = True
    _no_split_modules = ["LlamaDecoderLayer"]
    _skip_keys_device_placement = "past_key_values"
    _supports_flash_attn_2 = True

    def _init_weights(self, module):
        std = self.config.initializer_range
        if isinstance(module, nn.Linear):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=std)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()

    def _set_gradient_checkpointing(self, module, value=False):
        if isinstance(module, LlamaModel):
            module.gradient_checkpointing = value


LLAMA_INPUTS_DOCSTRING = r"""
    Args:
        input_ids (`torch.LongTensor` of shape `(batch_size, sequence_length)`):
            Indices of input sequence tokens in the vocabulary. Padding will be ignored by default should you provide
            it.

            Indices can be obtained using [`AutoTokenizer`]. See [`PreTrainedTokenizer.encode`] and
            [`PreTrainedTokenizer.__call__`] for details.

            [What are input IDs?](../glossary#input-ids)
        attention_mask (`torch.Tensor` of shape `(batch_size, sequence_length)`, *optional*):
            Mask to avoid performing attention on padding token indices. Mask values selected in `[0, 1]`:

            - 1 for tokens that are **not masked**,
            - 0 for tokens that are **masked**.

            [What are attention masks?](../glossary#attention-mask)

            Indices can be obtained using [`AutoTokenizer`]. See [`PreTrainedTokenizer.encode`] and
            [`PreTrainedTokenizer.__call__`] for details.

            If `past_key_values` is used, optionally only the last `input_ids` have to be input (see
            `past_key_values`).

            If you want to change padding behavior, you should read [`modeling_opt._prepare_decoder_attention_mask`]
            and modify to your needs. See diagram 1 in [the paper](https://arxiv.org/abs/1910.13461) for more
            information on the default strategy.

            - 1 indicates the head is **not masked**,
            - 0 indicates the head is **masked**.
        position_ids (`torch.LongTensor` of shape `(batch_size, sequence_length)`, *optional*):
            Indices of positions of each input sequence tokens in the position embeddings. Selected in the range `[0,
            config.n_positions - 1]`.

            [What are position IDs?](../glossary#position-ids)
        past_key_values (`tuple(tuple(torch.FloatTensor))`, *optional*, returned when `use_cache=True` is passed or when `config.use_cache=True`):
            Tuple of `tuple(torch.FloatTensor)` of length `config.n_layers`, with each tuple having 2 tensors of shape
            `(batch_size, num_heads, sequence_length, embed_size_per_head)`) and 2 additional tensors of shape
            `(batch_size, num_heads, encoder_sequence_length, embed_size_per_head)`.

            Contains pre-computed hidden-states (key and values in the self-attention blocks and in the cross-attention
            blocks) that can be used (see `past_key_values` input) to speed up sequential decoding.

            If `past_key_values` are used, the user can optionally input only the last `input_ids` (those that don't
            have their past key value states given to this model) of shape `(batch_size, 1)` instead of all `input_ids`
            of shape `(batch_size, sequence_length)`.
        inputs_embeds (`torch.FloatTensor` of shape `(batch_size, sequence_length, hidden_size)`, *optional*):
            Optionally, instead of passing `input_ids` you can choose to directly pass an embedded representation. This
            is useful if you want more control over how to convert `input_ids` indices into associated vectors than the
            model's internal embedding lookup matrix.
        use_cache (`bool`, *optional*):
            If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding (see
            `past_key_values`).
        output_attentions (`bool`, *optional*):
            Whether or not to return the attentions tensors of all attention layers. See `attentions` under returned
            tensors for more detail.
        output_hidden_states (`bool`, *optional*):
            Whether or not to return the hidden states of all layers. See `hidden_states` under returned tensors for
            more detail.
        return_dict (`bool`, *optional*):
            Whether or not to return a [`~utils.ModelOutput`] instead of a plain tuple.
"""


@add_start_docstrings(
    "The bare LLaMA Model outputting raw hidden-states without any specific head on top.",
    LLAMA_START_DOCSTRING,
)
class LlamaModel(LlamaPreTrainedModel):
    """
    Transformer decoder consisting of *config.num_hidden_layers* layers. Each layer is a [`LlamaDecoderLayer`]

    Args:
        config: LlamaConfig
    """

    def __init__(self, config: LlamaConfig):
        super().__init__(config)
        self.padding_idx = config.pad_token_id
        self.vocab_size = config.vocab_size

        self.embed_tokens = nn.Embedding(config.vocab_size, config.hidden_size, self.padding_idx)
        self.layers = nn.ModuleList([LlamaDecoderLayer(config, i) for i in range(config.num_hidden_layers)])
        self.norm = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)

        self.gradient_checkpointing = False
        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.embed_tokens

    def set_input_embeddings(self, value):
        self.embed_tokens = value

    # Copied from transformers.models.bart.modeling_bart.BartDecoder._prepare_decoder_attention_mask
    def _prepare_decoder_attention_mask(self, attention_mask, input_shape, inputs_embeds, past_key_values_length):
        # create causal mask
        # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
        combined_attention_mask = None
        if input_shape[-1] > 1:
            combined_attention_mask = _make_causal_mask(
                input_shape,
                inputs_embeds.dtype,
                device=inputs_embeds.device,
                past_key_values_length=past_key_values_length,
            )

        if attention_mask is not None:
            # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
            expanded_attn_mask = _expand_mask(attention_mask, inputs_embeds.dtype, tgt_len=input_shape[-1]).to(
                inputs_embeds.device
            )
            combined_attention_mask = (
                expanded_attn_mask if combined_attention_mask is None else expanded_attn_mask + combined_attention_mask
            )

        return combined_attention_mask

    @add_start_docstrings_to_model_forward(LLAMA_INPUTS_DOCSTRING)
    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, BaseModelOutputWithPast]:

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache

        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # retrieve input_ids and inputs_embeds
        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            batch_size, seq_length = input_ids.shape
        elif inputs_embeds is not None:
            batch_size, seq_length, _ = inputs_embeds.shape
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        seq_length_with_past = seq_length
        past_key_values_length = 0

        if past_key_values is not None:
            past_key_values_length = past_key_values[0][0].shape[2]
            seq_length_with_past = seq_length_with_past + past_key_values_length

        if position_ids is None:
            device = input_ids.device if input_ids is not None else inputs_embeds.device
            position_ids = torch.arange(
                past_key_values_length, seq_length + past_key_values_length, dtype=torch.long, device=device
            )
            position_ids = position_ids.unsqueeze(0).view(-1, seq_length)
        else:
            position_ids = position_ids.view(-1, seq_length).long()

        if inputs_embeds is None:
            inputs_embeds = self.embed_tokens(input_ids)
        # embed positions
        if attention_mask is None:
            attention_mask = torch.ones(
                (batch_size, seq_length_with_past), dtype=torch.bool, device=inputs_embeds.device
            )
            padding_mask = None
        else:
            if 0 in attention_mask:
                padding_mask = attention_mask
            else:
                padding_mask = None

        attention_mask = self._prepare_decoder_attention_mask(
            attention_mask, (batch_size, seq_length), inputs_embeds, past_key_values_length
        )

        hidden_states = inputs_embeds

        if self.gradient_checkpointing and self.training:
            if use_cache:
                logger.warning_once(
                    "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`..."
                )
                use_cache = False

        # decoder layers
        all_hidden_states = () if output_hidden_states else None
        all_self_attns = () if output_attentions else None
        next_decoder_cache = () if use_cache else None

        for idx, decoder_layer in enumerate(self.layers):
            if output_hidden_states:
                all_hidden_states += (hidden_states,)

            past_key_value = past_key_values[idx] if past_key_values is not None else None

            if self.gradient_checkpointing and self.training:

                def create_custom_forward(module):
                    def custom_forward(*inputs):
                        # None for past_key_value
                        return module(*inputs, past_key_value, output_attentions, padding_mask=padding_mask)

                    return custom_forward

                layer_outputs = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(decoder_layer), hidden_states, attention_mask, position_ids
                )
            else:
                layer_outputs = decoder_layer(
                    hidden_states,
                    attention_mask=attention_mask,
                    position_ids=position_ids,
                    past_key_value=past_key_value,
                    output_attentions=output_attentions,
                    use_cache=use_cache,
                    padding_mask=padding_mask,
                )

            hidden_states = layer_outputs[0]

            if use_cache:
                next_decoder_cache += (layer_outputs[2 if output_attentions else 1],)

            if output_attentions:
                all_self_attns += (layer_outputs[1],)

        hidden_states = self.norm(hidden_states)

        # add hidden states from the last decoder layer
        if output_hidden_states:
            all_hidden_states += (hidden_states,)

        next_cache = next_decoder_cache if use_cache else None
        if not return_dict:
            return tuple(v for v in [hidden_states, next_cache, all_hidden_states, all_self_attns] if v is not None)
        return BaseModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=next_cache,
            hidden_states=all_hidden_states,
            attentions=all_self_attns,
        )

class LmHeadTender(nn.Module):
    def __init__(self):
        super().__init__()
        self.quant_out_bf16 = False
        self.q_bits = 4
        self.decomp_factor = 14
        self.chunk_size = 256

    def forward(self, hidden_states, lm_weight):
        dtype = hidden_states.dtype
        decomp_factor = self.decomp_factor
        q_max = (2**(self.q_bits-1))-1

        scale = (torch.max(torch.abs(lm_weight.clone()),dim=-1,keepdim=True)[0])/q_max
        Wl = sym_dequant(sym_quant(lm_weight.clone(), scale, self.q_bits), scale, dtype)
        Wl = Wl.transpose(-1,-2)

        # LM Head =========================
        bsz, tgt_len, hidden_dim = hidden_states.shape
        orig_tgt_len = tgt_len
        hidden_states = hidden_states.view(tgt_len*bsz, hidden_dim)
        tgt_len, hidden_dim = hidden_states.size()
        zero = torch.zeros((1,1), dtype = hidden_states.dtype, device = hidden_states.device)

        chunks = int(math.ceil(tgt_len/self.chunk_size))
        assert(chunks != 0)

        padded_rows = chunks * self.chunk_size
        pad_num = padded_rows - tgt_len
        
        if pad_num > 0:
            padding = torch.zeros((pad_num, hidden_dim), dtype = hidden_states.dtype, device = hidden_states.device) 
            hidden_chunks = torch.cat((hidden_states, padding),dim=0)
        else:
            hidden_chunks = hidden_states

        hidden_chunks = hidden_chunks.reshape(chunks, self.chunk_size, hidden_dim)
        result = torch.zeros_like(hidden_chunks)

        h_tmax = torch.max(torch.abs(hidden_chunks), dim=-1)[0]  # chunks, chunk_size
        h_tmax = torch.max(h_tmax, dim=-1)[0] # chunks

        thresholds = []
        for i in range(decomp_factor):
            thresholds.append((h_tmax / (2 ** (decomp_factor - 1 - i))).unsqueeze(-1)) # chunks, 1
        
        h_cmax = torch.max(torch.abs(hidden_chunks), dim = -2)[0] # chunks, hidden_dim
        
        count = []
        for i in range(decomp_factor):
            if i==0:
                mask = (h_cmax <= thresholds[i]) #chunks, hidden_dim
            elif i == decomp_factor - 1:
                mask = (h_cmax > thresholds[i-1])
            else: 
                mask = torch.logical_and((thresholds[i - 1] < h_cmax), (h_cmax <= thresholds[i]))
        
            count.append(torch.sum(mask).item())
            if count[i] == 0:
                continue

            mask = mask.unsqueeze(1).repeat(1, self.chunk_size, 1) #chunks, chunk_size, hidden_dim
            scale = thresholds[i].unsqueeze(-1) / q_max #chunks, 1, 1
            decomp_fp = torch.where(mask, hidden_chunks, zero)
            decomp_i = sym_quant(decomp_fp, scale, self.q_bits)
            result += sym_dequant(decomp_i, scale, dtype)

        assert sum(count) == (hidden_dim * chunks)

        lm_out = torch.matmul(result, Wl)
        logits = lm_out.reshape(padded_rows, -1)[:bsz * orig_tgt_len]
        logits = logits.reshape(bsz, orig_tgt_len, -1)
        if self.quant_out_bf16:
            logits = quant_bfloat(logits)
        return logits

class LlamaForCausalLM(LlamaPreTrainedModel):
    _tied_weights_keys = ["lm_head.weight"]

    def __init__(self, config):
        super().__init__(config)
        self.model = LlamaModel(config)
        self.vocab_size = config.vocab_size

        self.quant_lm_head = False
        self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)
        self.lm_head_tender = LmHeadTender()

        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.model.embed_tokens

    def set_input_embeddings(self, value):
        self.model.embed_tokens = value

    def get_output_embeddings(self):
        return self.lm_head

    def set_output_embeddings(self, new_embeddings):
        self.lm_head = new_embeddings

    def set_decoder(self, decoder):
        self.model = decoder

    def get_decoder(self):
        return self.model

    @add_start_docstrings_to_model_forward(LLAMA_INPUTS_DOCSTRING)
    @replace_return_docstrings(output_type=CausalLMOutputWithPast, config_class=_CONFIG_FOR_DOC)
    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, CausalLMOutputWithPast]:
        r"""
        Args:
            labels (`torch.LongTensor` of shape `(batch_size, sequence_length)`, *optional*):
                Labels for computing the masked language modeling loss. Indices should either be in `[0, ...,
                config.vocab_size]` or -100 (see `input_ids` docstring). Tokens with indices set to `-100` are ignored
                (masked), the loss is only computed for the tokens with labels in `[0, ..., config.vocab_size]`.

        Returns:

        Example:

        ```python
        >>> from transformers import AutoTokenizer, LlamaForCausalLM

        >>> model = LlamaForCausalLM.from_pretrained(PATH_TO_CONVERTED_WEIGHTS)
        >>> tokenizer = AutoTokenizer.from_pretrained(PATH_TO_CONVERTED_TOKENIZER)

        >>> prompt = "Hey, are you conscious? Can you talk to me?"
        >>> inputs = tokenizer(prompt, return_tensors="pt")

        >>> # Generate
        >>> generate_ids = model.generate(inputs.input_ids, max_length=30)
        >>> tokenizer.batch_decode(generate_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]
        "Hey, are you conscious? Can you talk to me?\nI'm not conscious, but I can talk to you."
        ```"""

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # decoder outputs consists of (dec_features, layer_state, dec_hidden, dec_attn)
        outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        hidden_states = outputs[0]
        if self.config.pretraining_tp > 1:
            lm_head_slices = self.lm_head.weight.split(self.vocab_size // self.config.pretraining_tp, dim=0)
            logits = [F.linear(hidden_states, lm_head_slices[i]) for i in range(self.config.pretraining_tp)]
            logits = torch.cat(logits, dim=-1)
        else:
            if self.quant_lm_head:
                lm_weight = self.lm_head.weight.data.clone()
                logits = self.lm_head_tender(hidden_states, lm_weight)
            else:
                logits = self.lm_head(hidden_states)

        logits = logits.float()

        loss = None
        if labels is not None:
            # Shift so that tokens < n predict n
            shift_logits = logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            # Flatten the tokens
            loss_fct = CrossEntropyLoss()
            shift_logits = shift_logits.view(-1, self.config.vocab_size)
            shift_labels = shift_labels.view(-1)
            # Enable model parallelism
            shift_labels = shift_labels.to(shift_logits.device)
            loss = loss_fct(shift_logits, shift_labels)

        if not return_dict:
            output = (logits,) + outputs[1:]
            return (loss,) + output if loss is not None else output

        return CausalLMOutputWithPast(
            loss=loss,
            logits=logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )

    def prepare_inputs_for_generation(
        self, input_ids, past_key_values=None, attention_mask=None, inputs_embeds=None, **kwargs
    ):
        if past_key_values:
            input_ids = input_ids[:, -1:]

        position_ids = kwargs.get("position_ids", None)
        if attention_mask is not None and position_ids is None:
            # create position_ids on the fly for batch generation
            position_ids = attention_mask.long().cumsum(-1) - 1
            position_ids.masked_fill_(attention_mask == 0, 1)
            if past_key_values:
                position_ids = position_ids[:, -1].unsqueeze(-1)

        # if `inputs_embeds` are passed, we only want to use them in the 1st generation step
        if inputs_embeds is not None and past_key_values is None:
            model_inputs = {"inputs_embeds": inputs_embeds}
        else:
            model_inputs = {"input_ids": input_ids}

        model_inputs.update(
            {
                "position_ids": position_ids,
                "past_key_values": past_key_values,
                "use_cache": kwargs.get("use_cache"),
                "attention_mask": attention_mask,
            }
        )
        return model_inputs

    @staticmethod
    def _reorder_cache(past_key_values, beam_idx):
        reordered_past = ()
        for layer_past in past_key_values:
            reordered_past += (
                tuple(past_state.index_select(0, beam_idx.to(past_state.device)) for past_state in layer_past),
            )
        return reordered_past


@add_start_docstrings(
    """
    The LLaMa Model transformer with a sequence classification head on top (linear layer).

    [`LlamaForSequenceClassification`] uses the last token in order to do the classification, as other causal models
    (e.g. GPT-2) do.

    Since it does classification on the last token, it requires to know the position of the last token. If a
    `pad_token_id` is defined in the configuration, it finds the last token that is not a padding token in each row. If
    no `pad_token_id` is defined, it simply takes the last value in each row of the batch. Since it cannot guess the
    padding tokens when `inputs_embeds` are passed instead of `input_ids`, it does the same (take the last value in
    each row of the batch).
    """,
    LLAMA_START_DOCSTRING,
)
class LlamaForSequenceClassification(LlamaPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels
        self.model = LlamaModel(config)
        self.score = nn.Linear(config.hidden_size, self.num_labels, bias=False)

        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.model.embed_tokens

    def set_input_embeddings(self, value):
        self.model.embed_tokens = value

    @add_start_docstrings_to_model_forward(LLAMA_INPUTS_DOCSTRING)
    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, SequenceClassifierOutputWithPast]:
        r"""
        labels (`torch.LongTensor` of shape `(batch_size,)`, *optional*):
            Labels for computing the sequence classification/regression loss. Indices should be in `[0, ...,
            config.num_labels - 1]`. If `config.num_labels == 1` a regression loss is computed (Mean-Square loss), If
            `config.num_labels > 1` a classification loss is computed (Cross-Entropy).
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        transformer_outputs = self.model(
            input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        hidden_states = transformer_outputs[0]
        logits = self.score(hidden_states)

        if input_ids is not None:
            batch_size = input_ids.shape[0]
        else:
            batch_size = inputs_embeds.shape[0]

        if self.config.pad_token_id is None and batch_size != 1:
            raise ValueError("Cannot handle batch sizes > 1 if no padding token is defined.")
        if self.config.pad_token_id is None:
            sequence_lengths = -1
        else:
            if input_ids is not None:
                sequence_lengths = (torch.eq(input_ids, self.config.pad_token_id).long().argmax(-1) - 1).to(
                    logits.device
                )
            else:
                sequence_lengths = -1

        pooled_logits = logits[torch.arange(batch_size, device=logits.device), sequence_lengths]

        loss = None
        if labels is not None:
            labels = labels.to(logits.device)
            if self.config.problem_type is None:
                if self.num_labels == 1:
                    self.config.problem_type = "regression"
                elif self.num_labels > 1 and (labels.dtype == torch.long or labels.dtype == torch.int):
                    self.config.problem_type = "single_label_classification"
                else:
                    self.config.problem_type = "multi_label_classification"

            if self.config.problem_type == "regression":
                loss_fct = MSELoss()
                if self.num_labels == 1:
                    loss = loss_fct(pooled_logits.squeeze(), labels.squeeze())
                else:
                    loss = loss_fct(pooled_logits, labels)
            elif self.config.problem_type == "single_label_classification":
                loss_fct = CrossEntropyLoss()
                loss = loss_fct(pooled_logits.view(-1, self.num_labels), labels.view(-1))
            elif self.config.problem_type == "multi_label_classification":
                loss_fct = BCEWithLogitsLoss()
                loss = loss_fct(pooled_logits, labels)
        if not return_dict:
            output = (pooled_logits,) + transformer_outputs[1:]
            return ((loss,) + output) if loss is not None else output

        return SequenceClassifierOutputWithPast(
            loss=loss,
            logits=pooled_logits,
            past_key_values=transformer_outputs.past_key_values,
            hidden_states=transformer_outputs.hidden_states,
            attentions=transformer_outputs.attentions,
        )
