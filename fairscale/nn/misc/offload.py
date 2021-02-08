# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

"""
A wrapper which streams the model in and out of the GPU automatically during FW and optionally BW passes
(Can be used for inference only)
"""

from builtins import isinstance
import logging
from typing import Any, List

import torch
from torch import nn


def _split(modules: nn.Sequential, number_splits: int) -> List[List[nn.Module]]:
    number_splits = min(len(modules), number_splits)
    splits: List[List[nn.Module]] = [[] for _ in range(number_splits)]

    # Count the number of parameters per exposed layer, use that as a proxy for memory footprint
    total_number_params = sum([sum(p.numel() for p in m.parameters()) for m in modules])
    number_parameters_per_shard = total_number_params // number_splits

    current_shard = 0

    logging.info(
        f"This model has {total_number_params/1e6:.2f}M parameters, aiming for {number_parameters_per_shard/1e6:.2f}M parameters per shard"
    )

    for m in modules:
        # Number of parameters in the current shard
        current_shard_params = sum(p.numel() for sm in splits[current_shard] for p in sm.parameters())

        # This shard is big enough, point to the next one
        if (
            current_shard_params > 0
            and current_shard_params + sum(p.numel() for p in m.parameters()) > number_parameters_per_shard
            and current_shard < number_splits - 1
        ):
            current_shard += 1

        splits[current_shard].append(m)

    for i, split in enumerate(splits):
        current_shard_params = sum(p.numel() for sm in split for p in sm.parameters())
        logging.info(f"Shard {i} holds {current_shard_params/1e6:.2f}M parameters")

    return splits


class ModelShard(nn.Module):
    """
    Wrap one shard of the model, make it possible to load parameters on the 
    fly for the FW and BW pass on the given device.
    """

    def __init__(
        self, cpu_model_shard: nn.Module, device: torch.device, offload_device: torch.device, index: int,
    ):
        super().__init__()
        self.model_shard = cpu_model_shard
        self.index = index

        # Save all the parameter sizes to be able to restore them
        self.device = device
        torch.cuda.device(self.device)

        self.offload_device = offload_device

        self.model_shard.to(offload_device)
        self.cuda_stream = torch.cuda.Stream(
            device=self.device
        )  # needed to make sure load/offload really run in parallel with compute

    def forward(self, *inputs):  # type: ignore
        return self.model_shard(*inputs) if isinstance(inputs, tuple) else self.model_shard(inputs)

    def to(self, device: torch.device) -> "ModelShard":  # type: ignore
        # Make sure that the lookahead and lookback shards are not captured by this call
        self.model_shard.to(device)
        return self

    def train(self, mode: bool = True) -> "ModelShard":
        # Make sure that the lookahead and lookback shards are not captured by this call
        self.model_shard.train(mode)
        return self

    def to_device(self) -> None:
        self.model_shard.to(device=self.device, non_blocking=True)

    def forward_load(self, non_blocking: bool = True) -> None:
        with torch.cuda.stream(self.cuda_stream):
            # Restore all the parameter buffers
            self.model_shard.to(device=self.device, non_blocking=non_blocking)

    def backward_load(self, non_blocking: bool = True) -> None:
        with torch.cuda.stream(self.cuda_stream):
            self.model_shard.to(self.device, non_blocking=non_blocking)

    def forward_drop(self, non_blocking: bool = True) -> None:
        with torch.cuda.stream(self.cuda_stream):
            self.model_shard.to(self.offload_device, non_blocking=non_blocking)

    def backward_drop(self, non_blocking: bool = True) -> None:
        with torch.cuda.stream(self.cuda_stream):
            self.model_shard.to(self.offload_device, non_blocking=non_blocking)


class ShardSyncLayer(torch.autograd.Function):
    """
     The shard sync layer is a synchronization point between model shards.

     - In the forward pass, it drops parameters in the previous shard and
     loads parameters for the next shard.

     - In the backward pass, it does the reverse.

     It does not change or create any outputs at all, instead it just
     forwards the input as the output.

     NOTE: see https://pytorch.org/docs/stable/autograd.html#torch.autograd.Function
     """

    @staticmethod
    def forward(ctx: Any, inputs: Any, index: int, model_slices: Any, model_instance: Any) -> Any:  # type: ignore
        drop_index = index
        load_index = index + 1
        max_slices = len(model_slices)

        if drop_index >= 0:
            # Move shard from device to offload device.
            logging.info(f"Dropping shard {drop_index}")
            model_slices[drop_index].forward_drop()

        if load_index < max_slices:
            # Load shard from offload device to device.
            logging.info(f"Loading shard{load_index}")
            model_slices[load_index].forward_load()

        ctx.inputs = inputs
        ctx.index = index
        ctx.model_slices = model_slices
        ctx.model_instance = model_instance

        return inputs if isinstance(inputs, tuple) else (inputs,)

    @staticmethod
    def backward(ctx, *grad_outputs):  # type: ignore

        load_index = ctx.index
        drop_index = load_index + 1
        model_slices = ctx.model_slices
        model_instance = ctx.model_instance

        # TODO(anj-s): Are these redundant in the backward pass?
        if drop_index == len(model_slices):
            # Drop the last activation since it is still on the CPU
            # after the loss.backward() call.
            model_instance._activations[-1] = tuple([a.cuda() for a in list(model_instance._activations[-1])])

        if drop_index < len(model_slices):
            # Move shard from device to offload device.
            logging.info(f"Backward Dropping shard {drop_index}")
            model_slices[drop_index].backward_drop()
            model_instance._activations[drop_index] = tuple(
                [a.cpu() for a in list(model_instance._activations[drop_index])]
            )

        if load_index >= 0:
            # Load shard from offload device to device.
            logging.info(f"Backward Loading shard{load_index}")
            model_slices[load_index].backward_load()
            model_instance._activations[load_index] = tuple(
                [a.cuda() for a in list(model_instance._activations[load_index])]
            )

        # The returned variables need to mirror the forward inputs
        # TODO(anj-s): Why do we need to do this?
        if isinstance(grad_outputs, tuple):
            return grad_outputs[0], None, None, None

        return grad_outputs, None, None, None


class OffloadModel(nn.Module):
    """Implements training with optimizer state sharding and model sharding.

    This experiments with a different way to get to the full zero suite
    The model is sharded, then the normal distributed data parallel algorithm can be used on a per-model shard basis.
    Each shard is offloaded and loaded following a compute wavefront, during the forward and backward pass.

    Each model shard can be updated by a normal pytorch optimizer.

    Args:
        module (~torch.nn.Sequential): module to be parallelized
        optimizer (~torch.optim.Optimizer): optimizer to be used for training
        optimizer_params(Dict): extra parameters for the optimizer

        device (torch.device):
            device where the active model should reside

        offload_device (torch.device):
            device where the inactive model should reside

        n_slices (int):
            number of slices in which to decomppose the model
    """

    def __init__(
        self,
        model_cpu: nn.Sequential,  # hard pre-requisite for now, easier model slicing
        device: torch.device,
        offload_device: torch.device = torch.device("cpu"),
        n_slices: int = 5,
    ):
        super().__init__()

        self.device = device
        self.offload_device = offload_device

        # Slice the model into roughly equivalent sequential shards.
        splits = _split(model_cpu, n_slices)

        # List of model shards that will be placed on/off the device.
        self.model_slices: List[nn.Module] = []

        for i, split in enumerate(splits):
            # Add one model handling this slice
            self.model_slices.append(
                ModelShard(
                    cpu_model_shard=nn.Sequential(*split), device=device, offload_device=offload_device, index=i,
                )
            )

        # Expose a unified view of the slices
        self.model = torch.nn.Sequential(*self.model_slices)

        # intermediate actiavtions
        self._activations = []

    def forward(self, *inputs: Any, **_: Any) -> Any:
        shardSync = ShardSyncLayer.apply
        self._activations = []
        for index in range(-1, len(self.model_slices)):
            if index >= 0:
                # TODO(anj-s): This might be a redundant call since we have the previous
                # activation on the device already.
                self._activations[index] = tuple([a.cuda() for a in list(self._activations[index])])
                inputs = self._activations[index]
                inputs = self.model_slices[index](*inputs)
            # Call the custom autograd hooks (discard/load slices FW and BW)
            inputs = shardSync(inputs, index, self.model_slices, self)
            self._activations.append(inputs)
            if index >= 0:
                self._activations[index] = tuple([a.cpu() for a in list(self._activations[index])])

        # We don't move the last activation/output since the target is present
        # on the device.
        # TODO(anj-s): It is now a requirement that the target tensors be placed on the
        # device.
        result = self._activations[-1]
        return result[0] if len(result) == 1 else result