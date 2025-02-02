#!/bin/bash


if [ $# != 3 ] && [ $# != 0 ]
then
    echo "Usage: sh run_distribute_train_gpu.sh [CONFIG_PATH] [DATA_PATH] [HYP_PATH]"
exit 1
fi

get_real_path(){
  if [ "${1:0:1}" == "/" ]; then
    echo "$1"
  else
    echo "$(realpath -m $PWD/$1)"
  fi
}

if [ $# == 0 ]
then
  CONFIG_PATH=$"./config/network/yolov5s.yaml"
  DATA_PATH=$"./config/data/coco.yaml"
  HYP_PATH=$"./config/data/hyp.scratch-low.yaml"
fi

if [ $# == 3 ]
then
  CONFIG_PATH=$(get_real_path $1)
  DATA_PATH=$(get_real_path $2)
  HYP_PATH=$(get_real_path $3)
fi

echo $CONFIG_PATH
echo $DATA_PATH
echo $HYP_PATH


export DEVICE_NUM=8
rm -rf ./train_parallel
mkdir ./train_parallel
cp ../*.py ./train_parallel
cp -r ../config ./train_parallel
cp -r ../src ./train_parallel
mkdir ./train_parallel/scripts
cp -r ../scripts/*.sh ./train_parallel/scripts/
cd ./train_parallel || exit
env > env.log
mpirun --allow-run-as-root -n ${DEVICE_NUM} --output-filename log_output --merge-stderr-to-stdout \
python train.py \
  --ms_strategy="StaticShape" \
  --ms_amp_level="O0" \
  --ms_loss_scaler="static" \
  --ms_loss_scaler_value=1024 \
  --ms_optim_loss_scale=1024 \
  --ms_grad_sens=1024 \
  --overflow_still_update=False \
  --clip_grad=False \
  --cfg=$CONFIG_PATH \
  --data=$DATA_PATH \
  --hyp=$HYP_PATH \
  --device_target=GPU \
  --is_distributed=True \
  --epochs=300 \
  --batch_size=128 > log.txt 2>&1 &
cd ..