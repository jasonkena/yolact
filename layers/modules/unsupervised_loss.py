# -*- coding: utf-8 -*-
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pad_sequence
from data.config import cfg
from utils import gaussian, timer
import math
from ae import AutoEncoder, init_weights
import pdb
from torchvision.utils import make_grid

from torch.utils.tensorboard import SummaryWriter
import dynamic

import torchsnooper
import snoop

torchsnooper.register_snoop()


def unsup_writer(Writer):
    global writer
    writer = Writer


def unsup_iter(Iteration):
    global iteration
    iteration = Iteration


# https://stackoverflow.com/questions/55628014/indexing-a-3d-tensor-using-a-2d-tensor
def twoIndexThree(tensor, index):
    return tensor[torch.arange(tensor.shape[0]).unsqueeze(-1), index]


class UnsupervisedLoss(nn.Module):
    def __init__(self):
        super(UnsupervisedLoss, self).__init__()
        self.variance = VarianceLoss()

        self.autoencoder = AutoEncoder()
        self.autoencoder.apply(init_weights)

        self.num_classes = cfg.num_classes  # Background included
        self.background_label = 0
        self.top_k = cfg.nms_top_k
        self.nms_thresh = cfg.nms_thresh
        self.conf_thresh = cfg.nms_conf_thresh

        if self.nms_thresh <= 0:
            raise ValueError("nms_threshold must be non negative.")

    def forward(self, original, pred):
        # predictions: ['loc', 'conf', 'mask', 'proto', 'background', 'proto_x'])
        # loc shape: torch.size(batch_size,num_priors,6)
        # masks shape: torch.size(batch_size,num_priors,mask_dim)
        # proto* shape: torch.size(batch_size,mask_h,mask_w,mask_dim)
        # proto_x* shape: torch.size(batch_size, cfg.num_features, i,j)
        # background shape: torch.size(batch_size,3,i,j)

        # conf shape: torch.size(batch_size,num_priors,num_classes)
        # Softmaxed confidence in Foreground
        # Shape: batch,num_priors
        pred["conf"] = F.softmax(pred["conf"], dim=2)[:, :, 1]
        # add a batch dimension
        pred["priors"] = pred["priors"].unsqueeze(0).repeat(original.size(0), 1, 1)

        losses = {}
        with timer.env("Detect"):
            # detections
            dets = self.detect(pred["conf"], pred["loc"], pred["mask"], pred["priors"])
            keep = dets["keep"]

            pred["loc"] = dets["loc"][
                torch.arange(dets["loc"].shape[0]).unsqueeze(-1), keep
            ]
            pred["mask"] = dets["mask"][
                torch.arange(dets["mask"].shape[0]).unsqueeze(-1), keep
            ]
            pred["priors"] = dets["priors"][
                torch.arange(dets["priors"].shape[0]).unsqueeze(-1), keep
            ]
            pred["conf"] = torch.gather(dets["conf"], 1, keep)
            pred["reconstruction"] = self.autoencoder(
                original, pred["proto_x"], pred["loc"], pred["priors"]
            )

            losses["background"], losses["reconstruction"] = self.variance(
                original,
                pred["loc"],
                pred["mask"],
                pred["conf"],
                pred["proto"],
                pred["reconstruction"],
                pred["priors"],
            )

            scalers = dynamic.read()
            writer.add_scalars("Scalers/", scalers, iteration)
            writer.add_scalars("Raw_Losses/", losses, iteration)

            for key in losses:
                losses[key] = scalers[key] * losses[key]

            writer.add_scalars("Scaled_Losses/", losses, iteration)

            pred["losses"] = losses
        return pred

    def detect(self, conf, loc, mask, priors):
        # IoU Threshold, Conf_Threshold, IoU_Thresh
        """
        NO BATCH
        boxes=loc Shape: [num_priors, 4]
        masks: [batch, num_priors, mask_dim]
        """
        # unsup: Background isn't NMSed
        """ Perform nms for only the max scoring class that isn't background (class 0) """
        top_k_conf = cfg.nms_top_k_conf
        top_k_iou = cfg.nms_top_k_iou

        # __import__("pdb").set_trace()
        # conf shape: torch.size(batch_size,num_priors)
        sorted_conf, sorted_conf_idx = torch.topk(
            conf, top_k_conf, dim=-1, largest=True
        )
        # loc with size [batch,num_priors, 4], or 5 for unsup
        sorted_loc = loc[torch.arange(loc.shape[0]).unsqueeze(-1), sorted_conf_idx]
        # masks shape: Shape: [batch,num_priors, mask_dim]
        sorted_mask = mask[torch.arange(mask.shape[0]).unsqueeze(-1), sorted_conf_idx]
        sorted_priors = priors[
            torch.arange(priors.shape[0]).unsqueeze(-1), sorted_conf_idx
        ]

        # Dim: Batch, Detections, i,j
        gauss = gaussian.unnormalGaussian(
            maskShape=cfg.iou_gauss_dim, loc=sorted_loc, priors=sorted_priors
        )

        gaussShape = list(gauss.shape)
        # jaccard overlap: (tensor) Shape: [box_a.size(0), box_b.size(0)]
        gauss_rows = gauss.view(
            gaussShape[0], 1, gaussShape[1], *gaussShape[2:]
        ).repeat(1, gaussShape[1], 1, 1, 1)
        gauss_cols = gauss.view(
            gaussShape[0], gaussShape[1], 1, *gaussShape[2:]
        ).repeat(1, 1, gaussShape[1], 1, 1)
        # Batch, Detections, Detections, 2, I,J
        gauss_grid = torch.stack([gauss_rows, gauss_cols], dim=3)

        # [0] is to remove index
        gauss_intersection = torch.sum(torch.min(gauss_grid, dim=3)[0], dim=[3, 4])
        gauss_union = torch.sum(torch.max(gauss_grid, dim=3)[0], dim=[3, 4])

        # Positive required to not make NaN
        # Batch, Detections, Detections
        gauss_iou = gauss_intersection / (gauss_union + cfg.positive)

        # Batch, Detections
        iou_max, _ = gauss_iou.triu(diagonal=1).max(dim=1)

        # From lowest to highest IoU
        _, sorted_iou_idx = torch.topk(iou_max, top_k_iou, dim=-1, largest=False)

        return {
            "loc": sorted_loc,
            "mask": sorted_mask,
            "conf": sorted_conf,
            "priors": sorted_priors,
            "keep": sorted_iou_idx,
        }


class MyException(Exception):
    pass


class VarianceLoss(nn.Module):
    def __init__(self):
        super(VarianceLoss, self).__init__()
        pass

    def forward(self, original, loc, mask, conf, proto, reconstruction, priors):
        log = iteration % 20 == 0
        # original is [batch_size, 3, img_h, img_w]
        original = original.float()
        if log:
            writer.add_images(
                "variance/0_original", original, iteration, dataformats="NCHW",
            )

        originalShape = list(original.shape)[-2:]

        batch = original.size(0)
        # Batch, Priors, 3, i,j
        reconstructionShape = list(reconstruction.shape)
        # Batch, 3, i,j

        # proto* shape: torch.size(batch,mask_h,mask_w,mask_dim)
        # proto_shape = list(predictions[0]["proto"].shape)[:2]
        proto_shape = list(proto.shape)[1:3]

        unnormalGaussian = gaussian.unnormalGaussian(
            maskShape=proto_shape, loc=loc, priors=priors
        )

        # Display multiple images in Batch[0]
        if log:
            writer.add_images(
                "variance/1_unnormalGaussian",
                unnormalGaussian.view(-1, *proto_shape).unsqueeze(-1),
                iteration,
                dataformats="NHWC",
            )

        # Dim: Batch, Anchors, i, j
        assembledMask = gaussian.lincomb(proto=proto, masks=mask)
        assembledMask = torch.sigmoid(assembledMask)
        if log:
            writer.add_images(
                "variance/2_lincomb",
                assembledMask.reshape(-1, *proto_shape).unsqueeze(-1),
                iteration,
                dataformats="NHWC",
            )

        attention = assembledMask * unnormalGaussian
        if log:
            writer.add_images(
                "variance/3_attention",
                attention.reshape(-1, *proto_shape).unsqueeze(-1),
                iteration,
                dataformats="NHWC",
            )

        # conf shape: torch.size(batch_size,num_priors)
        # Batch, Anchors, i,j
        maskConfidence = torch.einsum("abcd,ab->abcd", attention, conf)
        if log:
            writer.add_images(
                "variance/4_maskConfidence",
                maskConfidence.reshape(-1, *proto_shape).unsqueeze(-1),
                iteration,
                dataformats="NHWC",
            )

        maskConfidence = F.interpolate(
            maskConfidence, originalShape, mode="bilinear", align_corners=False,
        )
        # Batch, i,j
        # if NaN, use Softmax, because no possible undefined
        # SoftMax to approximate max, but we still need the proportion
        # for use in background weights, to discourage overlapping
        # maximumConf = torch.max(
        # torch.softmax(maskConfidence, dim=1) * maskConfidence, dim=1
        # )[0]
        finalConf = (
            1
            - torch.max(
                maskConfidence ** 2
                / (torch.sum(maskConfidence, dim=1, keepdim=True) + cfg.positive),
                dim=1,
            )[0]
        )
        if log:
            writer.add_images(
                "variance/5_finalConf",
                finalConf.unsqueeze(1),
                iteration,
                dataformats="NCHW",
            )

        # img_h, img_w
        total_bg = torch.sum(finalConf, dim=0)
        if log:
            writer.add_image(
                "variance/6_total_bg", total_bg, iteration, dataformats="HW",
            )

        # Dim 3, img_h, img_w
        weightedMean = torch.einsum("abcd,acd->bcd", original, finalConf) / total_bg
        if log:
            writer.add_image("variance/7_weightedMean", weightedMean, iteration)
        # Dim: batch, 3, img_h, img_w
        squaredDiff = (original - weightedMean) ** 2
        if log:
            writer.add_images("variance/8_squaredDiff", squaredDiff, iteration)
        # Batch,3,img_h, image w
        weightedDiff = torch.einsum("abcd,acd->abcd", squaredDiff, finalConf)
        if log:
            writer.add_images("variance/9_weightedDiff", weightedDiff, iteration)

        weightedVariance = weightedDiff / (total_bg + cfg.positive)
        if log:
            writer.add_images(
                "variance/10_weightedVariance", weightedVariance, iteration
            )

        if log:
            writer.add_images(
                "variance/11_reconstruction",
                reconstruction.view(-1, 3, *reconstructionShape[-2:]),
                iteration,
                dataformats="NCHW",
            )

        # Batch, Priors, 3, i,j -> Batch*priors, 3, i,j
        # reconstruction = reconstruction.view(-1, 3, *reconstructionShape[-2:])
        # reconstruction = F.interpolate(
        # reconstruction, originalShape, mode="bilinear", align_corners=False,
        # )
        # reconstruction = reconstruction.view(
        # *reconstructionShape[:2], 3, *originalShape
        # )

        # batch, priors, 3, i, j
        # original = original.unsqueeze(1).repeat(1, reconstructionShape[1], 1, 1, 1)
        # NOTE: if it breaks after this, restore previous
        original = original.unsqueeze(1)
        # batch, priors, i, j
        reconstructionLoss = torch.sum((reconstruction - original) ** 2, dim=2)
        writer.add_images(
            "variance/12_reconstructionLoss",
            reconstructionLoss.view(-1, *originalShape).unsqueeze(-1),
            iteration,
            dataformats="NHWC",
        )
        reconstructionLoss = reconstructionLoss * maskConfidence
        writer.add_images(
            "variance/13_reconstructionLoss",
            reconstructionLoss.view(-1, *originalShape).unsqueeze(-1),
            iteration,
            dataformats="NHWC",
        )

        # NOTE NOTE NOTE Disabling background Loss Completely
        # backgroundLoss = torch.mean(weightedVariance)
        backgroundLoss = torch.sum(weightedVariance - weightedVariance)

        reconstructionLoss = torch.mean(reconstructionLoss)
        return backgroundLoss, reconstructionLoss
