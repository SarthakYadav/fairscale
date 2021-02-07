# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
# Based on https://github.com/pytorch/tutorials/blob/master/beginner_source/transformer_tutorial.py
# Apply CPU offload and problem sharding to a big transformer model

import argparse
import logging
import time

import torch
import torch.nn as nn
from torch.utils.data.dataloader import DataLoader
from torchvision.datasets import FakeData
from torchvision.transforms import ToTensor

OPTIM = torch.optim.SGD
LR = 1e-3

from fairscale.nn.misc.offload import OffloadWrapperExperimental


def train(args: argparse.Namespace):
    logging.basicConfig(level=logging.INFO)
    device = torch.device("cuda")
    torch.cuda.set_device(0)
    torch.manual_seed(5)

    # Setup the problem
    model = torch.nn.Sequential(
        torch.nn.Linear(args.inputs * args.inputs, args.hidden, bias=False),
        *([torch.nn.Linear(args.hidden, args.hidden) for _ in range(args.layers)]),
        torch.nn.Linear(args.hidden, args.outputs, bias=False),
    ).cpu()

    # Optim loop
    criterion = nn.CrossEntropyLoss()
    if args.offload:
        logging.info("Using sharded offloading for training")
        model = OffloadWrapperExperimental(
            model_cpu=model, device=device, offload_device=torch.device("cpu"), n_slices=args.slices,
        )  # type: ignore

    else:
        logging.info("Using Pytorch for training")
        model = model.to(torch.device("cuda"))

    optimizer = OPTIM(model.parameters(), lr=LR)

    transform = ToTensor()
    dataloader = DataLoader(
        FakeData(image_size=(1, args.inputs, args.inputs), num_classes=args.outputs, transform=transform),
        batch_size=args.batch_size,
    )

    def train_epoch(args):
        model.train()
        iter_count = 2
        for batch_inputs, batch_outputs in dataloader:
            batch_inputs, batch_outputs = batch_inputs.to("cuda"), batch_outputs.to("cuda")
            iter_count -= 1
            start = time.time_ns()
            with torch.autograd.profiler.profile(use_cuda=True, profile_memory=True) as prof:
                optimizer.zero_grad()
                inputs = batch_inputs.reshape(-1, args.inputs * args.inputs)
                output = model(inputs)
                # make_dot(output, dict(model.named_parameters())).render("attached_mine", format="png")
                loss = criterion(output, target=batch_outputs)
                # print(f"loss {loss.item()}")
                loss.backward()
                optimizer.step()
            prof.export_chrome_trace("/tmp/mpi_prof")
            print(prof.key_averages().table())

            print(
                "Loss {:.2f} - throughput {:.2f}fps".format(
                    loss.item(), args.batch_size / (time.time_ns() - start) * 10 ** 9
                )
            )

    train_epoch(args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test the CPU offload + sharding with a Transformer training")
    parser.add_argument("--epochs", action="store", default=1, type=int)
    parser.add_argument("--batch_size", action="store", default=1, type=int)
    parser.add_argument("--inputs", action="store", help="The dimension of the inputs", default=100, type=int)
    parser.add_argument("--hidden", action="store", help="The dimension of the hidden state", default=10000, type=int)
    parser.add_argument("--layers", action="store", help="he number of hidden layers", default=20, type=int)
    parser.add_argument("--outputs", action="store", help="The number of predicted classes", default=5, type=int)

    parser.add_argument("--offload", action="store_true", default=False)
    parser.add_argument("--slices", action="store", default=3, type=int)

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    logging.info("Benchmark arguments: %s" % args)
    logging.info("Starting training")
    train(args)
