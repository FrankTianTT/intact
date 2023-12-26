import dataclasses
from pathlib import Path
from collections import defaultdict
from functools import partial

import hydra
import torch
from torch.nn.utils import clip_grad_norm_
from tqdm import tqdm
from hydra.core.config_store import ConfigStore

from torchrl.envs import ParallelEnv
from torchrl.modules.tensordict_module.exploration import AdditiveGaussianWrapper
from torchrl.objectives.dreamer import DreamerActorLoss, DreamerValueLoss
from torchrl.trainers.helpers.collectors import SyncDataCollector
from torchrl.trainers.helpers.envs import correct_for_frame_skip
from torchrl.trainers.helpers.logger import LoggerConfig
from torchrl.trainers.helpers.trainers import TrainerConfig
from torchrl.data.replay_buffers import TensorDictReplayBuffer, LazyMemmapStorage

from causal_meta.helpers.envs import make_dreamer_env, create_make_env_list
from causal_meta.helpers.models import make_causal_dreamer, DreamerConfig
from causal_meta.helpers.logger import build_logger
from causal_meta.objectives.causal_dreamer import CausalDreamerModelLoss

from utils import grad_norm, match_length

config_fields = [
    (config_field.name, config_field.type, config_field)
    for config_cls in (
        DreamerConfig,
        TrainerConfig,
    )
    for config_field in dataclasses.fields(config_cls)
]
Config = dataclasses.make_dataclass(cls_name="Config", fields=config_fields)
cs = ConfigStore.instance()
cs.store(name="config", node=Config)


@hydra.main(version_base="1.1", config_path="conf", config_name="main")
def main(cfg: "DictConfig"):  # noqa: F821
    if torch.cuda.is_available():
        device = torch.device(cfg.model_device)
    else:
        device = torch.device("cpu")
    print(f"Using device {device}")

    logger = build_logger(cfg, "dreamer")

    make_env_fn = partial(
        make_dreamer_env,
        variable_num=cfg.variable_num,
        state_dim_per_variable=cfg.state_dim_per_variable,
        hidden_dim_per_variable=cfg.hidden_dim_per_variable
    )
    train_make_env_list, train_oracle_context = create_make_env_list(cfg, make_env_fn, mode="meta_train")
    test_make_env_list, test_oracle_context = create_make_env_list(cfg, make_env_fn, mode="meta_test")
    torch.save(train_oracle_context, "train_oracle_context.pt")
    torch.save(test_oracle_context, "test_oracle_context.pt")

    task_num = len(train_make_env_list)
    proof_env = train_make_env_list[0]()
    world_model, model_based_env, actor_model, value_model, policy = make_causal_dreamer(
        cfg=cfg,
        proof_environment=proof_env,
        device=device,

    )

    # Losses
    world_model_loss = CausalDreamerModelLoss(
        world_model,
        lambda_kl=cfg.lambda_kl,
        lambda_reco=cfg.lambda_reco,
        lambda_reward=cfg.lambda_reward if cfg.reward_fns == "" else 0.,
        lambda_continue=cfg.lambda_continue if cfg.termination_fns == "" else 0.,
        sparse_weight=cfg.sparse_weight,
        context_sparse_weight=cfg.context_sparse_weight,
        context_max_weight=cfg.context_max_weight,
        sampling_times=cfg.sampling_times,
    )
    actor_loss = DreamerActorLoss(
        actor_model,
        value_model,
        model_based_env,
        imagination_horizon=cfg.imagination_horizon,
        discount_loss=cfg.discount_loss,
        pred_continue=cfg.pred_continue,
    )
    value_loss = DreamerValueLoss(
        value_model,
        discount_loss=cfg.discount_loss,
    )

    exploration_policy = AdditiveGaussianWrapper(
        policy,
        sigma_init=0.3,
        sigma_end=0.3,
        # annealing_num_steps=cfg.total_frames,
    ).to(device)

    collector = SyncDataCollector(
        create_env_fn=ParallelEnv(task_num, train_make_env_list),
        actor_model_explore=exploration_policy,
        total_frames=cfg.train_frames_per_task * task_num,
        frames_per_batch=cfg.frames_per_batch,
        init_random_frames=cfg.init_frames_per_task * task_num,
        device=cfg.collector_device,
        storing_device=cfg.collector_device,
    )

    # replay buffer
    buffer_size = cfg.train_frames_per_task * task_num if cfg.buffer_size == -1 else cfg.buffer_size
    replay_buffer = TensorDictReplayBuffer(
        storage=LazyMemmapStorage(max_size=buffer_size),
    )
    final_seed = collector.set_seed(cfg.seed)
    print(f"init seed: {cfg.seed}, final seed: {final_seed}")

    # optimizers
    world_model_opt = torch.optim.Adam(world_model.get_parameter("module"), lr=cfg.world_model_lr)
    context_opt = torch.optim.Adam(world_model.get_parameter("context"), lr=cfg.context_lr)
    mask_logits_opt = torch.optim.Adam(world_model.get_parameter("mask_logits"), lr=cfg.mask_logits_lr)
    actor_opt = torch.optim.Adam(actor_model.parameters(), lr=cfg.actor_value_lr)
    value_opt = torch.optim.Adam(value_model.parameters(), lr=cfg.actor_value_lr)

    # Training loop
    collected_frames = 0
    pbar = tqdm(total=cfg.train_frames_per_task * task_num)
    for frames_per_task, tensordict in enumerate(collector):
        current_frames = tensordict.get(("collector", "mask")).sum().item()
        pbar.update(current_frames)
        collected_frames += current_frames

        tensordict = match_length(tensordict, cfg.batch_length)
        tensordict = tensordict.reshape(-1, cfg.batch_length)
        replay_buffer.extend(tensordict.cpu())

        mask = tensordict.get(("collector", "mask"))
        episode_reward = tensordict.get(("next", "episode_reward"))[mask]
        done = tensordict.get(("next", "done"))[mask]
        mean_episode_reward = episode_reward[done].mean()
        logger.add_scaler("rollout/reward_mean", tensordict[("next", "reward")][mask].mean())
        logger.add_scaler("rollout/reward_std", tensordict[("next", "reward")][mask].std())
        logger.add_scaler("rollout/episode_reward_mean", mean_episode_reward)
        logger.add_scaler("rollout/action_mean", tensordict["action"][mask].mean())
        logger.add_scaler("rollout/action_std", tensordict["action"][mask].std())

        if collected_frames < cfg.init_frames_per_task * task_num:
            continue

        for j in range(cfg.optim_steps_per_batch):
            # sample from replay buffer
            sampled_tensordict = replay_buffer.sample(cfg.batch_size).to(device, non_blocking=True)

            # update world model
            if True:
                model_loss_td, sampled_tensordict = world_model_loss(
                    sampled_tensordict
                )
                loss_world_model = (
                        model_loss_td["loss_model_kl"]
                        + model_loss_td["loss_model_reco"]
                        + model_loss_td["loss_model_reward"]
                        + model_loss_td["loss_model_continue"]
                )

                loss_world_model.backward()
                clip_grad_norm_(world_model.get_parameter("module"), cfg.grad_clip)
                world_model_opt.step()
                context_opt.step()

                if do_log:
                    logger.add_scaler("world_model/total_loss", loss_world_model)
                    logger.add_scaler("world_model/grad", grad_norm(world_model_opt))
                    logger.add_scaler("world_model/kl_loss", model_loss_td["loss_model_kl"])
                    logger.add_scaler("world_model/reco_loss", model_loss_td["loss_model_reco"])
                    logger.add_scaler("world_model/reward_loss", model_loss_td["loss_model_reward"])
                    logger.add_scaler("world_model/continue_loss", model_loss_td["loss_model_continue"])
                    logger.add_scaler("world_model/mean_continue",
                                      (sampled_tensordict[("next", "pred_continue")] > 0).float().mean())
                world_model_opt.zero_grad()
            else:
                grad = world_model_loss.reinforce(sampled_tensordict)
                logits = world_model.causal_mask.mask_logits
                logits.backward(grad)
                mask_logits_opt.step()
                if do_log:
                    logger.add_scaler("causal/sparsity", (logits < 0).float().mean())
                    logger.add_scaler("causal/mean_logits", logits.mean())
                mask_logits_opt.zero_grad()

            if collected_frames >= cfg.train_agent_frames:
                # update actor network
                actor_loss_td, sampled_tensordict = actor_loss(sampled_tensordict)
                actor_loss_td["loss_actor"].backward()
                clip_grad_norm_(actor_model.parameters(), cfg.grad_clip)
                actor_opt.step()
                if do_log:
                    logger.add_scaler("actor/loss", actor_loss_td["loss_actor"])
                    logger.add_scaler("actor/grad", grad_norm(actor_opt))
                    logger.add_scaler("actor/action_mean", sampled_tensordict["action"].mean())
                    logger.add_scaler("actor/action_std", sampled_tensordict["action"].std())
                actor_opt.zero_grad()

                # update value network
                value_loss_td, sampled_tensordict = value_loss(sampled_tensordict)
                value_loss_td["loss_value"].backward()
                clip_grad_norm_(value_model.parameters(), cfg.grad_clip)
                value_opt.step()
                if do_log:
                    logger.add_scaler("value/loss", value_loss_td["loss_value"])
                    logger.add_scaler("value/grad", grad_norm(value_opt))
                    logger.add_scaler("value/target_mean", sampled_tensordict["lambda_target"].mean())
                    logger.add_scaler("value/target_std", sampled_tensordict["lambda_target"].std())
                    logger.add_scaler("value/mean_continue",
                                      (sampled_tensordict[("next", "pred_continue")] > 0).float().mean())
                value_opt.zero_grad()

        if cfg.exploration != "":
            exploration_policy.step(current_frames)
        collector.update_policy_weights_()


if __name__ == "__main__":
    main()