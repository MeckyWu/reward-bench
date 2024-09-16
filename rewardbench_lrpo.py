# Copyright 2023 AllenAI. All rights reserved.
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

# Run RewardBench (evaluate any reward model on any dataet)

import argparse
import json
import logging
import os
import sys
from typing import Optional, Union

import numpy as np
import torch
import transformers
from accelerate import Accelerator
from accelerate.logging import get_logger
from tqdm import tqdm
from transformers import AutoTokenizer

from rewardbench import (
    DPO_MODEL_CONFIG,
    REWARD_MODEL_CONFIG,
    check_tokenizer_chat_template,
    load_preference_dataset,
)
from rewardbench.models.pipeline import LowRankBenchPipeline

from transformers import LlamaForSequenceClassification

class LlamaForPreferencePrediction(LlamaForSequenceClassification):
    def __init__(self, config, num_pref_rank=1):
        self.num_pref_rank = num_pref_rank
        config.num_labels = 2 * self.num_pref_rank
        super().__init__(config)
    
    def forward(
        self,
        input_ids: Optional[torch.LongTensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values = None, # type: ignore
        inputs_embeds: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ):
        seq_cls_outputs = super().forward(
            input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=True,
        )
        all_logits = seq_cls_outputs['logits']
        logits_u = all_logits[:, :self.num_pref_rank]
        logits_v = all_logits[:, self.num_pref_rank:]

        if not return_dict:
            output = (logits_u, logits_v) + seq_cls_outputs[1:]
            return output

        seq_cls_outputs['logits_u'] = logits_u
        seq_cls_outputs['logits_v'] = logits_v

        return seq_cls_outputs


def main():
    parser = argparse.ArgumentParser(description="Evaluate a reward model.")

    # core args
    parser.add_argument("--dataset", type=str, default="allenai/reward-bench", help="The dataset to evaluate on.")
    parser.add_argument("--split", type=str, default=None, help="The split to evaluate on.")
    parser.add_argument("--model", type=str, required=True, help="The model to evaluate.")
    parser.add_argument("--ref_model", type=str, default=None, help="The reference model to compare against.")
    parser.add_argument("--tokenizer", type=str, default=None, help="The tokenizer to use (defaults to model).")
    parser.add_argument(
        "--chat_template",
        type=str,
        default=None,
        help="The chat template to use (defaults to from tokenizer, from chattemplate).",
    )
    parser.add_argument(
        "--not_quantized", action="store_true", help="disable quantization for models that are quantized by default"
    )
    # inference args
    parser.add_argument("--batch_size", type=int, default=8, help="The batch size to use.")
    parser.add_argument("--max_length", type=int, default=512, help="The max length to use.")

    # system args
    parser.add_argument("--load_json", action="store_true", default=False, help="Load dataset as json.")
    parser.add_argument("--trust_remote_code", action="store_true", default=False, help="Trust remote code.")
    parser.add_argument("--debug", action="store_true", default=False, help="Debug mode.")
    parser.add_argument("--output_dir", type=str, default="results/", help="The output directory to save results.")
    parser.add_argument("--save_all", action="store_true", default=False, help="Save all results.")
    parser.add_argument(
        "--force_truncation", action="store_true", default=False, help="Force truncation (for if model errors)."
    )
    args = parser.parse_args()

    ###############
    # Setup logging
    ###############
    accelerator = Accelerator()
    current_device = accelerator.process_index

    logger = get_logger(__name__)
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    log_level = logging.INFO
    logger.setLevel(log_level)
    transformers.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.enable_default_handler()
    transformers.utils.logging.enable_explicit_format()

    logger.info(f"Running reward model on {args.model} with chat template {args.chat_template}")
    if args.trust_remote_code:
        logger.info("Loading model with Trust Remote Code")

    # basic checks from config
    if args.ref_model:
        is_dpo = True
        MODEL_CONFIGS = DPO_MODEL_CONFIG
        assert args.model != args.ref_model, "policy and reference model should be different"
        from trl.trainer.utils import DPODataCollatorWithPadding

        from rewardbench import DPOInference
    else:
        is_dpo = False
        MODEL_CONFIGS = REWARD_MODEL_CONFIG

    if args.chat_template:
        from fastchat.conversation import get_conv_template

        conv = get_conv_template(args.chat_template)
    else:
        conv = None

    if args.model in MODEL_CONFIGS:
        config = MODEL_CONFIGS[args.model]
    else:
        config = MODEL_CONFIGS["default"]
    logger.info(f"Using reward model config: {config}")

    # Default entries
    # "model_builder": AutoModelForSequenceClassification.from_pretrained,
    # "pipeline_builder": pipeline,
    # "quantized": True,
    # "custom_dialogue": False,
    # "model_type": "Seq. Classifier"

    quantized = config["quantized"]  # only Starling isn't quantized for now
    # if llama-3 in name, switch quantized to False (severely degrades performance)
    if (
        ("llama-3" in args.model)
        or ("Llama3" in args.model)
        or ("Llama-3" in args.model)
        or ("LLaMA3" in args.model)
        or args.not_quantized
    ):
        quantized = False
        logger.info(f"Disabling quantization for llama-3 or override flag (--not_quantized: {args.not_quantized})")
    custom_dialogue = config["custom_dialogue"]
    pipeline_builder = LowRankBenchPipeline
    _ = config["model_type"]
    if custom_dialogue:
        raise NotImplementedError("Custom dialogue not implemented yet for simpler data formatting.")

    model_builder = LlamaForPreferencePrediction.from_pretrained

    #########################
    # load dataset
    #########################
    logger.info("*** Load dataset ***")
    tokenizer_path = args.tokenizer if args.tokenizer else args.model
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, trust_remote_code=args.trust_remote_code)
    if args.dataset == "allenai/reward-bench":
        logger.info("Running core eval dataset.")
        from rewardbench import load_eval_dataset
        from rewardbench.constants import EXAMPLE_COUNTS, SUBSET_MAPPING
        from rewardbench.utils import calculate_scores_per_section

        # primary set compiles slightly more information
        dataset, subsets = load_eval_dataset(
            core_set=True,
            conv=conv,
            custom_dialogue_formatting=False,
            tokenizer=tokenizer,
            logger=logger,
            keep_columns=["text_chosen", "text_rejected", "prompt"],
        )
    else:
        dataset = load_preference_dataset(
            args.dataset, split=args.split, json=args.load_json, tokenizer=tokenizer, conv=conv
        )

    if args.debug:
        dataset = dataset.select(range(10))

    logger.info("*** Load reward model ***")

    ############################
    # Load DPO model pipeline
    ############################
    if is_dpo:
        raise NotImplementedError
        
    ############################
    # Load classifier model pipeline
    ############################
    else:

        # padding experiments for determinism
        tokenizer.padding_side = "left"
        truncation = False
        if args.force_truncation:
            truncation = True
            tokenizer.truncation_side = "left"

        reward_pipeline_kwargs = {
            "batch_size": args.batch_size,  # eval_args.inference_batch_size,
            "truncation": truncation,
            "padding": True,
            "max_length": args.max_length,
            "function_to_apply": "none",  # Compute raw logits
            "return_token_type_ids": False,
        }
        if quantized:
            model_kwargs = {
                "load_in_8bit": True,
                "device_map": {"": current_device},
                "torch_dtype": torch.float16 if torch.cuda.is_available() else None,
            }
        else:
            # note, device map auto does not work for quantized models
            model_kwargs = {"device_map": "auto"}

        model = model_builder(args.model, **model_kwargs, trust_remote_code=args.trust_remote_code)
        reward_pipe = pipeline_builder(
            "text-classification",  # often not used
            model=model,
            tokenizer=tokenizer,
        )

        # set pad token to eos token if not set
        if reward_pipe.tokenizer.pad_token_id is None:
            reward_pipe.model.config.pad_token_id = reward_pipe.tokenizer.eos_token_id
            reward_pipe.tokenizer.pad_token_id = reward_pipe.tokenizer.eos_token_id
        # For models whose config did not contains `pad_token_id`
        if reward_pipe.model.config.pad_token_id is None:
            reward_pipe.model.config.pad_token_id = reward_pipe.tokenizer.pad_token_id

        # if using fastchat template (no template in tokenizer), make the RM tokenizer output an EOS token
        if not check_tokenizer_chat_template(tokenizer):
            reward_pipe.tokenizer.add_eos_token = True

        dataloader = torch.utils.data.DataLoader(
            dataset,
            batch_size=args.batch_size,
            shuffle=False,
            drop_last=False,
        )

        model = accelerator.prepare(reward_pipe.model)
        reward_pipe.model = model

    ############################
    # Run inference
    ############################

    results = []
    scores_margin = []
    for step, batch in enumerate(tqdm(dataloader, desc="RM batch steps")):
        logger.info(f"RM inference step {step}/{len(dataloader)}")


        u_chosen, v_chosen = reward_pipe(batch["text_chosen"], **reward_pipeline_kwargs)
        u_rejected, v_rejected = reward_pipe(batch["text_rejected"], **reward_pipeline_kwargs)
        logits = torch.sum(u_chosen * v_rejected - u_rejected * v_chosen, dim=1)

        # for each item in batch, record 1 if chosen > rejected
        # extra score from dict within batched results (e.g. logits)
        # [{'label': 'LABEL_1', 'score': 0.6826171875},... ]
        # score_chosen_batch = rewards_chosen.cpu().numpy().tolist()
        # score_rejected_batch = rewards_rejected.cpu().numpy().tolist()
        logits = logits.cpu().numpy().tolist()

        # log results
        [
            results.append(1) if logit > 0 else results.append(0)
            for logit in logits
        ]
        scores_margin.extend(logits)

    ############################
    # compile scores
    ############################
    # calculate accuracy
    accuracy = sum(results) / len(results)
    logger.info(f"Results: {accuracy}, on {len(results)} prompts")

    # compute mean and std of scores, chosen and rejected, then margin between them
    # logger.info(f"Mean chosen: {np.mean(scores_chosen)}, std: {np.std(scores_chosen)}")
    # logger.info(f"Mean rejected: {np.mean(scores_rejected)}, std: {np.std(scores_rejected)}")
    logger.info(f"Mean margin: {np.mean(np.array(scores_margin))}")

    if args.dataset == "allenai/reward-bench":
        out_dataset = dataset.add_column("results", results)
        if args.debug:
            subsets = subsets[:10]
        out_dataset = out_dataset.add_column("subsets", subsets)
        out_dataset = out_dataset.to_pandas()  # I know this is meh

        results_grouped = {}
        present_subsets = np.unique(out_dataset["subsets"])
        for subset in present_subsets:
            subset_dataset = out_dataset[out_dataset["subsets"] == subset]
            num_correct = sum(subset_dataset["results"])
            num_total = len(subset_dataset["results"])
            logger.info(f"{subset}: {num_correct}/{num_total} ({num_correct/num_total})")
            results_grouped[subset] = num_correct / num_total

        results_section = calculate_scores_per_section(EXAMPLE_COUNTS, SUBSET_MAPPING, results_grouped)
        logger.info(f"Results: {results_section}")

    ############################
    # compile scores
    ############################
    # save score in json to args.output_dir + args.model + ".json"
    output_path = args.output_dir + args.model + ".json"
    dirname = os.path.dirname(output_path)
    os.makedirs(dirname, exist_ok=True)

    # remove old data
    if os.path.exists(output_path):
        os.remove(output_path)

    with open(output_path, "w") as f:
        json.dump(
            {
                "accuracy": accuracy,
                "num_prompts": len(results),
                "model": args.model,
                "ref_model": args.ref_model,
                "tokenizer": tokenizer_path,
                "chat_template": args.chat_template,
                "extra_results": results_grouped if args.dataset == "allenai/reward-bench" else None,
                "section_results": results_section if args.dataset == "allenai/reward-bench" else None,
                "section_results_mean": sum(results_section.values()) / len(results_section),
            },
            f,
        )

    # if save_all is passed, save a large jsonl with all scores_chosen, scores_rejected
    if args.save_all:
        output_path = args.output_dir + args.model + "_all.jsonl"
        dirname = os.path.dirname(output_path)
        os.makedirs(dirname, exist_ok=True)

        # remove old data
        if os.path.exists(output_path):
            os.remove(output_path)

        with open(output_path, "w") as f:
            for logit in scores_margin:
                f.write(json.dumps({"reward_margin": logit}) + "\n")


if __name__ == "__main__":
    main()
