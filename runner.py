# -*- coding: utf-8 -*-

# Max-Planck-Gesellschaft zur Förderung der Wissenschaften e.V. (MPG) is
# holder of all proprietary rights on this computer program.
# You can only use this computer program if you have closed
# a license agreement with MPG or you get the right to use the computer
# program from someone who is authorized to grant you that right.
# Any use of the computer program without a valid license is prohibited and
# liable to prosecution.
#
# Copyright©2022 Max-Planck-Gesellschaft zur Förderung
# der Wissenschaften e.V. (MPG). acting on behalf of its Max Planck Institute
# for Intelligent Systems. All rights reserved.
#
# Contact: mica@tue.mpg.de


import os, sys
import random
from glob import glob
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.backends.cudnn as cudnn
from insightface.app import FaceAnalysis
from insightface.app.common import Face
from insightface.utils import face_align
from loguru import logger
from pytorch3d.io import save_ply, save_obj
from skimage.io import imread
from tqdm import tqdm

from configs.config import get_cfg_defaults
from datasets.creation.util import get_arcface_input, get_center
from utils import util

INPUT = "../age-classification/data/generated-faces/generated.photos"
OUTPUT = "../age-classification/data/generated-faces/mica"
ARCFACE = "../age-classification/data/generated-faces/arcface"


def deterministic(rank):
    torch.manual_seed(rank)
    torch.cuda.manual_seed(rank)
    np.random.seed(rank)
    random.seed(rank)

    cudnn.deterministic = True
    cudnn.benchmark = False


def process(app, image_size=224):
    dst = Path(ARCFACE)
    dst.mkdir(parents=True, exist_ok=True)
    processes = []
    image_paths = sorted(glob(INPUT + '/*.*'))
    for image_path in tqdm(image_paths):
        name = Path(image_path).stem
        img = cv2.imread(image_path)
        bboxes, kpss = app.det_model.detect(img, max_num=0, metric='default')
        if bboxes.shape[0] == 0:
            continue
        i = get_center(bboxes, img)
        bbox = bboxes[i, 0:4]
        det_score = bboxes[i, 4]
        kps = None
        if kpss is not None:
            kps = kpss[i]
        face = Face(bbox=bbox, kps=kps, det_score=det_score)
        blob, aimg = get_arcface_input(face, img)
        file = str(Path(dst, name))
        np.save(file, blob)
        cv2.imwrite(file + '.jpg', face_align.norm_crop(img, landmark=face.kps, image_size=image_size))
        processes.append(file + '.npy')

    return processes


def to_batch(path):
    src = path.replace('npy', 'jpg')
    if not os.path.exists(src):
        src = path.replace('npy', 'png')

    image = imread(src)[:, :, :3]
    image = image / 255.
    image = cv2.resize(image, (224, 224)).transpose(2, 0, 1)
    image = torch.tensor(image).cuda()[None]

    arcface = np.load(path)
    arcface = torch.tensor(arcface).cuda()[None]

    return image, arcface


def load_checkpoint(args, mica):
    checkpoint = torch.load(args)
    if 'arcface' in checkpoint:
        mica.arcface.load_state_dict(checkpoint['arcface'])
    if 'flameModel' in checkpoint:
        mica.flameModel.load_state_dict(checkpoint['flameModel'])


def main(cfg):
    device = 'cuda:0'
    cfg.model.testing = True
    mica = util.find_model_using_name(model_dir='micalib.models', model_name=cfg.model.name)(cfg, device)
    load_checkpoint("data/pretrained/mica.tar", mica)
    mica.eval()

    faces = mica.render.faces[0].cpu()
    Path("output").mkdir(exist_ok=True, parents=True)

    app = FaceAnalysis(name='antelopev2', providers=['CUDAExecutionProvider'])
    app.prepare(ctx_id=0, det_size=(224, 224))

    with torch.no_grad():
        logger.info(f'Processing has started...')
        paths = process(app)
        for path in tqdm(paths):
            name = Path(path).stem
            images, arcface = to_batch(path)
            codedict = mica.encode(images, arcface)
            opdict = mica.decode(codedict)
            meshes = opdict['pred_canonical_shape_vertices']
            code = opdict['pred_shape_code']
            lmk = mica.flame.compute_landmarks(meshes)

            mesh = meshes[0]
            landmark_51 = lmk[0, 17:]
            landmark_7 = landmark_51[[19, 22, 25, 28, 16, 31, 37]]
            rendering = mica.render.render_mesh(mesh[None])
            image = (rendering[0].cpu().numpy().transpose(1, 2, 0).copy() * 255)[:, :, [2, 1, 0]]
            image = np.minimum(np.maximum(image, 0), 255).astype(np.uint8)

            dst = Path(OUTPUT)
            dst.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(f'{OUTPUT}/{name}.jpg', image)
            # save_ply(f'{dst}/mesh.ply', verts=mesh.cpu() * 1000.0, faces=faces)  # save in millimeters
            save_obj(f'{OUTPUT}/{name}.obj', verts=mesh.cpu() * 1000.0, faces=faces)
            # np.save(f'{dst}/identity', code[0].cpu().numpy())
            # np.save(f'{dst}/kpt7', landmark_7.cpu().numpy() * 1000.0)
            # np.save(f'{dst}/kpt68', lmk.cpu().numpy() * 1000.0)

        logger.info(f'Processing finished. Results has been saved in {OUTPUT}')


if __name__ == '__main__':
    # parser = argparse.ArgumentParser(description='MICA - Towards Metrical Reconstruction of Human Faces')
    # parser.add_argument('-i', default='demo/input', type=str, help='Input folder with images')
    # parser.add_argument('-o', default='demo/output', type=str, help='Output folder')
    # parser.add_argument('-a', default='demo/arcface', type=str, help='Processed images for MICA input')
    # parser.add_argument('-m', default='data/pretrained/mica.tar', type=str, help='Pretrained model path')

    cfg = get_cfg_defaults()
    deterministic(42)
    main(cfg)

