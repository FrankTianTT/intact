import torch
from torch import nn

from causal_meta.stats.mcc import mean_corr_coef
from causal_meta.stats.metric import mutual_info_estimation


class ContextModel(nn.Module):
    def __init__(
            self,
            meta=False,
            max_context_dim=0,
            task_num=0,
            init_scale=0,
    ):
        super().__init__()
        self.meta = meta
        self.max_context_dim = max_context_dim
        self.task_num = task_num
        self.init_scale = init_scale

        init_context_hat = torch.randn(task_num, max_context_dim) * init_scale
        self.context_hat = torch.nn.Parameter(init_context_hat)

    def reset(self, task_num=None):
        device = self.context_hat.device
        self.task_num = task_num or self.task_num

        init_context_hat = torch.randn(self.task_num, self.max_context_dim) * self.init_scale
        self.context_hat.data = init_context_hat.to(device)

    def extra_repr(self):
        if self.meta:
            return 'max_context_dim={}, task_num={}'.format(
                self.max_context_dim,
                self.task_num,
            )
        else:
            return ""

    def forward(self, idx=None):
        if idx is None:
            assert not self.meta, "idx should not be None when meta is True"
            return torch.empty(0).to(self.context_hat.device)

        assert idx.shape[-1] == 1, "last dim of idx should be 1, got {}".format(idx.shape)
        return self.context_hat[idx[..., 0]]

    def get_mutual_info(self, idx, valid_context_idx=None, reduction='mean'):
        context_hat = self(idx)
        if valid_context_idx is not None:
            context_hat = context_hat[:, valid_context_idx]
        return mutual_info_estimation(context_hat, reduction=reduction)

    def get_mcc(self, context_gt, valid_idx=None, return_permutation=True, method="kernel"):
        if isinstance(context_gt, torch.Tensor):
            context_gt = context_gt.detach().cpu().numpy()

        if valid_idx is None:
            context_hat = self.context_hat.detach().cpu().numpy()
        else:
            context_hat = self.context_hat[:, valid_idx].detach().cpu().numpy()

        mcc, permutation = mean_corr_coef(context_gt, context_hat, return_permutation=True, method=method)

        if return_permutation:
            return mcc, permutation, context_hat
        else:
            return mcc


def test_context_model():
    max_context_dim = 10
    task_num = 100
    batch_size = 32
    x_size = 4

    context_model = ContextModel(meta=False)
    x = torch.randn(batch_size, x_size)
    x = torch.cat([x, context_model(None)], dim=-1)
    assert x.shape == (batch_size, x_size)

    context_model = ContextModel(meta=True, max_context_dim=max_context_dim, task_num=task_num)
    idx = torch.randint(0, task_num, (batch_size, 1))
    x = torch.randn(batch_size, x_size)
    x = torch.cat([x, context_model(idx)], dim=-1)
    assert x.shape == (batch_size, x_size + max_context_dim)


if __name__ == '__main__':
    test_context_model()