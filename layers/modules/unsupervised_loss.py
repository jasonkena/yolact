# -*- coding: utf-8 -*-
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pad_sequence
from data.config import cfg
from utils import gaussian, timer
import math
from ae import AutoEncoder
import pdb

from torch.utils.tensorboard import SummaryWriter

writer = SummaryWriter()
iter_counter = 0


class UnsupervisedLoss(nn.Module):
    def __init__(self):
        super(UnsupervisedLoss, self).__init__()
        self.variance = VarianceLoss()
        self.autoencoder = AutoEncoder()
        self.num_classes = cfg.num_classes  # Background included
        self.background_label = 0
        self.top_k = cfg.nms_top_k
        self.nms_thresh = cfg.nms_thresh
        self.conf_thresh = cfg.nms_conf_thresh

        if self.nms_thresh <= 0:
            raise ValueError("nms_threshold must be non negative.")

    def forward(self, original, predictions):
        global iter_counter
        losses = {}
        # loc shape: torch.size(batch_size,num_priors,6)
        loc_data = predictions["loc"]
        # conf shape: torch.size(batch_size,num_priors,num_classes)
        conf_data = predictions["conf"]
        # Softmaxed confidence in Foreground
        # Shape: batch,num_priors
        conf_data = F.softmax(conf_data, dim=2)[:, :, 1]
        # masks shape: torch.size(batch_size,num_priors,mask_dim)
        mask_data = predictions["mask"]
        # proto* shape: torch.size(batch_size,mask_h,mask_w,mask_dim)
        proto_data = predictions["proto"]

        with timer.env("Detect"):
            # decoded boxes with size [num_priors, 4]
            all_results = self.detect(conf_data, loc_data, mask_data)
            keep = all_results["keep"]
            # AE Scaled loss needs non-kept Detections
            losses["ae_loss"] = self.ae_scaled_loss(
                original[batch_idx],
                all_results["iou"],
                all_results["keep"],
                all_results["loc"],
                all_results["conf"],
            )

            # IoU not included because not needed by variance
            filtered_result = {
                "loc": all_results["loc"][keep],
                "mask": all_results["mask"][keep],
                "conf": all_results["conf"][keep],
                "proto": proto_data[batch_idx],
            }
            out.append(filtered_result)

        losses["variance_loss"] = self.variance(original, out) / 100
        print(losses)
        iter_counter += 1
        return losses

    def detect(self, conf, loc, mask):
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
        # NOTE: Potential sorting inefficiency
        sorted_conf, idx = conf.sort(dim=-1, descending=True)
        sorted_conf = sorted_conf[:, :top_k_conf]
        # loc with size [batch,num_priors, 4], or 5 for unsup
        sorted_loc = loc[idx][:, :top_k_conf]
        # masks shape: Shape: [num_priors, mask_dim]
        sorted_mask = mask[idx][:, :top_k_conf]

        # Dim: Batch, Detections, i,j
        gauss = gaussian.unnormalGaussian(maskShape=cfg.iou_gauss_dim, loc=sorted_loc)

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

        # Batch, Detections, Detections
        gauss_iou = gauss_intersection / (gauss_union)

        # Batch, Detections
        iou_max, _ = gauss_iou.triu(diagonal=1).max(dim=1)

        # From lowest to highest IoU
        _, sorted_iou_idx = iou_max.sort(descending=False, dim=-1)
        sorted_iou_idx = sorted_iou_idx[:, :top_k_iou]

        return {
            "iou": gauss_iou,
            "loc": sorted_loc,
            "mask": sorted_mask,
            "conf": sorted_conf,
            "keep": sorted_iou_idx,
        }

    def ae_scaled_loss(self, original, iou, keep, loc, conf):
        # __import__("pdb").set_trace()
        # AE input should be of size [batch_size, 3, img_h, img_w]
        # conf shape: torch.size(batch_size,num_priors)

        # conf matrix: torch.size(batch_size,num_priors,num_priors)
        conf_matrix = conf.unsqueeze(1).permute(1, 2) @ conf.unsqueeze(1)
        # Remove Lower Triangle and Diagonal
        final_scale = iou * conf_matrix.triu(1)

        # Dim batch,priors
        # try:
        ae_loss = self.autoencoder(original, loc[keep])
        # except RuntimeError:
        # pdb.set_trace()
        # ae_loss but in scores
        # batch, priors, priors
        ae_grid = torch.zeros_like(final_scale)
        try:
            ae_grid[keep] = ae_loss.unsqueeze(2).repeat(1, 1, ae_grid.size(2))
        except RuntimeError:
            pdb.set_trace()
        ae_grid = ae_grid * final_scale

        return torch.sum(ae_grid)


class MyException(Exception):
    pass


class VarianceLoss(nn.Module):
    def __init__(self):
        super(VarianceLoss, self).__init__()
        pass

    def forward(self, original, loc, mask, conf, proto):
        global iter_counter
        original = original.float()
        # original is [batch_size, 3, img_h, img_w]

        # This is correct, because of the tranpose above
        # conf shape: torch.size(batch_size,num_priors,num_classes)
        # predictions is array of Dicts from detect
        # boxes=loc Shape: [num_priors, 5]

        resizeShape = list(original.shape)[-2:]

        # Assuming it has no batch
        priors_shape = list([a["conf"].size(0) for a in predictions])

        # proto* shape: torch.size(batch,mask_h,mask_w,mask_dim)
        # proto_shape = list(predictions[0]["proto"].shape)[:2]
        proto_shape = list(proto.shape)[1:3]

        print("loc", torch.isnan(loc).any())
        # batch, num_priors, i,j, with Padded sequence
        unnormalGaussian = gaussian.unnormalGaussian(maskShape=proto_shape, loc=loc)
        writer.add_image(
            "unnormalGaussian", unnormalGaussian[0, 0], iter_counter, dataformats="HW"
        )
        print("unnormalGaussian", torch.isnan(unnormalGaussian).any())
        print("unnormalGaussian positive", (unnormalGaussian >= 0).all())

        # Dim: Batch, Anchors, i, j
        assembledMask = gaussian.lincomb(proto=proto, masks=masks)
        writer.add_image("lincomb", assembledMask[0, 0], iter_counter, dataformats="HW")
        assembledMask = torch.sigmoid(assembledMask)

        print("assembledMask", torch.isnan(assembledMask).any())
        if torch.isnan(assembledMask).any():
            __import__("pdb").set_trace()
        print("assembledMask positive", (assembledMask >= 0).all())

        # Dim: Batch, Anchors, i, j
        attention = assembledMask * unnormalGaussian
        print("attention", torch.isnan(attention).any())
        print("attention positive", (attention >= 0).all())
        writer.add_image("attention", attention[0, 0], iter_counter, dataformats="HW")

        # conf shape: torch.size(batch_size,num_priors) #no num_classes
        # Batch, Anchors, i,j
        maskConfidence = torch.einsum("abcd,ab->abcd", attention, conf)
        print("maskConfidence", torch.isnan(maskConfidence).any())
        print("mask confidence positive", (maskConfidence >= 0).all())
        print("mask confidence under one", (maskConfidence <= 1).all())
        writer.add_image(
            "maskConfidence", maskConfidence[0, 0], iter_counter, dataformats="HW"
        )
        # logConf = torch.log(maskConfidence)

        # unsup: REMOVE CHANNEL DIMENSION HERE, since Pad_sequence is summed, it does nothing
        # Confidence in background, see desmos
        # Dim: batch, h, w
        finalConf = 1 - torch.sum(
            (maskConfidence ** 2)
            / (
                torch.sum(maskConfidence, dim=1, keepdim=True).repeat(
                    1, maskConfidence.size(1), 1, 1
                )
                + cfg.positive
            ),
            dim=1,
        )
        finalConf = torch.where(
            torch.isnan(finalConf), torch.zeros_like(finalConf), finalConf
        )
        writer.add_image("finalConf", finalConf[0], iter_counter, dataformats="HW")
        # Might be undefined because some batches don't have maximum number of detections
        print("finalConf", torch.isnan(finalConf).any())
        if torch.isnan(finalConf).any():
            __import__("pdb").set_trace()
        print("finalconf positive", (finalConf >= 0).all())
        # if not (finalConf >= 0).all():
        # pdb.set_trace()

        # finalConf = 1 - torch.sum(F.softmax(logConf, dim=1) * maskConfidence, dim=1)

        # Resize to Original Image Size, add fake depth
        # unsup: Interpolation may be incorrect
        # Dim batch, img_h, img_w
        resizedConf = F.interpolate(
            finalConf.unsqueeze(1), resizeShape, mode="bilinear", align_corners=False
        )[:, 0]
        writer.add_image("resizedConf", resizedConf[0], iter_counter, dataformats="HW")
        print("resizedConf", torch.isnan(resizedConf).any())
        print("resizedConf positive", (resizedConf >= 0).all())
        # if cfg.use_amp:
        # finalConf = finalConf.half()
        # resizedConf = resizedConf.half()

        # unsup: AGGREGATING RESULTS BETWEEN BATCHES HERE
        # Dim: h, w
        totalConf = torch.sum(resizedConf, dim=0)
        writer.add_image("totalConf", totalConf, iter_counter, dataformats="HW")
        print("totalConf", torch.isnan(totalConf).any())
        print("totalconf all positive", (totalConf >= 0).all())
        print("TOTAL_CONF {}".format(totalConf))

        # Dim 3, img_h, img_w
        print("original", torch.isnan(original).any())
        print("original all positive", (original >= 0).all())
        weightedMean = torch.einsum("abcd,acd->bcd", original, resizedConf)
        writer.add_image("weightedMean", weightedMean, iter_counter)
        print("weightedMean", torch.isnan(weightedMean).any())

        # Dim: batch, 3, img_h, img_w
        squaredDiff = (original - weightedMean) ** 2
        print("squaredDiff", torch.isnan(squaredDiff).any())
        # if cfg.use_amp:
        # squaredDiff = squaredDiff.half()

        # Batch,3,img_h, image w
        weightedDiff = torch.einsum("abcd,acd->abcd", squaredDiff, resizedConf)
        writer.add_image("weightedDiff", weightedDiff[0], iter_counter)
        print("weightedDiff", torch.isnan(weightedDiff).any())
        # print("TOTAL CONF {}".format(totalConf + cfg.positive))
        # print("CFG POSITIVE {}".format(cfg.positive))
        # weightedDiff = torch.where(torch.isnan(weightedDiff), torch.zeros_like(totalConf), weightedDiff)
        # totalConf = torch.where(torch.isnan(totalConf), torch.ones_like(totalConf), totalConf)
        # sTotalConf = torch.tensor(totalConf.shape).fill_(cfg.positive) if torch.isnan(totalConf).any() else (totalConf + cfg.positive)
        weightedVariance = weightedDiff / (totalConf + cfg.positive)
        writer.add_image("weightedVariance", weightedVariance[0], iter_counter)
        print("weightedVariance", torch.isnan(weightedVariance).any())

        # result = torch.mean(weightedVariance, dim=(2, 3))
        # result = torch.sum(result)
        result = torch.sum(weightedVariance)
        print("resulttype", result.type())
        print("result", torch.isnan(result).any())

        # if cfg.use_amp:
        # return result.half()

        # Normalize by number of elements in original image
        return result

    # was normalized by /original.numel()


# class ScaledAutoencoderLoss(nn.Module):
# def __init__(self, img_h, img_w):
# super(ScaledAutoencoderLoss, self).__init__()
# pass

