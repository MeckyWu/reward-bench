#!/bin/bash

#SBATCH --job-name=bench
#SBATCH --account genai_interns
#SBATCH --qos low
##SBATCH --partition=learnai  # change as needed, e.g., lowpri on some clusters
##SBATCH --gres=gpu:1        # uncomment only if/as needed
#SBATCH --time=01:00:00   
#SBATCH --nodes=1
#SBATCH --gpus-per-node=1
##SBATCH --cpus-per-task=8    # change as needed
## %j is the job id, q%u is the user id
#SBATCH --output=/data/home/yuewu96/reward-bench/slurm/%j.log

model_path_reward=$1
mode="${2:-bt}"
# NUM_PREF_RANK=$3

source /data/home/yuewu96/miniconda3/etc/profile.d/conda.sh
# conda init bash
conda activate handbook

# cat /etc/hosts
if [ "$mode" == "bt" ]; then 
    echo "$mode: Evaluating Bradeley-Terry"
    python rewardbench_custom.py --model $model_path_reward
else 
    echo "$mode: Evaluating LRPO"
    python rewardbench_lrpo.py --model $model_path_reward
fi
