import torch
import torch.nn as nn
import torch.nn.functional as F

from intact.modules.models.base_world_model import BaseWorldModel
from intact.modules.utils import build_mlp


class PlainMDPWorldModel(BaseWorldModel):
    def __init__(
        self,
        obs_dim,
        action_dim,
        meta=False,
        max_context_dim=10,
        task_num=100,
        residual=True,
        learn_obs_var=True,
        hidden_dims=None,
        log_var_bounds=(-10.0, 0.5),
    ):
        """World-model class for environment learning with causal discovery.

        :param obs_dim: number of observation dimensions
        :param action_dim: number of action dimensions
        :param meta: whether to use envs-RL
        :param max_context_dim: number of context dimensions, used for envs-RL, set to 0 if normal RL
        :param task_num: number of tasks, used for envs-RL, set to 0 if normal RL
        :param residual: whether to use residual connection for transition model
        :param hidden_dims: hidden dimensions for transition model
        :param log_var_bounds: bounds for log_var of gaussian nll loss
        """

        self.hidden_dims = hidden_dims or [256, 256]
        self.log_var_bounds = log_var_bounds
        self._learn_obs_var = learn_obs_var

        super().__init__(
            obs_dim=obs_dim,
            action_dim=action_dim,
            meta=meta,
            max_context_dim=max_context_dim,
            task_num=task_num,
            residual=residual,
        )

    @property
    def learn_obs_var(self):
        return self._learn_obs_var

    def build_nets(self):
        mlp = build_mlp(
            input_dim=self.all_input_dim,
            output_dim=self.output_dim * 2,
            hidden_dims=self.hidden_dims,
            extra_dims=None,
            activate_name="SiLU",
        )

        return nn.ModuleDict(dict(mlp=mlp))

    def get_log_var(self, log_var):
        min_log_var, max_log_var = self.log_var_bounds
        log_var = max_log_var - F.softplus(max_log_var - log_var)
        log_var = min_log_var + F.softplus(log_var - min_log_var)
        return log_var

    def get_outputs(self, mean, log_var, observation, batch_size):
        next_obs_mean, reward_mean, terminated = (
            mean[:, :-2],
            mean[:, -2:-1],
            mean[:, -1:],
        )
        if self.learn_obs_var:
            next_obs_log_var = self.get_log_var(log_var[:, :-2])
            reward_log_var = self.get_log_var(log_var[:, -2:-1])
        else:
            next_obs_log_var = torch.zeros_like(log_var[:, :-2])
            reward_log_var = torch.zeros_like(log_var[:, -2:-1])

        next_obs_mean = next_obs_mean.reshape(*batch_size, self.obs_dim)
        if self.residual:
            next_obs_mean = observation + next_obs_mean
        next_obs_log_var = next_obs_log_var.reshape(*batch_size, self.obs_dim)
        reward_mean = reward_mean.reshape(*batch_size, 1)
        reward_log_var = reward_log_var.reshape(*batch_size, 1)
        terminated = terminated.reshape(*batch_size, 1)

        return (
            next_obs_mean,
            next_obs_log_var,
            reward_mean,
            reward_log_var,
            terminated,
        )

    def forward(self, observation, action, idx=None):
        context = self.context_model(idx)

        inputs = torch.cat([observation, action, context], dim=-1)
        batch_shape, dim = inputs.shape[:-1], inputs.shape[-1]

        mean, log_var = self.nets["mlp"](inputs.reshape(-1, dim)).chunk(
            2, dim=-1
        )
        return self.get_outputs(mean, log_var, observation, batch_shape)
