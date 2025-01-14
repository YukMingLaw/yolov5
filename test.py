import io
import glob
import os
import time
import json
import yaml
import codecs
import numpy as np
from pathlib import Path
from threading import Thread
from contextlib import redirect_stdout
from pycocotools.coco import COCO
try:
    from third_party.fast_coco.fast_coco_eval_api import Fast_COCOeval as COCOeval
    print("[INFO] Use third party coco eval api to speed up mAP calculation.")
except ImportError:
    from pycocotools.cocoeval import COCOeval
    print("[INFO] Third party coco eval api import failed, use default api.")

import mindspore as ms
from mindspore.context import ParallelMode
from mindspore import context, Tensor, ops
from mindspore.communication.management import init, get_rank, get_group_size

from src.network.yolo import Model
from config.args import get_args_test
from src.general import coco80_to_coco91_class, check_file, check_img_size, xyxy2xywh, xywh2xyxy, \
    colorstr, box_iou, Synchronize
from src.dataset import create_dataloader
from src.metrics import ConfusionMatrix, non_max_suppression, scale_coords, ap_per_class
from src.plots import plot_study_txt, plot_images, output_to_target
from third_party.yolo2coco.yolo2coco import YOLO2COCO


def save_one_json(predn, jdict, path, class_map):
    # Save one JSON result {"image_id": 42, "category_id": 18, "bbox": [258.15, 41.29, 348.26, 243.78], "score": 0.236}
    image_id = int(path.stem) if path.stem.isnumeric() else path.stem
    box = xyxy2xywh(predn[:, :4])  # xywh
    box[:, :2] -= box[:, 2:] / 2  # xy center to top-left corner
    for p, b in zip(predn.tolist(), box.tolist()):
        jdict.append({
            'image_id': image_id,
            'category_id': class_map[int(p[5])],
            'bbox': [round(x, 3) for x in b],
            'score': round(p[4], 5)})


def save_one_txt(predn, save_conf, shape, file):
    # Save one txt result
    gn = np.array(shape)[[1, 0, 1, 0]]  # normalization gain whwh
    for *xyxy, conf, cls in predn.tolist():
        xywh = (xyxy2xywh(np.array(xyxy).view(1, 4)) / gn).view(-1).tolist()  # normalized xywh
        line = (cls, *xywh, conf) if save_conf else (cls, *xywh)  # label format
        with open(file, 'a') as f:
            f.write(('%g ' * len(line)).rstrip() % line + '\n')


def process_batch(detections, labels, iouv):
    """
    Return correct prediction matrix
    Arguments:
        detections (array[N, 6]), x1, y1, x2, y2, conf, class
        labels (array[M, 5]), class, x1, y1, x2, y2
    Returns:
        correct (array[N, 10]), for 10 IoU levels
    """
    correct = np.zeros((detections.shape[0], iouv.shape[0])).astype(np.bool_)
    iou = box_iou(labels[:, 1:], detections[:, :4])
    correct_class = labels[:, 0:1] == detections[:, 5]
    for i in range(len(iouv)):
        x = np.where((iou >= iouv[i]) & correct_class)  # IoU > threshold and classes match
        if x[0].shape[0]:
            matches = np.concatenate((np.stack(x, 1), iou[x[0], x[1]][:, None]), 1)  # [label, detect, iou]
            if x[0].shape[0] > 1:
                matches = matches[matches[:, 2].argsort()[::-1]]
                matches = matches[np.unique(matches[:, 1], return_index=True)[1]]
                # matches = matches[matches[:, 2].argsort()[::-1]]
                matches = matches[np.unique(matches[:, 0], return_index=True)[1]]
            correct[matches[:, 1].astype(int), i] = True
    return np.array(correct).astype(np.bool_)


def load_checkpoint_to_yolo(model, ckpt_path):
    param_dict = ms.load_checkpoint(ckpt_path)
    new_params = {}
    for k, v in param_dict.items():
        if k.startswith("model.") or k.startswith("updates"):
            new_params[k] = v
        if k.startswith("ema.ema."):
            k = k[len("ema.ema."):]
            new_params[k] = v
    ms.load_param_into_net(model, new_params)
    print(f"load ckpt from \"{ckpt_path}\" success.", flush=True)


def compute_metrics(plots, save_dir, names, nc, seen, stats, verbose, training):
    # Compute metrics
    p, r, f1, mp, mr, map50, map, t0, t1 = 0., 0., 0., 0., 0., 0., 0., 0., 0.
    ap, ap_class = [], []
    stats = [np.concatenate(x, 0) for x in zip(*stats)]  # to numpy
    if len(stats) and stats[0].any():
        tp, fp, p, r, f1, ap, ap_class = ap_per_class(*stats, plot=plots, save_dir=save_dir, names=names)
        ap50, ap = ap[:, 0], ap.mean(1)  # AP@0.5, AP@0.5:0.95
        mp, mr, map50, map = p.mean(), r.mean(), ap50.mean(), ap.mean()
    nt = np.bincount(stats[3].astype(int), minlength=nc)  # number of targets per class

    # Print results
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        title = ('%22s' + '%11s' * 6) % ('Class', 'Images', 'Instances', 'P', 'R', 'mAP50', 'mAP50-95')
        pf = '%20s' + '%12i' * 2 + '%12.3g' * 4  # print format
        print(title)
        print(pf % ('all', seen, nt.sum(), mp, mr, map50, map))

        # Print results per class
        if (verbose or (nc < 50 and not training)) and nc > 1 and len(stats):
            for i, c in enumerate(ap_class):
                # Class     Images  Instances          P          R      mAP50   mAP50-95:
                print(pf % (names[c], seen, nt[c], p[i], r[i], ap50[i], ap[i]))
    map_str = buffer.getvalue()
    print(map_str, flush=True)
    return map_str


def coco_eval(anno_json, pred_json, dataset, is_coco):
    anno = COCO(anno_json)  # init annotations api
    pred = anno.loadRes(pred_json)  # init predictions api
    eval = COCOeval(anno, pred, 'bbox')
    if is_coco:
        eval.params.imgIds = [int(Path(x).stem) for x in dataset.img_files]  # image IDs to evaluate
    eval.evaluate()
    eval.accumulate()
    eval.summarize()
    map, map50 = eval.stats[:2]  # update results (mAP@0.5:0.95, mAP@0.5)
    return map, map50


def merge_json(project_dir, prefix):
    merged_json = os.path.join(project_dir, f"{prefix}_predictions_merged.json")
    merged_result = []
    for json_file in Path(project_dir).rglob("*.json"):
        print(f"[INFO] Merge json file {json_file.resolve()}", flush=True)
        with open(json_file, "r") as file_handler:
            merged_result.extend(json.load(file_handler))
    with open(merged_json, "w") as file_handler:
        json.dump(merged_result, file_handler)
    print(f"[INFO] Write merged results to file {merged_json} successfully.", flush=True)
    return merged_json


def test(data,
         weights=None,
         batch_size=32,
         imgsz=640,
         conf_thres=0.001,
         iou_thres=0.6,  # for NMS
         save_json=False,
         single_cls=False,
         augment=False,
         verbose=False,
         model=None,
         dataloader=None,
         dataset=None,
         save_dir=Path(''),  # for saving images
         save_txt=False,  # for auto-labelling
         save_hybrid=False,  # for hybrid auto-labelling
         save_conf=False,  # save auto-label confidences
         plots=False,
         compute_loss=None,
         half_precision=False,
         trace=False,
         rect=False,
         is_coco=False,
         v5_metric=False,
         is_distributed=False,
         rank=0,
         rank_size=1,
         opt=None,
         cur_epoch=0):
    # Configure
    if isinstance(data, str):
        is_coco = data.endswith('coco.yaml')
        with open(data) as f:
            data = yaml.load(f, Loader=yaml.SafeLoader)
    nc = 1 if single_cls else int(data['nc'])  # number of classes
    iouv = np.linspace(0.5, 0.95, 10)  # iou vector for mAP@0.5:0.95
    niou = np.prod(iouv.shape)
    synchronize = None
    if is_distributed:
        synchronize = Synchronize(rank_size)
    project_dir = os.path.join(opt.project, f"epoch_{cur_epoch}")
    save_dir = os.path.join(project_dir, f"save_dir_{rank}")
    os.makedirs(os.path.join(save_dir, f"labels_{rank}"), exist_ok=False)
    # Initialize/load model and set device
    training = model is not None
    if model is None:  # called by train.py
        # Load model
        # Hyperparameters
        with open(opt.hyp) as f:
            hyp = yaml.load(f, Loader=yaml.SafeLoader)  # load hyps
        model = Model(opt.cfg, ch=3, nc=nc, anchors=hyp.get('anchors'), sync_bn=False)  # create
        ckpt_path = weights
        load_checkpoint_to_yolo(model, ckpt_path)
        gs = max(int(ops.cast(model.stride, ms.float16).max()), 32)  # grid size (max stride)
        imgsz = check_img_size(imgsz, s=gs)  # check img_size

    gs = max(int(ops.cast(model.stride, ms.float16).max()), 32)  # grid size (max stride)
    imgsz = imgsz[0] if isinstance(imgsz, list) else imgsz
    imgsz = check_img_size(imgsz, s=gs)  # check img_size

    # Half
    if half_precision:
        ms.amp.auto_mixed_precision(model, amp_level="O2")

    model.set_train(False)

    # Dataloader
    if dataloader is None or dataset is None:
        print("enable rect", rect, flush=True)
        batch_size = 1 if rect else batch_size
        task = opt.task if opt.task in ('train', 'val', 'test') else 'val'  # path to train/val/test images
        dataloader, dataset, per_epoch_size = create_dataloader(data[task], imgsz, batch_size, gs, opt,
                                                                epoch_size=1, pad=0.5, rect=rect,
                                                                rank=rank,
                                                                rank_size=rank_size,
                                                                num_parallel_workers=4 if rank_size > 1 else 8,
                                                                shuffle=False,
                                                                drop_remainder=False,
                                                                prefix=colorstr(f'{task}: '))
        assert per_epoch_size == dataloader.get_dataset_size()
        data_loader = dataloader.create_dict_iterator(output_numpy=True, num_epochs=1)
        print(f"Test create dataset success, epoch size {per_epoch_size}.")
    else:
        assert dataset is not None
        assert dataloader is not None
        per_epoch_size = dataloader.get_dataset_size()
        data_loader = dataloader.create_dict_iterator(output_numpy=True, num_epochs=1)

    if v5_metric:
        print("Testing with YOLOv5 AP metric...")

    seen = 0
    confusion_matrix = ConfusionMatrix(nc=nc)
    names = {k: v for k, v in enumerate(model.names if hasattr(model, 'names') else model.module.names)}

    class_map = coco80_to_coco91_class() if is_coco else list(range(1000))
    p, r, f1, mp, mr, map50, map, t0, t1 = 0., 0., 0., 0., 0., 0., 0., 0., 0.
    loss = np.zeros(3)
    jdict, stats, ap, ap_class = [], [], [], []
    s_time = time.time()
    t2 = 0.
    for batch_i, meta_data in enumerate(data_loader):
        img, targets, paths, shapes = meta_data["img"], meta_data["label_out"], \
                                      meta_data["img_files"], meta_data["shapes"]
        img = img.astype(np.float32) / 255.0  # 0 - 255 to 0.0 - 1.0
        img_tensor = Tensor.from_numpy(img)
        targets_tensor = Tensor.from_numpy(targets)
        if half_precision:
            img_tensor = ops.cast(img_tensor, ms.float16)
            targets_tensor = ops.cast(targets_tensor, ms.float16)

        targets = targets.reshape((-1, 6))
        targets = targets[targets[:, 1] >= 0]
        nb, _, height, width = img.shape  # batch size, channels, height, width
        data_time = time.time() - s_time
        # Run model
        t = time.time()
        pred_out, train_out = model(img_tensor, augment=augment)  # inference and training outputs

        infer_time = time.time() - t
        t0 += infer_time

        # Compute loss
        if compute_loss:
            loss += compute_loss(train_out, targets_tensor)[1][:3].asnumpy()  # box, obj, cls

        # NMS
        targets[:, 2:] *= np.array([width, height, width, height], targets.dtype)  # to pixels
        lb = [targets[targets[:, 0] == i, 1:] for i in range(nb)] if save_hybrid else []  # for autolabelling
        t = time.time()
        out = non_max_suppression(pred_out.asnumpy(),
                                  conf_thres,
                                  iou_thres,
                                  labels=lb,
                                  multi_label=True,
                                  agnostic=single_cls)
        nms_time = time.time() - t
        t1 += nms_time

        # Metrics
        t = time.time()
        for si, pred in enumerate(out):
            labels = targets[targets[:, 0] == si, 1:]
            nl, npr, shape = labels.shape[0], pred.shape[0], shapes[si][0]  # number of labels, predictions
            if type(paths[si]) is np.ndarray or type(paths[si]) is np.bytes_:
                path = Path(str(codecs.decode(paths[si].tostring()).strip(b'\x00'.decode())))
            else:
                path = Path(paths[si])
            correct = np.zeros((npr, niou)).astype(np.bool_)  # init
            seen += 1

            if npr == 0:
                if nl:
                    stats.append((correct, *np.zeros((2, 0)).astype(np.bool_), labels[:, 0]))
                    if plots:
                        confusion_matrix.process_batch(detections=None, labels=labels[:, 0])
                continue

            # Predictions
            if single_cls:
                pred[:, 5] = 0
            predn = np.copy(pred)
            predn[:, :4] = scale_coords(img[si].shape[1:], predn[:, :4], shape, shapes[si][1:])  # native-space pred

            # Evaluate
            if nl:
                tbox = xywh2xyxy(labels[:, 1:5])  # target boxes
                tbox = scale_coords(img[si].shape[1:], tbox, shape, shapes[si][1:])  # native-space labels
                labelsn = np.concatenate((labels[:, 0:1], tbox), 1)  # native-space labels
                correct = process_batch(predn, labelsn, iouv)
                if plots:
                    confusion_matrix.process_batch(predn, labelsn)
            stats.append((correct, pred[:, 4], pred[:, 5], labels[:, 0]))  # (correct, conf, pcls, tcls)

            # Save/log
            if save_txt:
                save_one_txt(predn, save_conf, shape, file=os.path.join(save_dir, 'labels', f'{path.stem}.txt'))
            if save_json:
                save_one_json(predn, jdict, path, class_map)  # append to COCO-JSON dictionary
        metric_time = time.time() - t
        t2 += metric_time
        # Plot images
        if plots and batch_i < 3:
            f = os.path.join(save_dir, f'test_batch{batch_i}_labels.jpg')  # labels
            Thread(target=plot_images, args=(img, targets, paths, f, names), daemon=True).start()
            f = os.path.join(save_dir, f'test_batch{batch_i}_pred.jpg')  # predictions
            Thread(target=plot_images, args=(img, output_to_target(out), paths, f, names), daemon=True).start()

        # print(f"Test step: {batch_i + 1}/{per_epoch_size}: cost time {time.time() - s_time:.2f}s", flush=True)
        print(f"Test step: {batch_i + 1}/{per_epoch_size}: cost time [{(time.time() - s_time) * 1e3:.2f}]ms "
              f"Data time: [{data_time * 1e3:.2f}]ms  Infer time: [{infer_time * 1e3:.2f}]ms  "
              f"NMS time: [{nms_time * 1e3:.2f}]ms  "
              f"Metric time: [{metric_time * 1e3:.2f}]ms", flush=True)
        s_time = time.time()

    # compute_metrics(plots, save_dir, names, nc, seen, verbose, training)

    # Print speeds
    total = tuple(x for x in (t0, t1, t2, t0 + t1 + t2)) + (imgsz, imgsz, batch_size)  # tuple
    t = tuple(x / seen * 1E3 for x in (t0, t1, t2, t0 + t1 + t2)) + (imgsz, imgsz, batch_size)  # tuple
    # if not training:
    # print('Speed: %.1f/%.1f/%.1f ms inference/NMS/total per %gx%g image at batch-size %g' % t)
    print('Speed: %.1f/%.1f/%.1f/%.1f ms inference/NMS/Metric/total per %gx%g image at batch-size %g' % t)
    print('Total time: %.1f/%.1f/%.1f/%.1f s inference/NMS/Metric/total %gx%g image at batch-size %g' % total)
    # print(f"Total seen: [{seen}]", flush=True)
    # Plots
    if plots:
        confusion_matrix.plot(save_dir=save_dir, names=list(names.values()))

    # Save JSON
    if save_json and len(jdict):
        w = Path(weights).stem if weights is not None else ''  # weights
        data_dir = Path(data["val"]).parent
        anno_json = os.path.join(data_dir, "annotations/instances_val2017.json")
        if opt.transfer_format and not os.path.exists(anno_json): # data format transfer if annotations does not exists
            print("[INFO] Transfer annotations from yolo to coco format.")
            transformer = YOLO2COCO(data_dir, output_dir=data_dir,
                                    class_names=data["names"], class_map=class_map,
                                    mode='val', annotation_only=True)
            transformer()
        pred_json = os.path.join(save_dir, f"{w}_predictions_{rank}.json")  # predictions json
        print('\nEvaluating pycocotools mAP... saving %s...' % pred_json)
        with open(pred_json, 'w') as f:
            json.dump(jdict, f)
        sync_tmp_file = os.path.join(project_dir, 'sync_file.tmp')
        if synchronize is not None:
            if rank == 0:
                print(f"[INFO] Create sync temp file at path {sync_tmp_file}", flush=True)
                os.mknod(sync_tmp_file)
            synchronize()
            # Merge multiple results files
            if rank == 0:
                print("[INFO] Merge detection results...", flush=True)
                pred_json = merge_json(project_dir, prefix=w)
        try:  # https://github.com/cocodataset/cocoapi/blob/master/PythonAPI/pycocoEvalDemo.ipynb
            if rank == 0:
                print("[INFO] Start evaluating mAP...", flush=True)
                map, map50 = coco_eval(anno_json, pred_json, dataset, is_coco)
                print("[INFO] Finish evaluating mAP.", flush=True)
                if os.path.exists(sync_tmp_file):
                    print(f"[INFO] Delete sync temp file at path {sync_tmp_file}", flush=True)
                    os.remove(sync_tmp_file)
            else:
                print(f"[INFO] Waiting for rank [0] device...", flush=True)
                while os.path.exists(sync_tmp_file):
                    time.sleep(1)
                print(f"[INFO] Rank [{rank}] continue executing.", flush=True)
        except Exception as e:
            print(f'pycocotools unable to run: {e}')

    # Return results
    if not training:
        s = f"\n{len(glob.glob(os.path.join(save_dir, 'labels/*.txt')))} labels saved to " \
            f"{os.path.join(save_dir, 'labels')}" if save_txt else ''
        print(f"Results saved to {save_dir}, {s}")
    maps = np.zeros(nc) + map
    for i, c in enumerate(ap_class):
        maps[c] = ap[i]

    model.set_train()
    return (mp, mr, map50, map, *(loss / per_epoch_size).tolist()), maps, t


if __name__ == '__main__':
    opt = get_args_test()
    opt.save_json |= opt.data.endswith('coco.yaml')
    opt.data, opt.cfg, opt.hyp = check_file(opt.data), check_file(opt.cfg), check_file(opt.hyp)  # check files
    print(opt)

    ms_mode = ms.GRAPH_MODE if opt.ms_mode == "graph" else ms.PYNATIVE_MODE
    ms.set_context(mode=ms.PYNATIVE_MODE, device_target=opt.device_target)
    # ms.set_context(pynative_synchronize=True)
    context.set_context(mode=ms_mode, device_target=opt.device_target)
    if opt.device_target == "Ascend":
        device_id = int(os.getenv('DEVICE_ID', 0))
        context.set_context(device_id=device_id)
    rank, rank_size, parallel_mode = 0, 1, ParallelMode.STAND_ALONE
    # Distribute Test
    if opt.is_distributed:
        init()
        rank, rank_size, parallel_mode = get_rank(), get_group_size(), ParallelMode.DATA_PARALLEL
    context.set_auto_parallel_context(parallel_mode=parallel_mode, gradients_mean=True, device_num=rank_size)
    opt.total_batch_size = opt.batch_size
    opt.rank_size = rank_size
    opt.rank = rank
    if rank_size > 1:
        assert opt.batch_size % opt.rank_size == 0, '--batch-size must be multiple of device count'
        opt.batch_size = opt.total_batch_size // opt.rank_size
    if opt.task in ('train', 'val', 'test'):  # run normally
        print("opt:", opt)
        test(opt.data,
             opt.weights,
             opt.batch_size,
             opt.img_size,
             opt.conf_thres,
             opt.iou_thres,
             opt.save_json,
             opt.single_cls,
             opt.augment,
             opt.verbose,
             save_txt=opt.save_txt | opt.save_hybrid,
             save_hybrid=opt.save_hybrid,
             save_conf=opt.save_conf,
             trace=not opt.no_trace,
             rect=opt.rect,
             plots=False,
             half_precision=False,
             v5_metric=opt.v5_metric,
             is_distributed=opt.is_distributed,
             rank=opt.rank,
             rank_size=opt.rank_size,
             opt=opt)

    elif opt.task == 'speed':  # speed benchmarks
        test(opt.data, opt.weights, opt.batch_size, opt.img_size, 0.25, 0.45,
             save_json=False, plots=False, half_precision=False, v5_metric=opt.v5_metric)

    elif opt.task == 'study':  # run over a range of settings and save/plot
        # python test.py --task study --data coco.yaml --iou 0.65 --weights yolov5.ckpt
        x = list(range(256, 1536 + 128, 128))  # x axis (image sizes)
        f = f'study_{Path(opt.data).stem}_{Path(opt.weights).stem}.txt'  # filename to save to
        y = []  # y axis
        for i in x:  # img-size
            print(f'\nRunning {f} point {i}...')
            r, _, t = test(opt.data, opt.weights, opt.batch_size, i, opt.conf_thres, opt.iou_thres, opt.save_json,
                           plots=False, half_precision=False, v5_metric=opt.v5_metric)
            y.append(r + t)  # results and times
        np.savetxt(f, y, fmt='%10.4g')  # save
        os.system('zip -r study.zip study_*.txt')
        plot_study_txt(x=x)  # plot
