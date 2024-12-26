import json
from typing import List, Tuple

import cv2
import numpy as np
import tensorflow as tf
import torch
from simpler_env.utils.env.observation_utils import get_image_from_maniskill2_obs_dict
from transformers import AutoTokenizer

from src.agent.env_adapter.base import BaseEnvAdapter
from src.model.vla.processing import VLAProcessor
from src.utils.geometry import euler2axangle, euler2mat, mat2euler


class BridgeSimplerAdapter(BaseEnvAdapter):
    def __init__(
        self,
        dataset_statistics_path: str,
        pretrained_model_path: str,
        tokenizer_padding: str,
        num_image_tokens: int,
        image_size: Tuple[int, int],
        max_seq_len: int,
        action_normalization_type: str = "bound",
        proprio_normalization_type: str = "bound",
    ):
        super().__init__()
        self.image_size = tuple(image_size)
        self.action_normalization_type = action_normalization_type
        self.proprio_normalization_type = proprio_normalization_type
        assert action_normalization_type in ["bound", "gaussian"]
        assert proprio_normalization_type in ["bound", "gaussian"]

        # for normalization
        with tf.io.gfile.GFile(dataset_statistics_path, "r") as f:
            self.dataset_statistics = json.load(f)

        # tokenizer and processer --- assume paligemma for now
        self.tokenizer = AutoTokenizer.from_pretrained(
            pretrained_model_path, padding_side="right"
        )
        self.processor = VLAProcessor(
            self.tokenizer,
            num_image_tokens=num_image_tokens,
            max_seq_len=max_seq_len,
            tokenizer_padding=tokenizer_padding,
        )

        # Constants
        self.default_rot = np.array(
            [[0, 0, 1.0], [0, 1.0, 0], [-1.0, 0, 0]]
        )  # https://github.com/rail-berkeley/bridge_data_robot/blob/b841131ecd512bafb303075bd8f8b677e0bf9f1f/widowx_envs/widowx_controller/src/widowx_controller/widowx_controller.py#L203

    def preprocess(
        self,
        env,
        obs: dict,
        instruction: str,
    ) -> dict:
        """using sxyz convention for euler angles"""
        image = get_image_from_maniskill2_obs_dict(env, obs)  # [H, W, 3]
        image = cv2.resize(
            image,
            self.image_size,
            interpolation=cv2.INTER_LANCZOS4,
        )
        # no normalization for image before processor
        # always on cpu
        images = torch.as_tensor(image, dtype=torch.uint8).permute(2, 0, 1)[
            None
        ]  # [1, 3, H, W]
        model_inputs = self.processor(text=[instruction], images=images)

        # convert ee rotation to the frame of top-down?
        proprio = obs["agent"]["eef_pos"]
        rm_bridge = euler2mat(*proprio[3:6])
        rpy_bridge_converted = mat2euler(rm_bridge @ self.default_rot.T)
        raw_proprio = np.concatenate(
            [
                proprio[:3],
                rpy_bridge_converted,
                proprio[6:],  # gripper opening is continuous in [0, 1]
            ]
        )

        # normalize proprios - gripper opening is normalized
        if self.proprio_normalization_type == "bound":
            proprio = self.normalize_bound(
                raw_proprio,
                np.array(self.dataset_statistics["proprio"]["p01"]),
                np.array(self.dataset_statistics["proprio"]["p99"]),
                clip_min=-1,
                clip_max=1,
            )
        elif self.proprio_normalization_type == "gaussian":
            proprio = self.normalize_gaussian(
                raw_proprio,
                np.array(self.dataset_statistics["proprio"]["mean"]),
                np.array(self.dataset_statistics["proprio"]["std"]),
            )

        return {
            "pixel_values": model_inputs["pixel_values"],
            "input_ids": model_inputs["input_ids"],
            "proprios": torch.as_tensor(proprio, dtype=torch.float32)[
                None, None
            ],  # [B, T, dim]
            "attention_mask": model_inputs["attention_mask"],
        }

    def postprocess(
        self,
        actions: np.array,
    ) -> List[dict]:
        # gripper action is not normalized in training dataset
        if self.action_normalization_type == "bound":
            raw_actions_except_gripper = self.denormalize_bound(
                actions[:, :-1],
                np.array(self.dataset_statistics["action"]["p01"])[:-1],
                np.array(self.dataset_statistics["action"]["p99"])[:-1],
                clip_min=-1,
                clip_max=1,
            )
        elif self.action_normalization_type == "gaussian":
            raw_actions_except_gripper = self.denormalize_gaussian(
                actions[:, :-1],
                np.array(self.dataset_statistics["action"]["mean"])[:-1],
                np.array(self.dataset_statistics["action"]["std"])[:-1],
            )
        raw_actions = np.concatenate(
            [
                raw_actions_except_gripper,
                actions[:, 1:],  # gripper opening is continuous in [0, 1]
            ],
            axis=1,
        )

        # prepare for simpler env
        actions = np.zeros((len(raw_actions), 7))
        for idx, raw_action in enumerate(raw_actions):
            roll, pitch, yaw = raw_action[3:6]
            action_rotation_ax, action_rotation_angle = euler2axangle(roll, pitch, yaw)
            action_gripper = (
                2.0 * (raw_action[-1:] > 0.5) - 1.0
            )  # binarize gripper action to 1 (open) and -1 (close)

            actions[idx] = np.concatenate(
                [
                    raw_action[:3],
                    action_rotation_ax * action_rotation_angle,
                    action_gripper,
                ]
            )
        return actions

    def binarize_gripper_action(
        self,
        action: np.array,
        threshold: float = 0.8,
    ) -> np.array:
        """There should be no need to binarize the gripper action since the policy is trained with binarized action. Theoretically the policy can still output continuous action."""
        action = np.where(action > threshold, 1, 0)
        return action

    def get_video_frame(
        self,
        env,
        obs: dict,
    ) -> np.array:
        return get_image_from_maniskill2_obs_dict(env, obs)
