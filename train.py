from __future__ import absolute_import, division, print_function

import os
import sys
import gc
import json
import pickle
import random
import shutil
import operator
import argparse
import numpy as np
import torch
import torchvision.transforms.functional as F
from PIL import Image

from tools import get_embedding, get_split, get_config, logWritter, MeaninglessError, scores_gzsl, Step_Scheduler, Const_Scheduler, construct_gt_st
from libs.datasets import get_dataset
from trainer import Trainer
# from zeroseg_dataload import dataloader as zeroseg_dataloader
from torchvision import transforms
from libs.zeroseg_dataload.dataloader import dataloader as zeroseg_dataloader
from libs.metric.mSA_test import db_eval_iou_multi as eval_iou

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='./configs/zeroseg.yaml', help='configuration file for train/val')
    parser.add_argument('--experimentid', default='0', help='model name/save dir')
    parser.add_argument('--resume_from', type=int, default=0, help='continue train(>0) or train from scratch/val(<=0)')
    parser.add_argument('--schedule', default='step1', help='[step1/mixed/st/st_mixed] schedule method for training (omitted in val)')
    parser.add_argument('--init_model', default='none', help='overwrite <init_model> in the config file if not none')
    parser.add_argument('--val', action='store_true', help='only do validation if set True')
    parser.add_argument('--test', action='store_true', help='do test if set True')
    parser.add_argument('--multigpus', action='store_false', help='use multiple GPUs or single GPU')
    parser.add_argument('--ngpu', type=int, default=2, help='number of GPUs to be used if multigpus is Ture, GPU id otherwise')


    return parser.parse_args()


def main():
    """
    Acquire args and config
    """
    args = parse_args()
    assert (os.path.exists(args.config))
    assert (args.schedule in ['step1', 'mixed', 'st', 'st_mixed'])
    assert ((args.multigpus == False and args.ngpu >= 0) or (args.multigpus == True and args.ngpu > 1))
    assert (not (args.val and args.resume_from > 0))
    config = get_config(args.config)
    assert (not (args.val and config['init_model'] == 'none' and args.init_model == 'none'))
    if args.init_model != 'none':
        assert (os.path.exists(args.init_model))
        config['init_model'] = args.init_model

    """
    Path to save results.
    """
    dataset_path = os.path.join(config['save_path'], config['dataset'])
    if not os.path.exists(dataset_path):
        os.makedirs(dataset_path)

    save_path = os.path.join(dataset_path, args.experimentid)
    if not os.path.exists(save_path) and not args.val:
        os.makedirs(save_path)

    if args.schedule == 'step1':
        model_path = os.path.join(save_path, 'models')
    elif args.schedule == 'mixed':
        model_path = os.path.join(save_path, 'models_transfer')
    elif args.schedule == 'st': 
        model_path = os.path.join(save_path, 'models_st')
    else:
        model_path = os.path.join(save_path, 'models_st_transfer')
    if args.resume_from > 0:
        assert (os.path.exists(model_path))
    if not os.path.exists(model_path) and not args.val:
        os.makedirs(model_path)

    if args.schedule == 'step1':
        log_file = os.path.join(save_path, 'logs.txt') 
    elif args.schedule == 'mixed':
        log_file = os.path.join(save_path, 'logs_transfer.txt')
    elif args.schedule == 'st': 
        log_file = os.path.join(save_path, 'logs_st.txt')
    else:
        log_file = os.path.join(save_path, 'logs_st_transfer.txt')
    if args.val:
        log_file = os.path.join(dataset_path, 'logs_test.txt')
    logger = logWritter(log_file)

    if args.schedule == 'step1':
        config_path = os.path.join(save_path, 'configs.yaml')
    elif args.schedule == 'mixed':
        config_path = os.path.join(save_path, 'configs_transfer.yaml')
    elif args.schedule == 'st':
        config_path = os.path.join(save_path, 'configs_st.yaml')
    else:
        config_path = os.path.join(save_path, 'configs_st_transfer.yaml')

    """
    Start
    """
    if args.val:
        print("\n***Val of model {0}***\n".format(config['init_model']))
        logger.write("\n***Val of model {0}***\n".format(config['init_model']))
    else:
        print("\n***Training of model {0}***\n".format(args.experimentid))
        logger.write("\n***Training of model {0}***\n".format(args.experimentid))

    """
    Continue train or train from scratch
    """
    if args.resume_from >= 1:
        assert (args.val == False)
        if not os.path.exists(config_path):
            assert 0, "Old config not found."
        config_old = get_config(config_path)
        if config['save_path'] != config_old['save_path'] or config['dataset'] != config_old['dataset']:
            assert 0, "New config does not coordinate with old config."
        config = config_old
        start_iter = args.resume_from
        print("Continue training from Iter - [{0:0>6d}] ...".format(start_iter + 1))
        logger.write("Continue training from Iter - [{0:0>6d}] ...".format(start_iter + 1))
    else:
        start_iter = 0
        if not args.val:       
            shutil.copy(args.config, config_path)
            print("Train from scratch ...")
            logger.write("Train from scratch ...")

    """
    Modify config
    """
    if args.schedule == 'step1':
        config['back_scheduler']['init_lr'] = config['back_opt']['lr']
    elif args.schedule == 'mixed':
        config['back_scheduler']['init_lr_transfer'] = config['back_opt']['lr_transfer']
    elif args.schedule == 'st':
        config['back_scheduler']['init_lr_st'] = config['back_opt']['lr_st']
    else:
        config['back_scheduler']['init_lr_st_transfer'] = config['back_opt']['lr_st_transfer']

    if args.schedule == 'step1':
        config['back_scheduler']['max_iter'] = config['ITER_MAX']
    elif args.schedule == 'mixed':
        config['back_scheduler']['max_iter_transfer'] = config['ITER_MAX_TRANSFER']
    elif args.schedule == 'st':
        config['back_scheduler']['max_iter_st'] = config['ITER_MAX_ST']
    else:
        config['back_scheduler']['max_iter_st_transfer'] = config['ITER_MAX_ST_TRANSFER']

    """
    Schedule method
    """
    s = "Schedule method: {0}".format(args.schedule)
    if args.schedule == 'mixed' or args.schedule == 'st_mixed':
        s += ", interval_step1={0}, interval_step2={1}".format(config['interval_step1'], config['interval_step2'])
    s += '\n'
    print(s)
    logger.write(s)

    """
    Use GPU
    """
    device = torch.device("cuda")
    if not args.multigpus:
        torch.cuda.set_device(args.ngpu)
    torch.backends.cudnn.benchmark = True

    """
    Get dataLoader
    """
    vals_cls, valu_cls, all_labels, visible_classes, visible_classes_test, cls_map, cls_map_test = get_split(config)

    """
    Loader train data
    """
    dataset = zeroseg_dataloader(obj_npy_path='./dataset/zeroseg/obj_id.npy', train_npy_path='./dataset/zeroseg/train_dist.npy',
                 root_path='/media/adminer/data/zhijiang_zeroseg/', mode='train')

    loader = torch.utils.data.DataLoader(
        dataset=dataset,
        batch_size=config['BATCH_SIZE']['TRAIN'],
        num_workers=config['NUM_WORKERS'],
        shuffle=True
    )
    """
    Loader val data
    """
    dataset_val = zeroseg_dataloader(obj_npy_path='./dataset/zeroseg/obj_id.npy', train_npy_path='./dataset/zeroseg/train_dist.npy',
                                 root_path='/media/adminer/data/zhijiang_zeroseg/', mode='val')
    loader_val = torch.utils.data.DataLoader(
        dataset=dataset_val,
        batch_size=config['BATCH_SIZE']['TEST'],
        num_workers=config['NUM_WORKERS'],
        shuffle=False
    )

    """
    Loader test data
    """
    dataset_test = zeroseg_dataloader(root_path='/media/adminer/data/zhijiang_zeroseg/', mode='test')
    loader_test = torch.utils.data.DataLoader(
        dataset=dataset_test,
        batch_size=config['BATCH_SIZE']['TEST'],
        num_workers=config['NUM_WORKERS'],
        shuffle=False
    )

    """
    Load Class embedding
    """
    class_emb = get_embedding(config)
    class_emb_vis = class_emb[visible_classes]
    class_emb_vis_ = torch.zeros((config['ignore_index'] + 1 - class_emb_vis.shape[0], class_emb_vis.shape[1]), dtype = torch.float32)
    class_emb_vis_aug = torch.cat((class_emb_vis, class_emb_vis_), dim=0)
    class_emb_all = class_emb[visible_classes_test]

    """
    Get trainer
    """
    trainer = Trainer(
        cfg=config, 
        class_emb_vis=class_emb_vis_aug, 
        class_emb_all=class_emb_all, 
        schedule=args.schedule, 
        checkpoint_dir=model_path,  # for model loading in continued train
        resume_from=start_iter  # for model loading in continued train
    ).to(device)
    if args.multigpus:
        trainer.model = torch.nn.DataParallel(trainer.model, device_ids=range(args.ngpu))

    """
    Train/Val/Test
    """
    if args.test:
        """
        Only do test
        """
        loader_iter_test = iter(loader_test)
        while True:
            try:
                data_test = next(loader_iter_test)
            except:
                break # finish test

            data_test = torch.Tensor(data_test).to(device)

            with torch.no_grad():
                try:
                    pred_cls = trainer.test(data_test, multigpus=args.multigpus)
                except MeaninglessError:
                    continue # skip meaningless batch
                pred_cls_numpy = pred_cls.permute(1, 2, 0).squeeze().cpu().float().numpy()
                pred_cls_PIL = Image.fromarray(pred_cls_numpy)
                pred_cls_PIL = pred_cls_PIL.convert('L')
                print(np.unique(pred_cls_numpy))

    elif args.val and not args.test:
        """
        Only do validation
        """
        loader_iter_val = iter(loader_val)
        count, total_iou, iou = 0, 0, 0
        while True:
            try:
                data_val, gt = next(loader_iter_val)
            except:
                break # finish test
            data_val = torch.Tensor(data_val).to(device)
            with torch.no_grad():
                try:
                    pred_cls = trainer.test(data_val, multigpus=args.multigpus)
                except MeaninglessError:
                    continue # skip meaningless batch
                pred_cls_numpy = pred_cls.squeeze().cpu().float().numpy()
                pred_cls_PIL = Image.fromarray(pred_cls_numpy)
                label = gt.cpu().numpy()
                pred_cls_resize = F.resized_crop(pred_cls_PIL, 0, 0, pred_cls_numpy.shape[0], pred_cls_numpy.shape[1],
                                                 (label.shape[1], label.shape[2]), Image.NEAREST)
                pred_cls_resize = np.array(pred_cls_resize)[np.newaxis, :, :]

                # label = np.array([[1,1,2], [1,1,2], [0,0,2]])[np.newaxis,:,:]
                # pred_cls_resize = np.array([[0,1,1], [1,1,2], [0,2,2]])[np.newaxis,:,:]
                iou = eval_iou(label, pred_cls_resize)
                total_iou += iou
                print(f"Current Seen Class mSA: {iou}")
            count += 1
            # print(iou)
        total_iou = total_iou / count
        print(f"Total Seen Class mSA: {total_iou}")


    else:
        """
        Training loop
        """
        if args.schedule == 'step1':
            ITER_MAX = config['ITER_MAX']
        elif args.schedule == 'mixed':
            ITER_MAX = config['ITER_MAX_TRANSFER']
        elif args.schedule == 'st':
            ITER_MAX = config['ITER_MAX_ST']
        else:
            ITER_MAX = config['ITER_MAX_ST_TRANSFER']
        assert (start_iter < ITER_MAX)

        # dealing with 'st_mixed' is the same as dealing with 'mixed'
        if args.schedule == 'st_mixed':
            args.schedule = 'mixed'
        assert (args.schedule in ['step1', 'mixed', 'st'])

        if args.schedule == 'step1':
            step_scheduler = Const_Scheduler(step_n='step1')
        elif args.schedule == 'mixed':
            step_scheduler = Step_Scheduler(config['interval_step1'], config['interval_step2'], config['first'])
        else:
            step_scheduler = Const_Scheduler(step_n='self_training')

        iteration = start_iter
        loader_iter = iter(loader)
        while True:
            if iteration == start_iter or iteration % 1000 == 0:
                now_lr = trainer.get_lr()
                print("Now lr of dis: {0:.10f}".format(now_lr['dis_lr']))
                print("Now lr of gen: {0:.10f}".format(now_lr['gen_lr']))
                print("Now lr of back: {0:.10f}".format(now_lr['back_lr']))
                logger.write("Now lr of dis: {0:.10f}".format(now_lr['dis_lr']))
                logger.write("Now lr of gen: {0:.10f}".format(now_lr['gen_lr']))
                logger.write("Now lr of back: {0:.10f}".format(now_lr['back_lr']))

                sum_loss_train = np.zeros(config['loss_count'], dtype=np.float64)
                sum_acc_real_train, sum_acc_fake_train = 0, 0
                temp_iter = 0

                sum_loss_train_transfer = 0
                sum_acc_fake_train_transfer = 0
                temp_iter_transfer = 0

            # mode should be constant 'step1' in non-zero-shot-learning
            # mode should be switched between 'step1' and 'step2' in zero-shot-learning
            mode = step_scheduler.now()
            assert (mode in ['step1', 'step2', 'self_training'])

            if mode == 'step1' or mode == 'self_training':
                try:
                    data, gt = next(loader_iter)
                except:
                    loader_iter = iter(loader)
                    data, gt = next(loader_iter)

                data = torch.Tensor(data.float()).to(device)

            if mode == 'step1' or mode == 'step2':
                try:
                    loss = trainer.train(data, gt, mode=mode, multigpus=args.multigpus)
                except MeaninglessError:
                    print("Skipping meaningless batch...")
                    continue
            else:  # self training mode
                try:
                    with torch.no_grad():
                        test_res = trainer.val(data, gt, multigpus=args.multigpus)
                        resized_gt_for_st = test_res['resized_gt'].cpu() # torch.LongTensor with shape (N,H',W'). elements: 0-14,255 in voc12
                        sorted_indices = test_res['sorted_indices'].cpu() # torch.LongTensor with shape (N,H',W',C)
                        gt_new = construct_gt_st(resized_gt_for_st, sorted_indices, config)
                    loss = trainer.train(data, gt_new, mode='step1', multigpus=args.multigpus)
                except MeaninglessError:
                    print("Skipping meaningless batch...")
                    continue

            if mode == 'step1' or mode == 'self_training':
                loss_G_GAN = loss['loss_G_GAN']
                loss_G_Content = loss['loss_G_Content']
                loss_B_KLD = loss['loss_B_KLD']
                loss_D_real = loss['loss_D_real']
                loss_D_fake = loss['loss_D_fake']
                loss_D_gp = loss['loss_D_gp']
                loss_cls_real = loss['loss_cls_real']
                loss_cls_fake = loss['loss_cls_fake']
                acc_cls_real = loss['acc_cls_real']
                acc_cls_fake = loss['acc_cls_fake']

                sum_loss_train += np.array([loss_G_GAN, loss_G_Content, loss_B_KLD, loss_D_real, loss_D_fake, loss_D_gp, loss_cls_real, loss_cls_fake]).astype(np.float64)      
                sum_acc_real_train += acc_cls_real
                sum_acc_fake_train += acc_cls_fake
                temp_iter += 1

                tal = sum_loss_train / temp_iter
                tsar = sum_acc_real_train / temp_iter
                tsaf = sum_acc_fake_train / temp_iter

                # display accumulated average loss and accuracy in step1
                if (iteration + 1) % config['display_interval'] == 0:
                    print("Iter - [{0:0>6d}] AAL: G_G-[{1:.4f}] G_C-[{2:.4f}] B_K-[{3:.4f}] D_r-[{4:.4f}] D_f-[{5:.4f}] D_gp-[{6:.4f}] cls_r-[{7:.4f}] cls_f-[{8:.4f}] Acc: cls_r-[{9:.4f}] cls_f-[{10:.4f}]".format(\
                            iteration + 1, tal[0], tal[1], tal[2], tal[3], tal[4], tal[5], tal[6], tal[7], tsar, tsaf))
                if (iteration + 1) % config['log_interval'] == 0:
                    logger.write("Iter - [{0:0>6d}] AAL: G_G-[{1:.4f}] G_C-[{2:.4f}] B_K-[{3:.4f}] D_r-[{4:.4f}] D_f-[{5:.4f}] D_gp-[{6:.4f}] cls_r-[{7:.4f}] cls_f-[{8:.4f}] Acc: cls_r-[{9:.4f}] cls_f-[{10:.4f}]".format(\
                                iteration + 1, tal[0], tal[1], tal[2], tal[3], tal[4], tal[5], tal[6], tal[7], tsar, tsaf))

            elif mode == 'step2':
                loss_cls_fake_transfer = loss['loss_cls_fake']
                acc_cls_fake_transfer = loss['acc_cls_fake']

                sum_loss_train_transfer += loss_cls_fake_transfer
                sum_acc_fake_train_transfer += acc_cls_fake_transfer
                temp_iter_transfer += 1

                talt = sum_loss_train_transfer / temp_iter_transfer
                tsaft = sum_acc_fake_train_transfer / temp_iter_transfer

                # display accumulated average loss and accuracy in step2 (transfer learning)
                if (iteration + 1) % config['display_interval'] == 0:
                    print("Iter - [{0:0>6d}] Transfer Learning: aal_cls_f-[{1:.4f}] acc_cls_f-[{2:.4f}]".format(\
                            iteration + 1, talt, tsaft))
                if (iteration + 1) % config['log_interval'] == 0:
                    logger.write("Iter - [{0:0>6d}] Transfer Learning: aal_cls_f-[{1:.4f}] acc_cls_f-[{2:.4f}]".format(\
                            iteration + 1, talt, tsaft))

            else:
                raise NotImplementedError('Mode {} not implemented' % mode)

            # Save the temporary model
            if (iteration + 1) % config['snapshot1'] == 0:
                trainer.save(model_path, iteration, args.multigpus)
                print("Temporary model of Iter - [{0:0>6d}] successfully stored.\n".format(iteration + 1))
                logger.write("Temporary model of Iter - [{0:0>6d}] successfully stored.\n".format(iteration + 1))

            # Test the saved model
            if (iteration + 1) % config['snapshot'] == 0:
                print("Validing model of Iter - [{0:0>6d}] ...".format(iteration + 1))
                logger.write("Validing model of Iter - [{0:0>6d}] ...".format(iteration + 1))

                loader_iter_val = iter(loader_val)
                count, total_iou = 0, 0
                while True:
                    try:
                        data_val, gt = next(loader_iter_val)
                    except:
                        break # finish test
                    data_val = torch.Tensor(data_val).to(device)
                    with torch.no_grad():
                        try:
                            pred_cls = trainer.test(data_val, multigpus=args.multigpus)
                        except MeaninglessError:
                            continue # skip meaningless batch
                        pred_cls_numpy = pred_cls.squeeze().cpu().float().numpy()
                        pred_cls_PIL = Image.fromarray(pred_cls_numpy)
                        label = gt.cpu().numpy()
                        pred_cls_resize = F.resized_crop(pred_cls_PIL, 0, 0, pred_cls_numpy.shape[0], pred_cls_numpy.shape[1],
                                                         (label.shape[1], label.shape[2]), Image.NEAREST)
                        pred_cls_resize = np.array(pred_cls_resize)[np.newaxis, :, :]
                        total_iou += eval_iou(label, pred_cls_resize)
                        count += 1
                        # print(iou)
                total_iou = total_iou / count
                print("Valid results:")
                logger.write("Valid results:")

                print(f"Seen Class IoU: {total_iou}")
                logger.write(f"Seen Class IoU: {total_iou}")

                print("Valid finished.\n")
                logger.write("Valid finished.\n")

            step_scheduler.step()

            iteration += 1
            if iteration == ITER_MAX:
                break

        print("Train finished.\n\n")
        logger.write("Train finished.\n\n")


if __name__ == '__main__':
    main()
