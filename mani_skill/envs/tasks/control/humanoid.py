"""Adapted from https://github.com/google-deepmind/dm_control/blob/main/dm_control/suite/humanoid.py"""

from typing import Any, Dict, Union

import numpy as np
import sapien
import torch

from mani_skill.agents.robots.humanoid import Humanoid
from mani_skill.envs.sapien_env import BaseEnv
from mani_skill.envs.utils import randomization, rewards
from mani_skill.sensors.camera import CameraConfig
from mani_skill.utils import common, sapien_utils
from mani_skill.utils.building.ground import build_ground
from mani_skill.utils.geometry import rotation_conversions
from mani_skill.utils.registration import register_env
from mani_skill.utils.structs.pose import Pose
from mani_skill.utils.structs.types import Array, SceneConfig, SimConfig

# Height of head above which stand reward is 1.
_STAND_HEIGHT = 1.4

# Horizontal speeds above which move reward is 1.
_WALK_SPEED = 1
_RUN_SPEED = 10


@register_env("MS-HumanoidFake-v1", max_episode_steps=100)
class HumanoidEnv(BaseEnv):
    agent: Union[Humanoid]

    def __init__(self, *args, robot_uids="humanoid", **kwargs):
        super().__init__(*args, robot_uids=robot_uids, **kwargs)
        # active_links_names = [link.name for link in self.active_links]
        # print("links", self.agent.robot.links_map.keys())
        # print()
        # print("active", active_links_names)
        # print()
        # print("joints", self.agent.robot.joints_map.keys())
        # print()
        # print("active_joints", self.agent.robot.active_joints_map.keys())
        # print()
        # print("num", len(self.agent.robot.active_joints))
        # print()
        # print("all_joints", len(self.agent.robot.joints))

    @property
    def _default_sim_config(self):
        return SimConfig(
            scene_cfg=SceneConfig(
                solver_position_iterations=4, solver_velocity_iterations=1
            ),
        )

    # @property
    # def _default_sensor_configs(self):
    #     return [
    #         # replicated from xml file
    #         CameraConfig(
    #             uid="cam0",
    #             pose=sapien_utils.look_at(eye=[0, -2.8, 0.8], target=[0, 0, 0]),
    #             width=128,
    #             height=128,
    #             fov=np.pi / 4,
    #             near=0.01,
    #             far=100,
    #             mount = self.agent.robot.get_root()
    #         ),
    #     ]

    @property
    def _default_human_render_camera_configs(self):
        return [
            # replicated from xml file
            CameraConfig(
                uid="render_cam",
                pose=sapien_utils.look_at(eye=[0, 2, 2], target=[0, 0, 0]),
                width=512,
                height=512,
                fov=np.pi / 2.5,
                near=0.01,
                far=100,
            ),
        ]

    @property
    def head_height(self):
        """Returns the height of the head."""
        return self.agent.robot.links_map["head"].pose.p[:, -1]

    @property
    def torso_upright(self):
        # print("rot_mat")
        # print(self.agent.robot.links_map["torso"].pose.to_transformation_matrix())
        # print("rot_mat_zz", self.agent.robot.links_map["torso"].pose.to_transformation_matrix()[:,2,2])
        # print("rot_mat.shape", self.agent.robot.links_map["torso"].pose.to_transformation_matrix().shape)
        return self.agent.robot.links_map["torso"].pose.to_transformation_matrix()[
            :, 2, 2
        ]

    # not sure this is correct - are our pose rotations the same as mujocos?
    # test out the transpose (taking column instead of row)
    # right now, it should represnt the z axis of global in the local frame - hm, no, this should be correct
    @property
    def torso_vertical_orientation(self):
        return (
            self.agent.robot.links_map["torso"]
            .pose.to_transformation_matrix()[:, 2, :3]
            .view(-1, 3)
        )

    @property
    def extremities(self):
        torso_frame = (
            self.agent.robot.links_map["torso"]
            .pose.to_transformation_matrix()[:, :3, :3]
            .view(-1, 3, 3)
        )
        torso_pos = self.agent.robot.links_map["torso"].pose.p
        positions = []
        for side in ("left_", "right_"):
            for limb in ("hand", "foot"):
                torso_to_limb = (
                    self.agent.robot.links_map[side + limb].pose.p - torso_pos
                ).view(-1, 1, 3)
                positions.append(
                    (torso_to_limb @ torso_frame).view(-1, 3)
                )  # reverse order mult == extrems in torso frame
        return torch.stack(positions, dim=1).view(-1, 12)  # (b, 4, 3) -> (b,12)

    @property
    def center_of_mass_velocity(self):
        # """Returns the center of mass velocity of robot"""
        vels = torch.stack(
            [
                link.get_linear_velocity() * link.mass[0].item()
                for link in self.active_links
            ],
            dim=0,
        )  # (num_links, b, 3)
        com_vel = vels.sum(dim=0) / self.robot_mass  # (b, 3)
        return com_vel

    # # dm_control also includes foot pressures as state obs space
    def _get_obs_state_dict(self, info: Dict):
        return dict(
            agent=self._get_obs_agent(),  # dm control blocks out first 7 qpos, happens by default for us, includes qpos & qvel
            head_height=self.head_height,  # (b,1)
            extremities=self.extremities,  # (b, 4, 3)
            torso_vertical=self.torso_vertical_orientation,  # (b, 3)
            com_velocity=self.center_of_mass_velocity,  # (b, 3)
        )

    def compute_dense_reward(self, obs: Any, action: Array, info: Dict):
        standing = rewards.tolerance(
            self.head_height,
            lower=_STAND_HEIGHT,
            upper=float("inf"),
            margin=_STAND_HEIGHT / 4,
        )
        upright = rewards.tolerance(
            self.torso_upright,
            lower=0.9,
            upper=float("inf"),
            sigmoid="linear",
            margin=1.9,
            value_at_margin=0,
        )
        stand_reward = standing.view(-1) * upright.view(-1)
        small_control = rewards.tolerance(
            action, margin=1, value_at_margin=0, sigmoid="quadratic"
        ).mean(
            dim=-1
        )  # (b, a) -> (b)
        small_control = (4 + small_control) / 5
        horizontal_velocity = self.center_of_mass_velocity[:, :2]
        dont_move = rewards.tolerance(horizontal_velocity, margin=2).mean(
            dim=-1
        )  # (b,3) -> (b)

        return small_control.view(-1) * stand_reward * dont_move.view(-1)

    def compute_normalized_dense_reward(self, obs: Any, action: Array, info: Dict):
        max_reward = 1.0
        return self.compute_dense_reward(obs, action, info) / max_reward

    def _load_scene(self, options: dict):
        loader = self.scene.create_mjcf_loader()
        articulation_builders, actor_builders, sensor_configs = loader.parse(
            self.agent.mjcf_path
        )
        for a in actor_builders:
            a.build(a.name)

        self.ground = build_ground(self.scene, floor_width=100, altitude=0)

        self.active_links = [
            link for link in self.agent.robot.get_links() if "dummy" not in link.name
        ]
        self.robot_mass = np.sum([link.mass[0].item() for link in self.active_links])

    def _initialize_episode(self, env_idx: torch.Tensor, options: Dict):
        with torch.device(self.device):
            b = len(env_idx)
            #     # qpos sampled same as dm_control, but ensure no self intersection explicitly here
            #     random_qpos = torch.rand(b, self.agent.robot.dof[0])
            #     q_lims = self.agent.robot.get_qlimits()
            #     q_ranges = q_lims[..., 1] - q_lims[..., 0]
            #     random_qpos *= q_ranges
            #     random_qpos += q_lims[..., 0]

            #     # overwrite planar joint qpos - these are special for planar robots
            #     # first two joints are dummy rootx and rootz
            #     random_qpos[:, :2] = 0
            #     # y is axis of rotation of our planar robot (xz plane), so we randomize around it
            #     random_qpos[:, 2] = torch.pi * (2 * torch.rand(b) - 1)  # (-pi,pi)
            # self.agent.reset(self.agent.keyframes["squat"].qpos)
            pos = sapien.Pose(p=[0, 0, 0], q=[1, 0, 0, 0])
            self.agent.robot.set_root_pose(pos)
            self.agent.reset(torch.zeros(b, self.agent.robot.dof[0]))
        # @pass

    # @property  # dm_control mjc function adapted for maniskill
    # def height(self):
    #     """Returns relative height of the robot"""
    #     return (
    #         self.agent.robot.links_map["torso"].pose.p[:, -1]
    #         - self.agent.robot.links_map["foot_heel"].pose.p[:, -1]
    #     ).view(-1, 1)

    # # dm_control mjc function adapted for maniskill
    # def touch(self, link_name):
    #     """Returns function of sensor force values"""
    #     force_vec = self.agent.robot.get_net_contact_forces([link_name])
    #     force_mag = torch.linalg.norm(force_vec, dim=-1)
    #     return torch.log1p(force_mag)


# @register_env("MS-humanoidStand-v1", max_episode_steps=600)
# class humanoidStandEnv(humanoidEnv):
#     def __init__(self, *args, **kwargs):
#         super().__init__(*args, **kwargs)

#     def compute_dense_reward(self, obs: Any, action: Array, info: Dict):
#         standing = rewards.tolerance(self.height, lower=_STAND_HEIGHT, upper=2.0)
#         return standing.view(-1)

#     def compute_normalized_dense_reward(self, obs: Any, action: Array, info: Dict):
#         # this should be equal to compute_dense_reward / max possible reward
#         max_reward = 1.0
#         return self.compute_dense_reward(obs=obs, action=action, info=info) / max_reward


# @register_env("MS-humanoidHop-v1", max_episode_steps=600)
# class humanoidHopEnv(humanoidEnv):
#     def __init__(self, *args, **kwargs):
#         super().__init__(*args, **kwargs)

#     def compute_dense_reward(self, obs: Any, action: Array, info: Dict):
#         standing = rewards.tolerance(self.height, lower=_STAND_HEIGHT, upper=2.0)
#         hopping = rewards.tolerance(
#             self.subtreelinvelx,
#             lower=_HOP_SPEED,
#             upper=float("inf"),
#             margin=_HOP_SPEED / 2,
#             value_at_margin=0.5,
#             sigmoid="linear",
#         )

#         return standing.view(-1) * hopping.view(-1)

#     def compute_normalized_dense_reward(self, obs: Any, action: Array, info: Dict):
#         max_reward = 1.0
#         return self.compute_dense_reward(obs=obs, action=action, info=info) / max_reward
