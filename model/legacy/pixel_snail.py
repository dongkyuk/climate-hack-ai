import math
import numpy as np
import torch
from torch import nn
from torch.nn import functional as F
from model.pixel_snail_2d import PixelSNAIL2D
from einops import rearrange

# [1] PixelSNAIL: https://arxiv.org/pdf/1712.09763.pdf

def elu_conv_elu(conv, x):
    """ELU -> conv -> ELU

    Args:
        conv : Desired convolutional module to use
        x : Input to ELU -> conv -> ELU block
    """
    return F.elu(conv(F.elu(x)))


def positional_encoding(shape):
    """Channel-wise positional encoding for 3D data

    Args:
        shape : Shape of input to be encoded

    Returns:
        The positional encoding (B x 3 x T x H x W)
    """
    b, _, t, h, w = shape
    zeros = torch.zeros((b, 1, t, h, w))
    return torch.cat(
        (
            (torch.arange(-0.5, 0.5, 1 / t)[None, None, :, None, None] + zeros),
            (torch.arange(-0.5, 0.5, 1 / h)[None, None, None, :, None] + zeros),
            (torch.arange(-0.5, 0.5, 1 / w)[None, None, None, None, :] + zeros)
        ),
        dim=1
    )


def causal_mask(seq_len, mask_center=False):
    """Causal masking for attention

    Args:
        seq_len : Length of sequence to mask
        mask_center : Mask itself. Defaults to False.

    Returns:
        (seq_len x seq_len) mask tensor
    """
    return torch.tril(torch.ones((seq_len, seq_len)), diagonal=-int(mask_center))


class CausalConv3d(nn.Conv3d):
    """Causal convolution

    Only pixels behind in time, or above/to the left
    in current time can be seen.

    mask_center decides if center of conv kernel
    should be masked or not.
    """

    def __init__(self, mask_center=False, *args, **kwargs):
        super().__init__(*args, **kwargs)
        i, o, t, h, w = self.weight.shape
        mask = torch.zeros((i, o, t, h, w))
        mask[:, :, : t // 2, :, :] = 1
        mask[:, :, t // 2, : h // 2, :] = 1
        mask[:, :, t // 2, h // 2, :w // 2 + int(not mask_center)] = 1
        self.register_buffer("mask", mask)

    def forward(self, x):
        self.weight.data *= self.mask
        return super().forward(x)


class GatedActivation(nn.Module):
    """Gated activation function
    
    First half of channels go to activation function,
    other half goes to sigmoid
    """

    def __init__(self, activation_fn=torch.tanh):
        super().__init__()
        self.activation_fn = activation_fn

    def forward(self, x):
        _, c, _, _, _ = x.shape
        x, gate = x[:, : c // 2, :, :, :], x[:, c // 2:, :, :, :]
        return self.activation_fn(x) * torch.sigmoid(gate)


class ResidualBlock(nn.Module):
    """Residual block with gated activation function as seen in [1]
    
    Note that kernel size 2 is causal in nature, as long as you
    leave edge out of output, as they don't mean anything
    valuable. This also keeps input dim same as output dim.
    """

    def __init__(self, n_channels, attention=True):
        super().__init__()
        if attention:
            self.input_conv = nn.Conv3d(in_channels=n_channels, out_channels=n_channels, kernel_size=2, padding=1)
            self.output_conv = nn.Conv3d(in_channels=n_channels, out_channels=2 * n_channels, kernel_size=2, padding=1)
        else:
            self.input_conv = CausalConv3d(mask_center=False, in_channels=n_channels, out_channels=n_channels,
                                           kernel_size=(3, 7, 7), padding=(1, 3, 3))
            self.output_conv = CausalConv3d(mask_center=False, in_channels=n_channels, out_channels=2 * n_channels,
                                            kernel_size=(3, 7, 7), padding=(1, 3, 3))

        self.activation = GatedActivation(activation_fn=nn.Identity())

    def forward(self, x):
        _, _, t, h, w = x.shape
        out = elu_conv_elu(self.input_conv, x)[:, :, :t, :h, :w]
        out = self.activation(self.output_conv(out)[:, :, :t, :h, :w])
        return x + out


class AttentionBlock(nn.Module):
    """Attention Block as seen in [1]
    
    Block takes in output of ResidualBlock concatenated with 
    the original input. Module takes care of pos-encoding.
    """

    def __init__(self, in_channels=259, key_channels=16, value_channels=128):
        super().__init__()
        self.query = nn.Conv3d(in_channels=in_channels + 3, out_channels=key_channels, kernel_size=1)
        self.key = nn.Conv3d(in_channels=in_channels + 3, out_channels=key_channels, kernel_size=1)
        self.value = nn.Conv3d(in_channels=in_channels + 3, out_channels=value_channels, kernel_size=1)

    def flatten(self, x):
        """(B, C, T, H, W) ->(B, T*H*W, C)

        Returns:
            Flattened sequence of pixels
        """
        return x.view(x.shape[0], x.shape[1], -1).transpose(1, 2)

    def forward(self, x):
        b, c, t, h, w = x.shape
        pos_enc = positional_encoding(x.shape).to(x.device)
        x = torch.cat((x, pos_enc), dim=1)
        q = self.flatten(self.query(x))
        k = self.flatten(self.key(x))
        v = self.flatten(self.value(x))

        attention = torch.bmm(q, k.transpose(-2, -1)) / math.sqrt(c)
        mask = causal_mask(t * h * w, mask_center=True).to(x.device)
        attention = attention.masked_fill(mask == 0, -np.inf)
        attention = F.softmax(attention, dim=-1).masked_fill(mask == 0, 0)
        out = torch.bmm(attention, v)
        return out.transpose(-2, -1).view((b, v.shape[-1], t, h, w))


class PixelSNAILBlock(nn.Module):
    """PixelSNAIL Block as seen in [1]

    Alongside the input, the block also receives the original input
    """

    def __init__(self, in_channels=256, input_channels=3, n_res_blocks=4, key_channels=16, value_channels=128,
                 attention=True):
        super().__init__()
        self.attention = attention
        self.res_blocks = nn.Sequential(
            *[ResidualBlock(n_channels=in_channels, attention=attention) for _ in range(n_res_blocks)])

        if attention:
            self.attention_block = AttentionBlock(in_channels=in_channels + input_channels, key_channels=key_channels,
                                                  value_channels=value_channels)
            self.attention_conv = nn.Conv3d(in_channels=value_channels, out_channels=in_channels, kernel_size=1)

        self.res_conv = nn.Conv3d(in_channels=in_channels, out_channels=in_channels, kernel_size=1)
        self.out_conv = nn.Conv3d(in_channels=in_channels, out_channels=in_channels, kernel_size=1)

    def forward(self, x, original_input):
        res_out = self.res_blocks(x)
        if self.attention:
            attention_out = self.attention_block(torch.cat((res_out, original_input), dim=1))
            attention_out = elu_conv_elu(self.attention_conv, attention_out)

        res_out = elu_conv_elu(self.res_conv, res_out)
        out = res_out + (self.attention and attention_out)
        return elu_conv_elu(self.out_conv, out)


class PixelSNAIL(nn.Module):
    """The PixelSNAIL [1] model"""

    def __init__(self, attention, input_channels, n_codes, n_filters, n_res_blocks, n_snail_blocks, key_channels=None,
                 value_channels=None):
        super().__init__()
        self.in_conv = CausalConv3d(mask_center=True, in_channels=input_channels,
                                    out_channels=n_filters, kernel_size=2, padding=1)
        self.pixel_snail_blocks = nn.ModuleList([
            PixelSNAILBlock(in_channels=n_filters,
                            input_channels=input_channels,
                            n_res_blocks=n_res_blocks,
                            key_channels=key_channels,
                            value_channels=value_channels,
                            attention=attention) for _ in range(n_snail_blocks)])
        self.out = nn.Conv3d(in_channels=n_filters, out_channels=n_codes, kernel_size=1)

    def forward(self, x):
        _, _, t, h, w = x.shape
        original_input = x
        x = self.in_conv(x)[:, :, :t, :h, :w]
        for block in self.pixel_snail_blocks:
            x = x + block(x, original_input)
        return self.out(x)


# class BottomPrior(nn.Module):
#     def __init__(self, n_codes, n_filters, n_res_blocks, n_snail_blocks, n_condition_blocks, key_channels,
#                  value_channels):
#         super().__init__()
#         self.bottom = PixelSNAIL(attention=False, input_channels=n_codes * 2, n_codes=n_codes,
#                                  n_filters=n_filters, n_res_blocks=n_res_blocks,
#                                  n_snail_blocks=n_snail_blocks)

#         self.condition_stack = []

#         for _ in range(n_condition_blocks):
#             self.condition_stack.extend([
#                 nn.Conv3d(in_channels=n_codes, out_channels=n_codes, kernel_size=3, padding=1),
#                 nn.ELU(),
#                 nn.Conv3d(in_channels=n_codes, out_channels=n_codes, kernel_size=3, padding=1)
#             ])

#         self.condition_stack = nn.ModuleList(
#             [nn.Conv3d(in_channels=n_codes, out_channels=n_codes, kernel_size=3, padding=1)
#              for _ in range(n_condition_blocks)])

#     def forward(self, top_code, bottom_code):
#         condition = F.interpolate(top_code, scale_factor=2)
#         for module in self.condition_stack:
#             condition = condition + module(condition)
#         bottom_code = self.bottom(torch.cat((bottom_code, condition), dim=1))
#         return bottom_code

class BottomPrior(nn.Module):
    def __init__(self, n_codes, n_filters, n_res_blocks, n_snail_blocks, n_condition_blocks, key_channels,
                 value_channels):
        super().__init__()
        self.condition_top = nn.Sequential(
            nn.ConvTranspose3d(in_channels=n_codes, out_channels=n_codes, kernel_size=(4, 3, 3), stride=(2,1,1), padding=1),
            nn.ELU(),
            nn.ConvTranspose3d(in_channels=n_codes, out_channels=n_codes, kernel_size=(3, 4, 4), stride=(1,2,2), padding=1),
            nn.ELU(),
            nn.ConvTranspose3d(in_channels=n_codes, out_channels=n_codes, kernel_size=(3, 3, 3), stride=(1,1,1), padding=1),
        )
        self.condition_bottom = nn.Sequential(
            nn.Conv3d(in_channels=n_codes, out_channels=n_codes, kernel_size=(4, 3, 3), stride=(2,1,1), padding=1),
            nn.ELU(),
            nn.Conv3d(in_channels=n_codes, out_channels=n_codes, kernel_size=(4, 3, 3), stride=(2,1,1), padding=1),
            nn.ELU(),
            nn.Conv3d(in_channels=n_codes, out_channels=n_codes, kernel_size=(4, 3, 3), stride=(2,1,1), padding=1),
            nn.ELU(),
            nn.Conv3d(in_channels=n_codes, out_channels=n_codes, kernel_size=(4, 3, 3), stride=(2,1,1), padding=1),
        )
        self.bottom = PixelSNAIL2D(
            [32, 32],
            512,
            128,
            5,
            2,
            2,
            128,
            attention=False,
            dropout=0.1,
            n_cond_res_block=2,
            cond_res_channel=1024,
        )

    def forward(self, top_code, bottom_code):
        condition_top = self.condition_top(top_code)
        condition_bottom = self.condition_bottom(bottom_code)
        condition = torch.cat((condition_top, condition_bottom.repeat(1, 1, 18, 1, 1)), dim=1)
        condition = rearrange(condition, 'b c s h w -> (b s) c h w')
        bottom_code = rearrange(bottom_code, 'b c s h w -> (b s) c h w')
        bottom_code = self.bottom(bottom_code, condition)
        bottom_code = rearrange(bottom_code, '(b s) c h w -> b c s h w', b=top_code.shape[0], s=18)
        return bottom_code


# class BottomPrior(nn.Module):
#     def __init__(self, n_codes, n_filters, n_res_blocks, n_snail_blocks, n_condition_blocks, key_channels,
#                  value_channels):
#         super().__init__()
#         self.condition_top = nn.Sequential(
#             nn.ConvTranspose3d(in_channels=n_codes, out_channels=n_codes, kernel_size=(4, 3, 3), stride=(2,1,1), padding=1),
#             nn.ELU(),
#             nn.ConvTranspose3d(in_channels=n_codes, out_channels=n_codes, kernel_size=(3, 4, 4), stride=(1,2,2), padding=1),
#             nn.ELU(),
#             nn.ConvTranspose3d(in_channels=n_codes, out_channels=n_codes, kernel_size=(3, 3, 3), stride=(1,1,1), padding=1),
#         )
#         self.in_conv = nn.Conv3d(in_channels=n_codes, out_channels=n_filters, kernel_size=3, padding=1)
#         self.bottom = nn.ModuleList([GatedResBlock(n_codes, n_filters, n_res_blocks, n_snail_blocks, n_condition_blocks, key_channels, value_channels) for _ in range(n_condition_blocks)])
#         self.out = nn.Conv3d(in_channels=n_filters, out_channels=n_filters, kernel_size=3, padding=1),

#     def forward(self, top_code, bottom_code):
#         condition = self.condition_top(top_code)
#         bottom_code = self.in_conv(bottom_code)
#         for block in self.bottom:
#             bottom_code = block(bottom_code, condition)
#         bottom_code = self.out(bottom_code)
#         return bottom_code

class TopPrior(nn.Module):
    def __init__(self, n_codes, n_filters, n_res_blocks, n_snail_blocks, n_condition_blocks, key_channels,
                 value_channels):
        super().__init__()
        self.top = PixelSNAIL(attention=True, input_channels=n_codes, n_codes=n_codes,
                              n_filters=n_filters, n_res_blocks=n_res_blocks,
                              n_snail_blocks=n_snail_blocks, key_channels=key_channels,
                              value_channels=value_channels)

    def forward(self, top_code):
        top_code = self.top(top_code)
        return top_code

