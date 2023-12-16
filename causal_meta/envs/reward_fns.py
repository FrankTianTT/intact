# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import torch

from causal_meta.envs import termination_fns


def ones(obs: torch.Tensor, act: torch.Tensor, next_obs: torch.Tensor) -> torch.Tensor:
    return torch.ones(*next_obs.shape[:-1], 1).to(next_obs.device)


reward_fns_dict = {
    "ones": ones,
}
