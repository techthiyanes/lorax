from typing import Optional

import torch
import torch.distributed
from loguru import logger
from transformers import (
    AutoConfig,
    AutoTokenizer,
)

from lorax_server.models.causal_lm import CausalLM
from lorax_server.models.custom_modeling.opt_modeling import OPTForCausalLM
from lorax_server.utils import (
    Weights,
    initialize_torch_distributed,
    weight_files,
)


class OPTSharded(CausalLM):
    def __init__(
        self,
        model_id: str,
        revision: Optional[str] = None,
        quantize: Optional[str] = None,
        compile: bool = False,
        dtype: Optional[torch.dtype] = None,
        trust_remote_code: bool = False,
    ):
        if compile:
            logger.info(f"Model {model_id} does not support CUDA graph compilation. Skipping compilation.")

        self.process_group, rank, world_size = initialize_torch_distributed()
        if torch.cuda.is_available():
            device = torch.device(f"cuda:{rank}")
            dtype = torch.float16 if dtype is None else dtype
        else:
            device = torch.device("cpu")
            dtype = torch.float32

        tokenizer = AutoTokenizer.from_pretrained(
            model_id,
            revision=revision,
            padding_side="left",
            truncation_side="left",
            trust_remote_code=trust_remote_code,
        )

        config = AutoConfig.from_pretrained(
            model_id,
            revision=revision,
            trust_remote_code=trust_remote_code,
        )
        config.quantize = quantize
        tokenizer.pad_token_id = config.pad_token_id

        torch.distributed.barrier(group=self.process_group)
        filenames = weight_files(model_id, revision=revision, extension=".safetensors")
        weights = Weights(filenames, device=device, dtype=dtype, process_group=self.process_group)
        weights._set_config(model_id, config)

        model = OPTForCausalLM(config, weights)

        torch.distributed.barrier(group=self.process_group)
        super(CausalLM, self).__init__(
            model_id=model_id,
            model=model,
            tokenizer=tokenizer,
            requires_padding=True,
            dtype=dtype,
            device=device,
            rank=rank,
            world_size=world_size,
            trust_remote_code=trust_remote_code,
        )

    def forward(self, input_ids, attention_mask, position_ids, past_key_values: Optional = None):
        outputs = self.model.forward(
            input_ids=input_ids,
            attention_mask=attention_mask,
            past_key_values=past_key_values,
            use_cache=True,
        )

        return outputs.logits, outputs.past_key_values
