from intact.utils.envs.mdp_env import make_mdp_env


def test_make_mdp_env():
    env = make_mdp_env("MyCartPole-v0")
    assert env is not None
    env.close()
