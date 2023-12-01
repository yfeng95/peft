# coding=utf-8
# Copyright 2023-present the HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import operator
import re
import warnings
from dataclasses import asdict, replace
from enum import Enum
from functools import reduce
from itertools import chain

import torch
from torch import nn
from tqdm import tqdm
from transformers.pytorch_utils import Conv1D

from peft.import_utils import is_bnb_4bit_available, is_bnb_available
from peft.tuners.tuners_utils import BaseTuner, BaseTunerLayer, check_target_module_exists
from peft.utils import (
    TRANSFORMERS_MODELS_TO_LORA_TARGET_MODULES_MAPPING,
    ModulesToSaveWrapper,
    _freeze_adapter,
    _get_submodules,
    get_auto_gptq_quant_linear,
    get_quantization_config,
)

from .config import BOFTConfig
from .layer import Linear, BOFTLayer


class BOFTModel(BaseTuner):
    """
    Creates BOFT and OFT model from a pretrained transformers model. Paper:
    https://arxiv.org/abs/2311.06243
    https://arxiv.org/abs/2306.07280
    
    Args:
        model ([`transformers.PreTrainedModel`]): The model to be adapted.
        config ([`BOFTConfig`]): The configuration of the BOFT model.
        adapter_name (`str`): The name of the adapter, defaults to `"default"`.

    Returns:
        `torch.nn.Module`: The BOFT model.

    Example::

        ```py
        >>> import transformers
        >>> from transformers import AutoModelForSeq2SeqLM, BOFTConfig 
        >>> from peft import BOFTConfig, get_peft_model

        >>> config = BOFTConfig(
        ...     boft_block_size=8,
        ...     boft_block_num=args.block_num,
        ...     boft_n_butterfly_factor=args.n_butterfly_factor,
        ...     target_modules=["query", "value", "key", "output.dense", "mlp.fc1", "mlp.fc2"],
        ...     boft_dropout=args.boft_dropout,
        ...     bias="boft_only",
        ...     modules_to_save=["classifier"],
        ... )

        >>> model = transformers.Dinov2ForImageClassification.from_pretrained(
        ...     "facebook/dinov2-large",
        ...     num_labels=100,
        ... )
        >>> boft_model = get_peft_model(model, config)
        ```

    **Attributes**:
        - **model** ([`transformers.PreTrainedModel`]) -- The model to be adapted.
        - **peft_config** ([`BOFTConfig`]): The configuration of the BOFT model.
    """

    def __init__(self, model, config, adapter_name):
        super().__init__(model, config, adapter_name)

    def _check_new_adapter_config(self, config: BOFTConfig) -> None:
        """
        A helper method to check the config when a new adapter is being added.

        Raise a ValueError if there is something wrong with the config or if it conflicts with existing adapters.

        """
        # TODO: there should be a check if any of the existing adapters actually has bias != "none", or else the check
        # does not fully correspond to the error message.
        if (len(self.peft_config) > 1) and (config.bias != "none"):
            raise ValueError(
                f"{self.__class__.__name__} supports only 1 adapter with bias. When using multiple adapters, "
                "set bias to 'none' for all adapters."
            )

    @staticmethod
    def _check_target_module_exists(boft_config, key):
        return check_target_module_exists(boft_config, key)

    def _create_and_replace(
        self,
        boft_config,
        adapter_name,
        target,
        target_name,
        parent,
        **optional_kwargs,
    ):
        bias = hasattr(target, "bias") and target.bias is not None
        kwargs = {
            "boft_block_size": boft_config.boft_block_size,
            "boft_block_num": boft_config.boft_block_num,
            "boft_n_butterfly_factor": boft_config.boft_n_butterfly_factor,
            "boft_dropout": boft_config.boft_dropout,
            "fan_in_fan_out": boft_config.fan_in_fan_out,
            "init_boft_weights": boft_config.init_boft_weights,
        }
        kwargs["bias"] = bias

        # If it is not a BOFTLayer, create a new module, else update it with new adapters
        if not isinstance(target, BOFTLayer):
            new_module = self._create_new_module(boft_config, adapter_name, target, **kwargs)
            if adapter_name != self.active_adapter:
                # adding an additional adapter: it is not automatically trainable
                new_module.requires_grad_(False)
            self._replace_module(parent, target_name, new_module, target)
        else:
            target.update_layer(
                adapter_name,
                boft_config.boft_block_size,
                boft_config.boft_block_num,
                boft_config.boft_n_butterfly_factor,
                boft_config.boft_bias_fit,
                boft_config.boft_dropout,
                boft_config.init_boft_weights,
            )

    @staticmethod
    def _replace_module(parent, child_name, new_module, child):
        setattr(parent, child_name, new_module)
        # It's not necessary to set requires_grad here, as that is handled by
        # _mark_only_adapters_as_trainable

        # child layer wraps the original module, unpack it
        if hasattr(child, "base_layer"):
            child = child.base_layer

        # TODO: layers with base_layer don't need the weight to be copied, as they have a reference already
        if not hasattr(new_module, "base_layer"):
            new_module.weight = child.weight
            if hasattr(child, "bias"):
                new_module.bias = child.bias

        if getattr(child, "state", None) is not None:
            if hasattr(new_module, "base_layer"):
                new_module.base_layer.state = child.state
            else:
                new_module.state = child.state
            new_module.to(child.weight.device)

        # dispatch to correct device
        for name, module in new_module.named_modules():
            if "boft_" in name:
                module.to(child.weight.device)

    def _mark_only_adapters_as_trainable(self) -> None:
        for n, p in self.model.named_parameters():
            if "boft_" not in n:
                p.requires_grad = False

        for active_adapter in self.active_adapters:
            bias = self.peft_config[active_adapter].bias
            if bias == "none":
                continue

            if bias == "all":
                for n, p in self.model.named_parameters():
                    if "bias" in n:
                        p.requires_grad = True
            elif bias == "boft_only":
                for m in self.model.modules():
                    if isinstance(m, BOFTLayer) and hasattr(m, "bias") and m.bias is not None:
                        m.bias.requires_grad = True
            else:
                raise NotImplementedError(f"Requested bias: {bias}, is not implemented.")


    @staticmethod
    def _create_new_module(boft_config, adapter_name, target, **kwargs):
        bias = kwargs.pop("bias", False)

        if isinstance(target, torch.nn.Linear):
            in_features, out_features = target.in_features, target.out_features
            if kwargs["fan_in_fan_out"]:
                warnings.warn(
                    "fan_in_fan_out is set to True but the target module is `torch.nn.Linear`. "
                    "Setting fan_in_fan_out to False."
                )
                kwargs["fan_in_fan_out"] = boft_config.fan_in_fan_out = False
        else:
            raise ValueError(
                f"Target module {target} is not supported. "
                f"Currently, only `torch.nn.Linear` is supported."
            )
        new_module = Linear(adapter_name, in_features, out_features, bias=bias, **kwargs)

        return new_module

    def __getattr__(self, name: str):
            """Forward missing attributes to the wrapped module."""
            try:
                return super().__getattr__(name)  # defer to nn.Module's logic
            except AttributeError:
                return getattr(self.model, name)

    def get_peft_config_as_dict(self, inference: bool = False):
        config_dict = {}
        for key, value in self.peft_config.items():
            config = {k: v.value if isinstance(v, Enum) else v for k, v in asdict(value).items()}
            if inference:
                config["inference_mode"] = True
        config_dict[key] = config
        return config

    def _set_adapter_layers(self, enabled=True):
        for module in self.model.modules():
            if isinstance(module, (BaseTunerLayer, ModulesToSaveWrapper)):
                module.enable_adapters(enabled)

    def enable_adapter_layers(self):
        self._set_adapter_layers(enabled=True)

    def disable_adapter_layers(self):
        for active_adapter in self.active_adapters:
            val = self.peft_config[active_adapter].bias
            if val != "none":
                msg = (
                    f"Careful, disabling adapter layers with bias configured to be '{val}' does not produce the same "
                    "output as the the base model would without adaption."
                )
                warnings.warn(msg)
        self._set_adapter_layers(enabled=False)

    def set_adapter(self, adapter_name):
        for module in self.model.modules():
            if isinstance(module, LoraLayer):
                if module.merged:
                    warnings.warn("Adapter cannot be set when the model is merged. Unmerging the model first.")
                    module.unmerge()
                module.set_adapter(adapter_name)

    @staticmethod
    def _prepare_adapter_config(peft_config, model_config):
        if peft_config.target_modules is None:
            if model_config["model_type"] not in TRANSFORMERS_MODELS_TO_LORA_TARGET_MODULES_MAPPING:
                raise ValueError("Please specify `target_modules` in `peft_config`")
            peft_config.target_modules = set(
                TRANSFORMERS_MODELS_TO_LORA_TARGET_MODULES_MAPPING[model_config["model_type"]]
            )
        return peft_config

    def _unload_and_optionally_merge(self, merge=True, progressbar: bool = False, safe_merge: bool = False):
        key_list = [key for key, _ in self.model.named_modules() if "boft" not in key]
        desc = "Unloading " + ("and merging " if merge else "") + "model"
        for key in tqdm(key_list, disable=not progressbar, desc=desc):
            try:
                parent, target, target_name = _get_submodules(self.model, key)
            except AttributeError:
                continue
            if isinstance(target, BOFTLayer):
                bias = target.bias is not None
                new_module = torch.nn.Linear(target.in_features, target.out_features, bias=bias)
                if merge:
                    target.merge(safe_merge=safe_merge)
                self._replace_module(parent, target_name, new_module, target)

            # save any additional trainable modules part of `modules_to_save`
            if isinstance(target, ModulesToSaveWrapper):
                setattr(parent, target_name, target.modules_to_save[target.active_adapter])

        return self.model

    def delete_adapter(self, adapter_name: str):
        """
        Deletes an existing adapter.

        Args:
            adapter_name (str): Name of the adapter to be deleted.
        """
        if adapter_name not in list(self.peft_config.keys()):
            raise ValueError(f"Adapter {adapter_name} does not exist")
        del self.peft_config[adapter_name]

        key_list = [key for key, _ in self.model.named_modules() if "lora" not in key]
        for key in key_list:
            _, target, _ = _get_submodules(self.model, key)
            if isinstance(target, BOFTLayer):
                for attr in [
                    "boft_block_size",
                    "boft_block_num",
                    "boft_R",
                    "boft_s",
                    "boft_dropout",
                ]:
                    if adapter_name in getattr(target, attr):
                        getattr(target, attr).pop(adapter_name)
                if adapter_name in target.active_adapters:
                    resetting_active_adapter = (
                        list(self.peft_config.keys())[0] if len(self.peft_config) > 0 else "default"
                    )
                    warnings.warn(
                        f"Adapter {adapter_name} was active which is now deleted. Setting active adapter to {resetting_active_adapter}. "
                    )
                    target.set_adapter(resetting_active_adapter)

    def merge_and_unload(self, progressbar: bool = False, safe_merge: bool = False):
        r"""
        This method merges the BOFT layers into the base model. This is needed if someone wants to use the base model
        as a standalone model.

        Args:
            progressbar (`bool`):
                whether to show a progressbar indicating the unload and merge process
            safe_merge (`bool`):
                whether to activate the safe merging check to check if there is any potential Nan in the adapter
                weights

        """
        return self._unload_and_optionally_merge(progressbar=progressbar, safe_merge=safe_merge)

    def unload(self):
        """
        Gets back the base model by removing all the boft modules without merging. This gives back the original base
        model.
        """
        return self._unload_and_optionally_merge(merge=False)