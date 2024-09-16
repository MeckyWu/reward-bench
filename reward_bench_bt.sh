# Define arrays of hyperparameters
LEARNING_RATES=(2e-6 2e-7 6e-7)
CHECKPOINTS=(150 300 450 600 750 900 1050 1200 1350 1500)  

output_dir="results"

# Loop over each combination of hyperparameters
for LR in "${LEARNING_RATES[@]}"; do
    for CKPT in "${CHECKPOINTS[@]}"; do
        # Define the model path
        model_path_reward="/fsx-project/yuewu96/bt_reward_model/checkpoints/llama3_bt_reward_full_lr_${LR}/checkpoint-${CKPT}"
        result_path="$output_dir$model_path_reward.json"
        # Check if the model path exists (directory or file)

        if [ -f "$result_path" ]; then
            echo "Skipping: $result_path already evaluated."
            continue
        fi

        if [ -d "$model_path_reward" ]; then
            echo "Evaluating: $model_path_reward"
            sbatch --requeue bench.slurm $model_path_reward
        else
            echo "Checkpoint does not exist."
        fi
    done
done
