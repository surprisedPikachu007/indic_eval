# MIT License

# Copyright (c) 2024 Adithya S K

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import os
import random
import shutil
from contextlib import nullcontext
from datetime import timedelta

import numpy as np

from indic_eval.evaluator import evaluate, make_results_table
from indic_eval.logging.evaluation_tracker import EvaluationTracker
from indic_eval.logging.hierarchical_logger import hlog, hlog_warn, htrack, htrack_block
from indic_eval.models.model_config import EnvConfig, create_model_config
from indic_eval.models.model_loader import load_model
from indic_eval.tasks.lighteval_task import LightevalTask, create_requests_from_tasks
from indic_eval.tasks.registry import Registry, taskinfo_selector
from indic_eval.utils import is_accelerate_available, is_tgi_available, is_valid_email, print_indic_eval_text_art
from indic_eval.utils_parallelism import test_all_gather


if not is_accelerate_available() and not is_tgi_available():
    hlog_warn("Using either accelerate or text-generation to run this script is advised.")

TOKEN = os.getenv("HF_TOKEN")
CACHE_DIR = os.getenv("HF_HOME")

if is_accelerate_available():
    from accelerate import Accelerator, InitProcessGroupKwargs

    accelerator = Accelerator(kwargs_handlers=[InitProcessGroupKwargs(timeout=timedelta(seconds=3000))])
else:
    accelerator = None


@htrack()
def main(args):
    env_config = EnvConfig(token=TOKEN, cache_dir=args.cache_dir)
    evaluation_tracker = EvaluationTracker(hub_results_org=args.results_org, token=TOKEN)
    evaluation_tracker.general_config_logger.log_args_info(
        args.num_fewshot_seeds, args.override_batch_size, args.max_samples, args.job_id
    )

    if args.max_samples:
        hlog(
            "WARNING: --max_samples WAS SET. THESE NUMBERS ARE ONLY PARTIAL AND SHOULD NOT BE USED FOR COMPARISON UNLESS YOU KNOW WHAT YOU ARE DOING."
        )

    with htrack_block("Starting evaluation"):
        print_indic_eval_text_art()
        
    with htrack_block("Test all gather"):
        test_all_gather(accelerator)

    with htrack_block("Creating model configuration"):
        model_config = create_model_config(args=args, accelerator=accelerator)

    with htrack_block("Model loading"):
        with accelerator.main_process_first() if accelerator is not None else nullcontext():
            model, model_info = load_model(config=model_config, env_config=env_config)
            evaluation_tracker.general_config_logger.log_model_info(model_info)

    with htrack_block("Tasks loading"):
        with accelerator.main_process_first() if accelerator is not None else nullcontext():
            if args.tasks == "indic_llm_leadeboard":
                if is_valid_email(args.push_to_leaderboard):
                    hlog(f"{args.push_to_leaderboard} is the email id through which we will contact you for verification")
                else:
                    raise ValueError("The email you have specified for --push_to_leaderboard is not valid")      
                if args.language in ["kannada", "hindi", "tamil", "telugu", "gujarati", "marathi", "malayalam","english"]:
                    hlog(f"{args.language} langauge selected")
                    # args.tasks = f"indiceval|ARC-Easy:{args.language}|5|0,indiceval|ARC-Challenge:{args.language}|10|0,indiceval|Hellaswag:{args.language}|5|0,indiceval|Boolq:{args.language}|5|0,indiceval|MMLU:{args.language}|5|0"
                    args.tasks = ",".join(f"indiceval|{benchmark}:{args.language}|5|0" for benchmark in ["ARC-Easy", "ARC-Challenge", "Hellaswag", "Boolq", "MMLU"])
                else:
                    raise ValueError(f"Invalid language: {args.language}. Supported languages are kannada, hindi, tamil, telugu, gujarati, marathi, and malayalam.")
                
            else:
                task_names_list, few_shots_dict = taskinfo_selector(args.tasks)
            task_dict = Registry(cache_dir=env_config.cache_dir).get_task_dict(
                task_names_list, custom_tasks=args.custom_tasks
            )
            LightevalTask.load_datasets(task_dict.values(), args.dataset_loading_processes)

            evaluation_tracker.task_config_logger.log(task_dict)

            hlog("Loading documents, and requests")
            requests, docs = create_requests_from_tasks(
                task_dict=task_dict,
                fewshot_dict=few_shots_dict,
                num_fewshot_seeds=args.num_fewshot_seeds,
                lm=model,
                max_samples=args.max_samples,
                evaluation_tracker=evaluation_tracker,
                use_chat_template=args.use_chat_template,
                system_prompt=args.system_prompt,
            )

    with htrack_block("Setting seeds and waiting for all processes"):
        hlog(f"setting seed to {1234} for random and numpy")
        random.seed(1234)
        np.random.seed(1234)
        if accelerator is not None:
            accelerator.wait_for_everyone()

    with htrack_block("Evaluation"):
        hlog(f"Evaluate on {len(task_names_list)} tasks.")
        evaluation_tracker = evaluate(
            lm=model,
            requests_dict=requests,
            docs=docs,
            task_dict=task_dict,
            override_bs=args.override_batch_size,
            evaluation_tracker=evaluation_tracker,
        )

    if accelerator.is_main_process if accelerator is not None else nullcontext():
        with htrack_block("Compiling and saving results"):
            evaluation_tracker.general_config_logger.log_end_time()
            evaluation_tracker.metrics_logger.aggregate(task_dict=task_dict, bootstrap_iters=1000)
            evaluation_tracker.details_logger.aggregate()

            if args.output_dir:
                evaluation_tracker.save(
                    args.output_dir, args.push_results_to_hub, args.pu ,args.push_details_to_hub, args.public_run
                )

            final_dict = evaluation_tracker.generate_final_dict()
            
        

        with htrack_block("Cleaninp up"):
            if args.delta_weights:
                tmp_weights_dir = f"{evaluation_tracker.general_config_logger.model_name}-delta-applied"
                hlog(f"Removing {tmp_weights_dir}")
                shutil.rmtree(tmp_weights_dir)
            if args.adapter_weights:
                tmp_weights_dir = f"{evaluation_tracker.general_config_logger.model_name}-adapter-applied"
                hlog(f"Removing {tmp_weights_dir}")
                shutil.rmtree(tmp_weights_dir)

        print(make_results_table(final_dict))
        with htrack_block("Uploading results to Indic LLM Leaderboard"):
            if args.push_to_leaderboard:
                if is_valid_email(args.push_to_leaderboard):
                    evaluation_tracker.push_to_leaderboard(final_dict)
                else:
                    raise ValueError("The email you have specified for --push_to_leaderboard is not valid")
            

        if not args.reuse_existing:
            model.cleanup()

        return final_dict