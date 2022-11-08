from math import log
import torch
from torch import nn
from .cnn_blocks import PeriodicConv2D, ResidualBlock, Upsample

# Large based on https://github.com/labmlai/annotated_deep_learning_paper_implementations/blob/master/labml_nn/diffusion/ddpm/unet.py
# MIT License


class ResNet(nn.Module):
    def __init__(
        self,
        in_channels,
        history=1,
        hidden_channels=128,
        activation="leaky",
        out_channels=None,
        upsampling=1,
        norm: bool = True,
        dropout: float = 0.1,
        n_blocks: int = 2,
        prob_type: str = None, # parametric, mcdropout, deter (used for ensembling)
        n_samples: int = 50 # only used for mcdropout
    ) -> None:
        super().__init__()
        self.in_channels = in_channels
        if out_channels is None:
            out_channels = in_channels
        self.out_channels = out_channels
        self.hidden_channels = hidden_channels
        self.upsampling = upsampling

        if activation == "gelu":
            self.activation = nn.GELU()
        elif activation == "relu":
            self.activation = nn.ReLU()
        elif activation == "silu":
            self.activation = nn.SiLU()
        elif activation == "leaky":
            self.activation = nn.LeakyReLU(0.3)
        else:
            raise NotImplementedError(f"Activation {activation} not implemented")
        
        assert not prob_type or prob_type in ['parametric', 'mcdropout', 'deter']
        self.prob_type = prob_type
        self.n_samples = n_samples

        insize = self.in_channels * history
        # Project image into feature map
        self.image_proj = PeriodicConv2D(insize, hidden_channels, kernel_size=7, padding=3)

        blocks = []
        for i in range(n_blocks):
            blocks.append(
                ResidualBlock(
                    hidden_channels,
                    hidden_channels,
                    activation=activation,
                    norm=True,
                    dropout=dropout
                )
            )
        
        if upsampling > 1:
            n_upsamplers = int(log(upsampling, 2))
            for i in range(n_upsamplers - 1):
                blocks.append(Upsample(hidden_channels))
                blocks.append(self.activation)
            blocks.append(Upsample(hidden_channels))

        self.blocks = nn.ModuleList(blocks)

        if norm:
            self.norm = nn.BatchNorm2d(hidden_channels)
        else:
            self.norm = nn.Identity()
        out_channels = self.out_channels
        self.final = PeriodicConv2D(hidden_channels, out_channels, kernel_size=7, padding=3)
        
        if prob_type == 'parametric':
            self.final_std = PeriodicConv2D(hidden_channels, out_channels, kernel_size=7, padding=3)

    def predict(self, x):
        if len(x.shape) == 5: # history
            x = x.flatten(1, 2)
        x = self.image_proj(x)

        mcdropout = (self.prob_type == 'mcdropout')

        for m in self.blocks:
            x = m(x, mcdropout)

        pred = self.final(self.activation(self.norm(x)))

        if self.prob_type == 'parametric':
            std = self.final_std(self.activation(self.norm(x)))
            std = torch.exp(std)
            pred = Normal(pred, std)

        return pred

    def forward(self, x: torch.Tensor, y: torch.Tensor, out_variables, metric, lat):
        # B, C, H, W
        pred = self.predict(x)
        return [m(pred, y, out_variables, lat) for m in metric], x

    def rollout(self, x, y, clim, variables, out_variables, steps, metric, transform, lat, log_steps, log_days):
        if steps > 1:
            assert len(variables) == len(out_variables)

        preds = []
        for _ in range(steps):
            x = self.predict(x)
            preds.append(x)
        preds = torch.stack(preds, dim=1)
        if len(y.shape) == 4:
            y = y.unsqueeze(1)

        return [m(preds, y, clim, transform, out_variables, lat, log_steps, log_days) for m in metric], preds

    def upsample(self, x, y, out_vars, transform, metric):
        with torch.no_grad():
            pred = self.predict(x)
        return [m(pred, y, transform, out_vars) for m in metric], pred


# model = ResNet(in_channels=1, out_channels=1, upsampling=2).cuda()
# x = torch.randn((64, 1, 32, 64)).cuda()
# y = model.predict(x)
# print (y.shape)
