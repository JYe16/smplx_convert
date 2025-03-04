# -*- coding: utf-8 -*-

# Max-Planck-Gesellschaft zur Förderung der Wissenschaften e.V. (MPG) is
# holder of all proprietary rights on this computer program.
# You can only use this computer program if you have closed
# a license agreement with MPG or you get the right to use the computer
# program from someone who is authorized to grant you that right.
# Any use of the computer program without a valid license is prohibited and
# liable to prosecution.
#
# Copyright©2020 Max-Planck-Gesellschaft zur Förderung
# der Wissenschaften e.V. (MPG). acting on behalf of its Max Planck Institute
# for Intelligent Systems. All rights reserved.
#
# Contact: ps-license@tuebingen.mpg.de


import sys
import os
import os.path as osp
from typing import List, Optional
import functools

os.environ['PYOPENGL_PLATFORM'] = 'egl'

import resource
import numpy as np
from collections import OrderedDict, defaultdict
from loguru import logger
import cv2
import argparse
import time
import open3d as o3d
from tqdm import tqdm
from threadpoolctl import threadpool_limits
import PIL.Image as pil_img
import matplotlib.pyplot as plt
import scipy.io

import torch
import torch.utils.data as dutils
from torchvision.models.detection import keypointrcnn_resnet50_fpn
from torchvision.transforms import Compose, Normalize, ToTensor

from expose.data.datasets import ImageFolder, ImageFolderWithBoxes

from expose.data.targets.image_list import to_image_list
from expose.utils.checkpointer import Checkpointer

from expose.data.build import collate_batch
from expose.data.transforms import build_transforms

from expose.models.smplx_net import SMPLXNet
from expose.config import cfg
from expose.config.cmd_parser import set_face_contour
from expose.utils.plot_utils import HDRenderer

rlimit = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (rlimit[1], rlimit[1]))

Vec3d = o3d.utility.Vector3dVector
Vec3i = o3d.utility.Vector3iVector


def collate_fn(batch):
    output_dict = dict()

    for d in batch:
        for key, val in d.items():
            if key not in output_dict:
                output_dict[key] = []
            output_dict[key].append(val)
    return output_dict


def preprocess_images(
        image_folder: str,
        exp_cfg,
        num_workers: int = 8, batch_size: int = 1,
        min_score: float = 0.5,
        scale_factor: float = 1.2,
        device: Optional[torch.device] = None
) -> dutils.DataLoader:
    if device is None:
        device = torch.device('cuda')
        if not torch.cuda.is_available():
            logger.error('CUDA is not available!')
            sys.exit(3)

    rcnn_model = keypointrcnn_resnet50_fpn(pretrained=True)
    rcnn_model.eval()
    rcnn_model = rcnn_model.to(device=device)

    transform = Compose(
        [ToTensor(), ]
    )

    # Load the images
    dataset = ImageFolder(image_folder, transforms=transform)
    dataset.paths.sort()
    rcnn_dloader = dutils.DataLoader(
        dataset, batch_size=batch_size, num_workers=num_workers,
        collate_fn=collate_fn
    )

    out_dir = osp.expandvars('$HOME/Dropbox/boxes')
    os.makedirs(out_dir, exist_ok=True)

    img_paths = []
    bboxes = []
    for bidx, batch in enumerate(
            tqdm(rcnn_dloader, desc='Processing with R-CNN')):
        batch['images'] = [x.to(device=device) for x in batch['images']]

        output = rcnn_model(batch['images'])
        for ii, x in enumerate(output):
            img = np.transpose(
                batch['images'][ii].detach().cpu().numpy(), [1, 2, 0])
            img = (img * 255).astype(np.uint8)

            img_path = batch['paths'][ii]
            _, fname = osp.split(img_path)
            fname, _ = osp.splitext(fname)

            #  out_path = osp.join(out_dir, f'{fname}_{ii:03d}.jpg')
            for n, bbox in enumerate(output[ii]['boxes']):
                bbox = bbox.detach().cpu().numpy()
                if output[ii]['scores'][n].item() < min_score:
                    continue
                img_paths.append(img_path)
                bboxes.append(bbox)

                #  cv2.rectangle(img, tuple(bbox[:2]), tuple(bbox[2:]),
                #  (255, 0, 0))
            #  cv2.imwrite(out_path, img[:, :, ::-1])

    dataset_cfg = exp_cfg.get('datasets', {})
    body_dsets_cfg = dataset_cfg.get('body', {})

    body_transfs_cfg = body_dsets_cfg.get('transforms', {})
    transforms = build_transforms(body_transfs_cfg, is_train=False)
    batch_size = body_dsets_cfg.get('batch_size', 64)

    expose_dset = ImageFolderWithBoxes(
        img_paths, bboxes, scale_factor=scale_factor, transforms=transforms)

    expose_collate = functools.partial(
        collate_batch, use_shared_memory=num_workers > 0,
        return_full_imgs=True)
    expose_dloader = dutils.DataLoader(
        expose_dset,
        batch_size=batch_size,
        num_workers=num_workers,
        collate_fn=expose_collate,
        drop_last=False,
        pin_memory=True,
    )
    return expose_dloader


def weak_persp_to_blender(
        targets,
        camera_scale,
        camera_transl,
        H, W,
        sensor_width=36,
        focal_length=5000):
    ''' Converts weak-perspective camera to a perspective camera
    '''
    if torch.is_tensor(camera_scale):
        camera_scale = camera_scale.detach().cpu().numpy()
    if torch.is_tensor(camera_transl):
        camera_transl = camera_transl.detach().cpu().numpy()

    output = defaultdict(lambda: [])
    for ii, target in enumerate(targets):
        orig_bbox_size = target.get_field('orig_bbox_size')
        bbox_center = target.get_field('orig_center')
        z = 2 * focal_length / (camera_scale[ii] * orig_bbox_size)

        transl = [
            camera_transl[ii, 0].item(), camera_transl[ii, 1].item(),
            z.item()]
        shift_x = - (bbox_center[0] / W - 0.5)
        shift_y = (bbox_center[1] - 0.5 * H) / W
        focal_length_in_mm = focal_length / W * sensor_width
        output['shift_x'].append(shift_x)
        output['shift_y'].append(shift_y)
        output['transl'].append(transl)
        output['focal_length_in_mm'].append(focal_length_in_mm)
        output['focal_length_in_px'].append(focal_length)
        output['center'].append(bbox_center)
        output['sensor_width'].append(sensor_width)
    for key in output:
        output[key] = np.stack(output[key], axis=0)
    return output


def undo_img_normalization(image, mean, std, add_alpha=True):
    if torch.is_tensor(image):
        image = image.detach().cpu().numpy().squeeze()

    out_img = (image * std[np.newaxis, :, np.newaxis, np.newaxis] +
               mean[np.newaxis, :, np.newaxis, np.newaxis])
    if add_alpha:
        out_img = np.pad(
            out_img, [[0, 0], [0, 1], [0, 0], [0, 0]],
            mode='constant', constant_values=1.0)
    return out_img


@torch.no_grad()
def main(
        image_folder: str,
        exp_cfg,
        show: bool = False,
        demo_output_folder: str = 'demo_output',
        pause: float = -1,
        focal_length: float = 5000,
        rcnn_batch: int = 1,
        sensor_width: float = 36,
        save_vis: bool = True,
        save_params: bool = False,
        save_mesh: bool = False,
        degrees: Optional[List[float]] = []
) -> None:
    device = torch.device('cuda')
    if not torch.cuda.is_available():
        logger.error('CUDA is not available!')
        sys.exit(3)

    logger.remove()
    logger.add(lambda x: tqdm.write(x, end=''),
               level=exp_cfg.logger_level.upper(),
               colorize=True)

    trte = 'train/' if define_training_data(image_folder) else 'test/'
    demo_output_folder = demo_output_folder + trte
    file = img_folder.split('/')[-1]

    if os.path.isfile(os.path.join(demo_output_folder, file + '.pt')):
        print(f"File {file} has already been finished....")
        exit(0)
    # image_folder = image_folder[56:88]
    expose_dloader = preprocess_images(image_folder + '/', exp_cfg, batch_size=rcnn_batch, device=device)
    logger.info(f'Saving results to: {demo_output_folder}')
    os.makedirs(demo_output_folder, exist_ok=True)

    model = SMPLXNet(exp_cfg)
    try:
        model = model.to(device=device)
    except RuntimeError:
        # Re-submit in case of a device error
        sys.exit(3)

    output_folder = exp_cfg.output_folder
    checkpoint_folder = osp.join(output_folder, exp_cfg.checkpoint_folder)
    checkpointer = Checkpointer(
        model, save_dir=checkpoint_folder, pretrained=exp_cfg.pretrained)

    arguments = {'iteration': 0, 'epoch_number': 0}
    extra_checkpoint_data = checkpointer.load_checkpoint()
    for key in arguments:
        if key in extra_checkpoint_data:
            arguments[key] = extra_checkpoint_data[key]

    model = model.eval()

    means = np.array(exp_cfg.datasets.body.transforms.mean)
    std = np.array(exp_cfg.datasets.body.transforms.std)

    render = save_vis or show
    body_crop_size = exp_cfg.get('datasets', {}).get('body', {}).get(
        'transforms').get('crop_size', 256)
    if render:
        hd_renderer = HDRenderer(img_size=body_crop_size)

    total_time = 0
    cnt = 0
    mesh_points = np.empty(shape=(0, 10475, 3), dtype=float)
    smplx_params = torch.empty(size=(0, 500))

    for bidx, batch in enumerate(tqdm(expose_dloader, dynamic_ncols=True)):

        full_imgs_list, body_imgs, body_targets = batch
        if full_imgs_list is None:
            continue

        full_imgs = to_image_list(full_imgs_list)
        body_imgs = body_imgs.to(device=device)
        body_targets = [target.to(device) for target in body_targets]
        full_imgs = full_imgs.to(device=device)

        torch.cuda.synchronize()
        start = time.perf_counter()
        model_output = model(body_imgs, body_targets, full_imgs=full_imgs,
                             device=device)
        torch.cuda.synchronize()
        elapsed = time.perf_counter() - start
        cnt += 1
        total_time += elapsed

        hd_imgs = full_imgs.images.detach().cpu().numpy().squeeze()
        body_imgs = body_imgs.detach().cpu().numpy()
        body_output = model_output.get('body')

        _, _, H, W = full_imgs.shape
        #  logger.info(f'{H}, {W}')
        #  H, W, _ = hd_imgs.shape
        if render:
            hd_imgs = np.transpose(undo_img_normalization(hd_imgs, means, std),
                                   [0, 2, 3, 1])
            hd_imgs = np.clip(hd_imgs, 0, 1.0)
            right_hand_crops = body_output.get('right_hand_crops')
            left_hand_crops = torch.flip(
                body_output.get('left_hand_crops'), dims=[-1])
            head_crops = body_output.get('head_crops')
            bg_imgs = undo_img_normalization(body_imgs, means, std)

            right_hand_crops = undo_img_normalization(
                right_hand_crops, means, std)
            left_hand_crops = undo_img_normalization(
                left_hand_crops, means, std)
            head_crops = undo_img_normalization(head_crops, means, std)

        body_output = model_output.get('body', {})
        num_stages = body_output.get('num_stages', 3)
        stage_n_out = body_output.get(f'stage_{num_stages - 1:02d}', {})
        model_vertices = stage_n_out.get('vertices', None)

        if stage_n_out is not None:
            model_vertices = stage_n_out.get('vertices', None)

        faces = stage_n_out['faces']
        if model_vertices is not None:
            model_vertices = model_vertices.detach().cpu().numpy()
            camera_parameters = body_output.get('camera_parameters', {})
            camera_scale = camera_parameters['scale'].detach()
            camera_transl = camera_parameters['translation'].detach()

        out_img = OrderedDict()

        final_model_vertices = None
        stage_n_out = model_output.get('body', {}).get('final', {})
        if stage_n_out is not None:
            final_model_vertices = stage_n_out.get('vertices', None)

        if final_model_vertices is not None:
            final_model_vertices = final_model_vertices.detach().cpu().numpy()
            camera_parameters = model_output.get('body', {}).get(
                'camera_parameters', {})
            camera_scale = camera_parameters['scale'].detach()
            camera_transl = camera_parameters['translation'].detach()

        hd_params = weak_persp_to_blender(
            body_targets,
            camera_scale=camera_scale,
            camera_transl=camera_transl,
            H=H, W=W,
            sensor_width=sensor_width,
            focal_length=focal_length,
        )

        if save_vis:
            bg_hd_imgs = np.transpose(hd_imgs, [0, 3, 1, 2])
            out_img['hd_imgs'] = bg_hd_imgs
        if render:
            # Render the initial predictions on the original image resolution
            hd_orig_overlays = hd_renderer(
                model_vertices, faces,
                focal_length=hd_params['focal_length_in_px'],
                camera_translation=hd_params['transl'],
                camera_center=hd_params['center'],
                bg_imgs=bg_hd_imgs,
                return_with_alpha=True,
            )
            out_img['hd_orig_overlay'] = hd_orig_overlays

        # Render the overlays of the final prediction
        if render:
            hd_overlays = hd_renderer(
                final_model_vertices,
                faces,
                focal_length=hd_params['focal_length_in_px'],
                camera_translation=hd_params['transl'],
                camera_center=hd_params['center'],
                bg_imgs=bg_hd_imgs,
                return_with_alpha=True,
                body_color=[0.4, 0.4, 0.7]
            )
            out_img['hd_overlay'] = hd_overlays

        for deg in degrees:
            hd_overlays = hd_renderer(
                final_model_vertices, faces,
                focal_length=hd_params['focal_length_in_px'],
                camera_translation=hd_params['transl'],
                camera_center=hd_params['center'],
                bg_imgs=bg_hd_imgs,
                return_with_alpha=True,
                render_bg=False,
                body_color=[0.4, 0.4, 0.7],
                deg=deg,
            )
            out_img[f'hd_rendering_{deg:03.0f}'] = hd_overlays

        if save_vis:
            for key in out_img.keys():
                out_img[key] = np.clip(
                    np.transpose(
                        out_img[key], [0, 2, 3, 1]) * 255, 0, 255).astype(
                    np.uint8)

        for idx in tqdm(range(len(body_targets)), 'Saving ...'):
            fname = body_targets[idx].get_field('fname')
            curr_out_path = osp.join(demo_output_folder, fname)
            # os.makedirs(curr_out_path, exist_ok=True)

            if save_vis:
                for name, curr_img in out_img.items():
                    pil_img.fromarray(curr_img[idx]).save(
                        osp.join(curr_out_path, f'{name}.png'))

            if save_mesh:
                # Store the final mesh
                expose_mesh = o3d.geometry.TriangleMesh()
                expose_mesh.vertices = Vec3d(
                    final_model_vertices[idx] + hd_params['transl'][idx])
                vertices = np.asarray(expose_mesh.vertices).reshape(1, 10475, 3)
                mesh_points = np.concatenate((mesh_points, vertices))

            if save_params:
                params_fname = osp.join(curr_out_path, f'{fname}_params.npz')
                out_params = dict(fname=fname)
                for key, val in stage_n_out.items():
                    if torch.is_tensor(val):
                        val = val.detach().cpu().numpy()[idx]
                    out_params[key] = val
                for key, val in hd_params.items():
                    if torch.is_tensor(val):
                        val = val.detach().cpu().numpy()
                    if np.isscalar(val[idx]):
                        out_params[key] = val[idx].item()
                    else:
                        out_params[key] = val[idx]
                expression = torch.FloatTensor(out_params['expression']).view(1, -1)
                jaw_pose = torch.FloatTensor(out_params['jaw_pose']).view(1, -1)
                left_hand_pose = torch.FloatTensor(out_params['left_hand_pose']).view(1, -1)
                right_hand_pose = torch.FloatTensor(out_params['right_hand_pose']).view(1, -1)
                betas = torch.FloatTensor(out_params['betas']).view(1, -1)
                global_orientation = torch.FloatTensor(out_params['global_orient']).view(1, -1)
                body_pose = torch.FloatTensor(out_params['body_pose']).view(1, -1)
                transl = torch.FloatTensor(out_params['transl']).view(1, -1)
                params = torch.cat((expression, jaw_pose, left_hand_pose, right_hand_pose, betas, global_orientation,
                                    body_pose, transl), 1)
                smplx_params = torch.cat((smplx_params, params))
                # np.savez_compressed(params_fname, **out_params)

            if show:
                nrows = 1
                ncols = 4 + len(degrees)
                fig, axes = plt.subplots(
                    ncols=ncols, nrows=nrows, num=0,
                    gridspec_kw={'wspace': 0, 'hspace': 0})
                axes = axes.reshape(nrows, ncols)
                for ax in axes.flatten():
                    ax.clear()
                    ax.set_axis_off()

                axes[0, 0].imshow(hd_imgs[idx])
                axes[0, 1].imshow(out_img['rgb'][idx])
                axes[0, 2].imshow(out_img['hd_orig_overlay'][idx])
                axes[0, 3].imshow(out_img['hd_overlay'][idx])
                start = 4
                for deg in degrees:
                    axes[0, start].imshow(
                        out_img[f'hd_rendering_{deg:03.0f}'][idx])
                    start += 1

                plt.draw()
                if pause > 0:
                    plt.pause(pause)
                else:
                    plt.show()

    if save_mesh:
        mesh_pt_name = '/mnt/Data/Datasets/Own/Mesh/RGB/'
        image_folder_splited = image_folder.split('/')
        pt_name = image_folder_splited[len(image_folder_splited) - 1]
        mesh_pt_name += pt_name + '_rgb.pt'
        torch.save(torch.FloatTensor(mesh_points), mesh_pt_name)
        print(f"Generated {pt_name} Shape {mesh_points.shape}")
        logger.info(f'Average inference time: {total_time / cnt}')
    if save_params:
        pt_name = file + '.pt'
        torch.save(smplx_params, os.path.join(demo_output_folder, pt_name))


def define_training_data(filename):
    train_set = ['P001', 'P002', 'P004', 'P005', 'P008', 'P009', 'P013', 'P014', 'P015', 'P016', 'P017', 'P018', 'P019',
                 'P025', 'P027', 'P028', 'P031', 'P034', 'P035', 'P038']
    for n in train_set:
        if n in filename:
            return True
    return False


if __name__ == '__main__':
    torch.backends.cudnn.benchmark = True
    torch.backends.cudnn.deterministic = False

    arg_formatter = argparse.ArgumentDefaultsHelpFormatter
    description = 'PyTorch SMPL-X Regressor Demo'
    parser = argparse.ArgumentParser(formatter_class=arg_formatter,
                                     description=description)

    parser.add_argument('--img-folder', type=str, default='S001C001P001R001A002')
    parser.add_argument('--exp-cfg', type=str, default='data/conf.yaml')
    parser.add_argument('--output-folder', default='/mnt/d/yzk/NTU/base_joint_pt_single/', type=str)
    parser.add_argument('--exp-opts', default=[], dest='exp_opts')
    parser.add_argument('--datasets', nargs='+',
                        default=['openpose'], type=str)
    parser.add_argument('--show', default=False,
                        type=lambda arg: arg.lower() in ['true'])
    parser.add_argument('--expose-batch',
                        dest='expose_batch',
                        default=1, type=int,
                        help='ExPose batch size')
    parser.add_argument('--rcnn-batch',
                        dest='rcnn_batch',
                        default=1, type=int,
                        help='R-CNN batch size')
    parser.add_argument('--pause', default=-1, type=float,
                        help='How much to pause the display')
    parser.add_argument('--focal-length', dest='focal_length', type=float,
                        default=5000,
                        help='Focal length')
    parser.add_argument('--degrees', type=float, nargs='*', default=[],
                        help='Degrees of rotation around the vertical axis')
    parser.add_argument('--save-vis', dest='save_vis', default=False,
                        type=lambda x: x.lower() in ['true'],
                        help='Whether to save visualizations')
    parser.add_argument('--save-mesh', dest='save_mesh', default=False,
                        type=lambda x: x.lower() in ['true'],
                        help='Whether to save meshes')
    parser.add_argument('--save-params', dest='save_params', default=True,
                        type=lambda x: x.lower() in ['true'],
                        help='Whether to save parameters')

    cmd_args = parser.parse_args()

    img_folder = cmd_args.img_folder
    show = cmd_args.show
    output_folder = cmd_args.output_folder
    pause = cmd_args.pause
    focal_length = cmd_args.focal_length
    save_vis = cmd_args.save_vis
    save_params = cmd_args.save_params
    save_mesh = cmd_args.save_mesh
    degrees = cmd_args.degrees
    expose_batch = cmd_args.expose_batch
    rcnn_batch = cmd_args.rcnn_batch

    cfg.merge_from_file(cmd_args.exp_cfg)
    cfg.merge_from_list(cmd_args.exp_opts)

    cfg.datasets.body.batch_size = expose_batch

    cfg.is_training = False
    cfg.datasets.body.splits.test = cmd_args.datasets
    use_face_contour = cfg.datasets.use_face_contour
    set_face_contour(cfg, use_face_contour=use_face_contour)

    with threadpool_limits(limits=1):
        main(
            image_folder=img_folder,
            exp_cfg=cfg,
            show=show,
            demo_output_folder=output_folder,
            pause=pause,
            focal_length=focal_length,
            save_vis=save_vis,
            save_mesh=save_mesh,
            save_params=save_params,
            degrees=degrees,
            rcnn_batch=rcnn_batch,
        )
