import os
import numpy as np
import imageio.v3
import json
import time
import torch
import torch.nn as nn
import torch.nn.functional as F
import lpips


from torch.utils.tensorboard import SummaryWriter
from skimage.metrics import structural_similarity as ssim
from tqdm import tqdm, trange

import matplotlib.pyplot as plt

from run_nerf_helpers import *

from load_llff import load_llff_data
from load_deepvoxels import load_dv_data
from load_blender import load_blender_data
from load_LINEMOD import load_LINEMOD_data


loss_fn_lpips = lpips.LPIPS(net="vgg")  # best forward scores
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
np.random.seed(0)
DEBUG = False


def batchify(fn, chunk):
    """Constructs a version of 'fn' that applies to smaller batches."""
    if chunk is None:
        return fn

    def ret(inputs):
        return torch.cat(
            [fn(inputs[i : i + chunk]) for i in range(0, inputs.shape[0], chunk)], 0
        )

    return ret


def run_network(inputs, viewdirs, fn, embed_fn, embeddirs_fn, netchunk=1024 * 64):
    """Prepares inputs and applies network 'fn'."""
    inputs_flat = torch.reshape(inputs, [-1, inputs.shape[-1]])
    embedded = embed_fn(inputs_flat)

    if viewdirs is not None:
        input_dirs = viewdirs[:, None].expand(inputs.shape)
        input_dirs_flat = torch.reshape(input_dirs, [-1, input_dirs.shape[-1]])
        embedded_dirs = embeddirs_fn(input_dirs_flat)
        embedded = torch.cat([embedded, embedded_dirs], -1)

    outputs_flat = batchify(fn, netchunk)(embedded)
    outputs = torch.reshape(
        outputs_flat, list(inputs.shape[:-1]) + [outputs_flat.shape[-1]]
    )
    return outputs


def batchify_rays(rays_flat, chunk=1024 * 32, **kwargs):
    """Render rays in smaller minibatches to avoid OOM."""
    all_ret = {}
    for i in range(0, rays_flat.shape[0], chunk):
        ret = render_rays(rays_flat[i : i + chunk], **kwargs)
        for k in ret:
            if k not in all_ret:
                all_ret[k] = []
            all_ret[k].append(ret[k])

    all_ret = {k: torch.cat(all_ret[k], 0) for k in all_ret}
    return all_ret


def render(
    H,
    W,
    K,
    chunk=1024 * 32,
    rays=None,
    c2w=None,
    ndc=True,
    near=0.0,
    far=1.0,
    use_viewdirs=False,
    c2w_staticcam=None,
    **kwargs,
):
    """Render rays
    Args:
      H: int. Height of image in pixels.
      W: int. Width of image in pixels.
      focal: float. Focal length of pinhole camera.
      chunk: int. Maximum number of rays to process simultaneously. Used to
        control maximum memory usage. Does not affect final results.
      rays: array of shape [2, batch_size, 3]. Ray origin and direction for
        each example in batch.
      c2w: array of shape [3, 4]. Camera-to-world transformation matrix.
      ndc: bool. If True, represent ray origin, direction in NDC coordinates.
      near: float or array of shape [batch_size]. Nearest distance for a ray.
      far: float or array of shape [batch_size]. Farthest distance for a ray.
      use_viewdirs: bool. If True, use viewing direction of a point in space in model.
      c2w_staticcam: array of shape [3, 4]. If not None, use this transformation matrix for
       camera while using other c2w argument for viewing directions.
    Returns:
      rgb_map: [batch_size, 3]. Predicted RGB values for rays.
      disp_map: [batch_size]. Disparity map. Inverse of depth.
      acc_map: [batch_size]. Accumulated opacity (alpha) along a ray.
      extras: dict with everything returned by render_rays().
    """
    if c2w is not None:
        # special case to render full image
        rays_o, rays_d = get_rays(H, W, K, c2w)
    else:
        # use provided ray batch
        rays_o, rays_d = rays

    if use_viewdirs:
        # provide ray directions as input
        viewdirs = rays_d
        if c2w_staticcam is not None:
            # special case to visualize effect of viewdirs
            rays_o, rays_d = get_rays(H, W, K, c2w_staticcam)
        viewdirs = viewdirs / torch.norm(viewdirs, dim=-1, keepdim=True)
        viewdirs = torch.reshape(viewdirs, [-1, 3]).float()

    sh = rays_d.shape  # [..., 3]
    if ndc:
        # for forward facing scenes
        rays_o, rays_d = ndc_rays(H, W, K[0][0], 1.0, rays_o, rays_d)

    # Create ray batch
    rays_o = torch.reshape(rays_o, [-1, 3]).float()
    rays_d = torch.reshape(rays_d, [-1, 3]).float()

    near, far = near * torch.ones_like(rays_d[..., :1]), far * torch.ones_like(
        rays_d[..., :1]
    )
    rays = torch.cat([rays_o, rays_d, near, far], -1)
    if use_viewdirs:
        rays = torch.cat([rays, viewdirs], -1)

    # Render and reshape
    all_ret = batchify_rays(rays, chunk, **kwargs)
    for k in all_ret:
        k_sh = list(sh[:-1]) + list(all_ret[k].shape[1:])
        all_ret[k] = torch.reshape(all_ret[k], k_sh)

    k_extract = ["rgb_map", "disp_map", "acc_map", "depth_map"]
    ret_list = [all_ret[k] for k in k_extract]
    ret_dict = {k: all_ret[k] for k in all_ret if k not in k_extract}
    return ret_list + [ret_dict]


def normalize_negative_one(img):
    normalized_input = (img - np.amin(img)) / (np.amax(img) - np.amin(img))
    return 2 * normalized_input - 1


def render_path(
    render_poses,
    hwf,
    K,
    chunk,
    render_kwargs,
    gt_imgs=None,
    savedir=None,
    render_factor=0,
    mask=[],
):
    H, W, fx, fy = hwf
    if render_factor != 0:
        # Render downsampled for speed
        H = H // render_factor
        W = W // render_factor
        fx = fx / render_factor
        fy = fy / render_factor

    rgbs = []
    disps = []
    depths = []

    psnrs_scores = []
    psnrs_unmasked_scores = []
    ssims_scores = []
    lpips_scores = []

    t = time.time()
    for i, c2w in enumerate(tqdm(render_poses)):
        print(i, time.time() - t)
        t = time.time()
        rgb, disp, acc, depth, _ = render(
            H, W, K, chunk=chunk, c2w=c2w[:3, :4], **render_kwargs
        )

        if len(mask) != 0:
            # All masks are the same
            first_mask = mask[0]
            rgb_masked = rgb.cpu().numpy() * first_mask

        rgbs.append(rgb.cpu().numpy())
        disps.append(disp.cpu().numpy())
        depth = visualize_depth(depth)
        depths.append(depth.cpu().numpy())

        if i == 0:
            print(rgb.shape, disp.shape, depth.shape)

        if gt_imgs is not None and render_factor == 0:
            gt_img = (
                gt_imgs[i].cpu().numpy() if torch.is_tensor(gt_imgs[i]) else gt_imgs[i]
            )
            rgb_img = rgb_masked if len(mask) else rgb.cpu().numpy()

            p = -10.0 * np.log10(np.mean(np.square(rgb_img - gt_img)))
            
            if len(mask) != 0:
                # remove mask from psnr calculation
                valid_mask = np.repeat(mask[0] != 0, 3, axis=2)
                psnr_unmasked = np.square(rgb_img - gt_img)[valid_mask]
                psnr_unmasked = -10.0 * np.log10(np.mean(psnr_unmasked))
            ssim_score = ssim(rgb_img, gt_img, channel_axis=2, data_range=1.0)

            # LPIPS require images in -1 to 1 range.
            rgb_lpips = lpips.im2tensor(normalize_negative_one(rgb_img))
            gt_lpips = lpips.im2tensor(normalize_negative_one(gt_img))

            rgb_lpips = rgb_lpips.to("cpu")
            gt_lpips = gt_lpips.to("cpu")

            lpips_score = loss_fn_lpips(rgb_lpips, gt_lpips)

            ssims_scores.append(ssim_score)
            psnrs_scores.append(p)
            lpips_scores.append(lpips_score.item())

            print("PSNR: ", p)
            print("SSIM: ", ssim_score)
            print("LPIPS: ", lpips_score.item())

            if len(mask) != 0:
                psnrs_unmasked_scores.append(psnr_unmasked)
                print("PSNR UNMASKED: ", psnr_unmasked)

        if savedir is not None:
            rgb8 = to8b(rgbs[-1])
            depth8 = to8b(depths[-1].transpose(1, 2, 0))
            filename_rgb = os.path.join(savedir, "{:03d}.png".format(i))
            filename_depth = os.path.join(savedir, "{:03d}_depth.png".format(i))
            imageio.v3.imwrite(filename_rgb, rgb8)
            imageio.v3.imwrite(filename_depth, depth8)

    if psnrs_unmasked_scores:
        psnr_unmasked_score = round(float(np.mean(psnrs_unmasked_scores)), 2)
        psnr_unmasked_median = round(float(np.median(psnrs_unmasked_scores)), 2)
        psnr_unmasked_max = round(float(np.max(psnrs_unmasked_scores)), 2)
        psnr_unmasked_min = round(float(np.min(psnrs_unmasked_scores)), 2)

    if psnrs_scores and ssims_scores and lpips_scores:
        psnr_score = round(float(np.mean(psnrs_scores)), 2)
        psnr_median = round(float(np.median(psnrs_scores)), 2)
        psnr_max = round(float(np.max(psnrs_scores)), 2)
        psnr_min = round(float(np.min(psnrs_scores)), 2)

        mean_ssim = round(float(np.mean(ssims_scores)), 3)
        median_ssim = round(float(np.median(ssims_scores)), 3)
        max_ssim = round(float(np.max(ssims_scores)), 3)
        min_ssim = round(float(np.min(ssims_scores)), 3)

        mean_lpips = round(float(np.mean(lpips_scores)), 3)
        median_lpips = round(float(np.median(lpips_scores)), 3)
        max_lpips = round(float(np.max(lpips_scores)), 3)
        min_lpips = round(float(np.min(lpips_scores)), 3)

        print(f"Average PSNR is {psnr_score}")
        print(f"Average SSIM is {mean_ssim}")
        print(f"Average LPIPS is {mean_lpips}")

        if savedir is not None:
            results = {
                "psnr_mean": psnr_score,
                "median_psnr": psnr_median,
                "psnr_max": psnr_max,
                "psnr_min": psnr_min,
                "psnr_unmasked_mean": psnr_unmasked_score if psnrs_unmasked_scores else "-",
                "median_psnr_unmasked": psnr_unmasked_median if psnrs_unmasked_scores else "-",
                "psnr_unmasked_max": psnr_unmasked_max if psnrs_unmasked_scores else "-",
                "psnr_unmasked_min": psnr_unmasked_min if psnrs_unmasked_scores else "-",
                "ssim_mean": mean_ssim,
                "median_ssim": median_ssim,
                "ssim_max": max_ssim,
                "ssim_min": min_ssim,
                "lpips_mean": mean_lpips,
                "median_lpips": median_lpips,
                "lpips_max": max_lpips,
                "lpips_min": min_lpips,
            }
            json_object = json.dumps(results, indent=4)
            with open(os.path.join(savedir, "results.json"), "w") as file:
                file.write(json_object)

    rgbs = np.stack(rgbs, 0)
    disps = np.stack(disps, 0)
    depths = np.stack(depths, 0)

    return rgbs, disps, depths


def create_nerf(args):
    """Instantiate NeRF's MLP model."""
    embed_fn, input_ch = get_embedder(args.multires, args.i_embed)

    input_ch_views = 0
    embeddirs_fn = None
    if args.use_viewdirs:
        embeddirs_fn, input_ch_views = get_embedder(args.multires_views, args.i_embed)
    output_ch = 5 if args.N_importance > 0 else 4
    skips = [4]
    model = NeRF(
        D=args.netdepth,
        W=args.netwidth,
        input_ch=input_ch,
        output_ch=output_ch,
        skips=skips,
        input_ch_views=input_ch_views,
        use_viewdirs=args.use_viewdirs,
    ).to(device)
    grad_vars = list(model.parameters())

    model_parameters = filter(lambda p: p.requires_grad, model.parameters())
    coarse_params = sum([np.prod(p.size()) for p in model_parameters])
    print("Trainable parameters COARSE: ", coarse_params)
    model_fine = None
    fine_params = 0
    if args.N_importance > 0:
        model_fine = NeRF(
            D=args.netdepth_fine,
            W=args.netwidth_fine,
            input_ch=input_ch,
            output_ch=output_ch,
            skips=skips,
            input_ch_views=input_ch_views,
            use_viewdirs=args.use_viewdirs,
        ).to(device)
        grad_vars += list(model_fine.parameters())

        model_parameters = filter(lambda p: p.requires_grad, model_fine.parameters())
        fine_params = sum([np.prod(p.size()) for p in model_parameters])
        print("Trainable parameters FINE: ", fine_params)


    print('Total trainable parameters: ', coarse_params + fine_params)
    network_query_fn = lambda inputs, viewdirs, network_fn: run_network(
        inputs,
        viewdirs,
        network_fn,
        embed_fn=embed_fn,
        embeddirs_fn=embeddirs_fn,
        netchunk=args.netchunk,
    )

    # Create optimizer
    optimizer = torch.optim.Adam(params=grad_vars, lr=args.lrate, betas=(0.9, 0.999))

    start = 0
    basedir = args.basedir
    expname = args.expname

    ##########################

    # Load checkpoints
    if args.ft_path is not None and args.ft_path != "None":
        ckpts = [args.ft_path]
    else:
        ckpts = [
            os.path.join(basedir, expname, f)
            for f in sorted(os.listdir(os.path.join(basedir, expname)))
            if "tar" in f
        ]

    print("Found ckpts", ckpts)
    if len(ckpts) > 0 and not args.no_reload:
        ckpt_path = ckpts[-1]
        print("Reloading from", ckpt_path)
        ckpt = torch.load(ckpt_path)

        start = ckpt["global_step"]
        optimizer.load_state_dict(ckpt["optimizer_state_dict"])

        # Load model
        model.load_state_dict(ckpt["network_fn_state_dict"])
        if model_fine is not None:
            model_fine.load_state_dict(ckpt["network_fine_state_dict"])

    ##########################

    render_kwargs_train = {
        "network_query_fn": network_query_fn,
        "perturb": args.perturb,
        "N_importance": args.N_importance,
        "network_fine": model_fine,
        "N_samples": args.N_samples,
        "network_fn": model,
        "use_viewdirs": args.use_viewdirs,
        "white_bkgd": args.white_bkgd,
        "raw_noise_std": args.raw_noise_std,
    }

    # NDC only good for LLFF-style forward facing data
    if args.dataset_type != "llff" or args.no_ndc:
        print("Not ndc!")
        render_kwargs_train["ndc"] = False
        render_kwargs_train["lindisp"] = args.lindisp

    render_kwargs_test = {k: render_kwargs_train[k] for k in render_kwargs_train}
    render_kwargs_test["perturb"] = False
    render_kwargs_test["raw_noise_std"] = 0.0

    return render_kwargs_train, render_kwargs_test, start, grad_vars, optimizer


def raw2outputs(raw, z_vals, rays_d, raw_noise_std=0, white_bkgd=False, pytest=False):
    """Transforms model's predictions to semantically meaningful values.
    Args:
        raw: [num_rays, num_samples along ray, 4]. Prediction from model.
        z_vals: [num_rays, num_samples along ray]. Integration time.
        rays_d: [num_rays, 3]. Direction of each ray.
    Returns:
        rgb_map: [num_rays, 3]. Estimated RGB color of a ray.
        disp_map: [num_rays]. Disparity map. Inverse of depth map.
        acc_map: [num_rays]. Sum of weights along each ray.
        weights: [num_rays, num_samples]. Weights assigned to each sampled color.
        depth_map: [num_rays]. Estimated distance to object.
    """
    raw2alpha = lambda raw, dists, act_fn=F.relu: 1.0 - torch.exp(-act_fn(raw) * dists)

    dists = z_vals[..., 1:] - z_vals[..., :-1]
    dists = torch.cat(
        [dists, torch.Tensor([1e10]).expand(dists[..., :1].shape)], -1
    )  # [N_rays, N_samples]

    dists = dists * torch.norm(rays_d[..., None, :], dim=-1)

    rgb = torch.sigmoid(raw[..., :3])  # [N_rays, N_samples, 3]
    noise = 0.0
    if raw_noise_std > 0.0:
        noise = torch.randn(raw[..., 3].shape) * raw_noise_std

        # Overwrite randomly sampled data if pytest
        if pytest:
            np.random.seed(0)
            noise = np.random.rand(*list(raw[..., 3].shape)) * raw_noise_std
            noise = torch.Tensor(noise)

    alpha = raw2alpha(raw[..., 3] + noise, dists)  # [N_rays, N_samples]
    # weights = alpha * tf.math.cumprod(1.-alpha + 1e-10, -1, exclusive=True)
    weights = (
        alpha
        * torch.cumprod(
            torch.cat([torch.ones((alpha.shape[0], 1)), 1.0 - alpha + 1e-10], -1), -1
        )[:, :-1]
    )
    rgb_map = torch.sum(weights[..., None] * rgb, -2)  # [N_rays, 3]

    depth_map = torch.sum(weights * z_vals, -1)
    disp_map = 1.0 / torch.max(
        1e-10 * torch.ones_like(depth_map), depth_map / torch.sum(weights, -1)
    )
    acc_map = torch.sum(weights, -1)

    if white_bkgd:
        rgb_map = rgb_map + (1.0 - acc_map[..., None])

    return rgb_map, disp_map, acc_map, weights, depth_map


def render_rays(
    ray_batch,
    network_fn,
    network_query_fn,
    N_samples,
    retraw=False,
    lindisp=False,
    perturb=0.0,
    N_importance=0,
    network_fine=None,
    white_bkgd=False,
    raw_noise_std=0.0,
    verbose=False,
    pytest=False,
):
    """Volumetric rendering.
    Args:
      ray_batch: array of shape [batch_size, ...]. All information necessary
        for sampling along a ray, including: ray origin, ray direction, min
        dist, max dist, and unit-magnitude viewing direction.
      network_fn: function. Model for predicting RGB and density at each point
        in space.
      network_query_fn: function used for passing queries to network_fn.
      N_samples: int. Number of different times to sample along each ray.
      retraw: bool. If True, include model's raw, unprocessed predictions.
      lindisp: bool. If True, sample linearly in inverse depth rather than in depth.
      perturb: float, 0 or 1. If non-zero, each ray is sampled at stratified
        random points in time.
      N_importance: int. Number of additional times to sample along each ray.
        These samples are only passed to network_fine.
      network_fine: "fine" network with same spec as network_fn.
      white_bkgd: bool. If True, assume a white background.
      raw_noise_std: ...
      verbose: bool. If True, print more debugging info.
    Returns:
      rgb_map: [num_rays, 3]. Estimated RGB color of a ray. Comes from fine model.
      disp_map: [num_rays]. Disparity map. 1 / depth.
      acc_map: [num_rays]. Accumulated opacity along each ray. Comes from fine model.
      raw: [num_rays, num_samples, 4]. Raw predictions from model.
      rgb0: See rgb_map. Output for coarse model.
      disp0: See disp_map. Output for coarse model.
      acc0: See acc_map. Output for coarse model.
      z_std: [num_rays]. Standard deviation of distances along ray for each
        sample.
    """
    N_rays = ray_batch.shape[0]
    rays_o, rays_d = ray_batch[:, 0:3], ray_batch[:, 3:6]  # [N_rays, 3] each
    viewdirs = ray_batch[:, -3:] if ray_batch.shape[-1] > 8 else None
    bounds = torch.reshape(ray_batch[..., 6:8], [-1, 1, 2])
    near, far = bounds[..., 0], bounds[..., 1]  # [-1,1]

    t_vals = torch.linspace(0.0, 1.0, steps=N_samples)
    if not lindisp:
        z_vals = near * (1.0 - t_vals) + far * (t_vals)
    else:
        z_vals = 1.0 / (1.0 / near * (1.0 - t_vals) + 1.0 / far * (t_vals))

    z_vals = z_vals.expand([N_rays, N_samples])

    if perturb > 0.0:
        # get intervals between samples
        mids = 0.5 * (z_vals[..., 1:] + z_vals[..., :-1])
        upper = torch.cat([mids, z_vals[..., -1:]], -1)
        lower = torch.cat([z_vals[..., :1], mids], -1)
        # stratified samples in those intervals
        t_rand = torch.rand(z_vals.shape)

        # Pytest, overwrite u with numpy's fixed random numbers
        if pytest:
            np.random.seed(0)
            t_rand = np.random.rand(*list(z_vals.shape))
            t_rand = torch.Tensor(t_rand)

        z_vals = lower + (upper - lower) * t_rand

    pts = (
        rays_o[..., None, :] + rays_d[..., None, :] * z_vals[..., :, None]
    )  # [N_rays, N_samples, 3]

    #     raw = run_network(pts)
    raw = network_query_fn(pts, viewdirs, network_fn)
    rgb_map, disp_map, acc_map, weights, depth_map = raw2outputs(
        raw, z_vals, rays_d, raw_noise_std, white_bkgd, pytest=pytest
    )

    if N_importance > 0:
        rgb_map_0, disp_map_0, acc_map_0, depth_map_0 = rgb_map, disp_map, acc_map, depth_map

        z_vals_mid = 0.5 * (z_vals[..., 1:] + z_vals[..., :-1])
        z_samples = sample_pdf(
            z_vals_mid,
            weights[..., 1:-1],
            N_importance,
            det=(perturb == 0.0),
            pytest=pytest,
        )
        z_samples = z_samples.detach()

        z_vals, _ = torch.sort(torch.cat([z_vals, z_samples], -1), -1)
        pts = (
            rays_o[..., None, :] + rays_d[..., None, :] * z_vals[..., :, None]
        )  # [N_rays, N_samples + N_importance, 3]

        run_fn = network_fn if network_fine is None else network_fine
        #         raw = run_network(pts, fn=run_fn)
        raw = network_query_fn(pts, viewdirs, run_fn)

        rgb_map, disp_map, acc_map, weights, depth_map = raw2outputs(
            raw, z_vals, rays_d, raw_noise_std, white_bkgd, pytest=pytest
        )

    ret = {"rgb_map": rgb_map, "disp_map": disp_map, "acc_map": acc_map, "depth_map": depth_map}
    if retraw:
        ret["raw"] = raw
    if N_importance > 0:
        ret["rgb0"] = rgb_map_0
        ret["disp0"] = disp_map_0
        ret["acc0"] = acc_map_0
        ret["depth0"] = depth_map_0
        ret["z_std"] = torch.std(z_samples, dim=-1, unbiased=False)  # [N_rays]

    for k in ret:
        if (torch.isnan(ret[k]).any() or torch.isinf(ret[k]).any()) and DEBUG:
            print(f"! [Numerical Error] {k} contains nan or inf.")

    return ret


def config_parser():
    import configargparse

    parser = configargparse.ArgumentParser()
    parser.add_argument("--config", is_config_file=True, help="config file path")
    parser.add_argument("--expname", type=str, help="experiment name")
    parser.add_argument(
        "--basedir", type=str, default="./logs/", help="where to store ckpts and logs"
    )
    parser.add_argument(
        "--datadir", type=str, default="./data/llff/fern", help="input data directory"
    )
    parser.add_argument("--testdir", type=str, default="", help="input test directory")
    parser.add_argument("--maskdir", type=str, default="", help="mask directory")

    # training options
    parser.add_argument("--netdepth", type=int, default=8, help="layers in network")
    parser.add_argument("--netwidth", type=int, default=256, help="channels per layer")
    parser.add_argument(
        "--netdepth_fine", type=int, default=8, help="layers in fine network"
    )
    parser.add_argument(
        "--netwidth_fine",
        type=int,
        default=256,
        help="channels per layer in fine network",
    )
    parser.add_argument(
        "--N_rand",
        type=int,
        default=32 * 32 * 4,
        help="batch size (number of random rays per gradient step)",
    )
    parser.add_argument("--lrate", type=float, default=5e-4, help="learning rate")
    parser.add_argument(
        "--lrate_decay",
        type=int,
        default=250,
        help="exponential learning rate decay (in 1000 steps)",
    )
    parser.add_argument(
        "--chunk",
        type=int,
        default=1024 * 32,
        help="number of rays processed in parallel, decrease if running out of memory",
    )
    parser.add_argument(
        "--netchunk",
        type=int,
        default=1024 * 64,
        help="number of pts sent through network in parallel, decrease if running out of memory",
    )
    parser.add_argument(
        "--no_batching",
        action="store_true",
        help="only take random rays from 1 image at a time",
    )
    parser.add_argument(
        "--no_reload", action="store_true", help="do not reload weights from saved ckpt"
    )
    parser.add_argument(
        "--ft_path",
        type=str,
        default=None,
        help="specific weights npy file to reload for coarse network",
    )

    # rendering options
    parser.add_argument(
        "--N_samples", type=int, default=64, help="number of coarse samples per ray"
    )
    parser.add_argument(
        "--N_importance",
        type=int,
        default=0,
        help="number of additional fine samples per ray",
    )
    parser.add_argument(
        "--perturb",
        type=float,
        default=1.0,
        help="set to 0. for no jitter, 1. for jitter",
    )
    parser.add_argument(
        "--use_viewdirs", action="store_true", help="use full 5D input instead of 3D"
    )
    parser.add_argument(
        "--i_embed",
        type=int,
        default=0,
        help="set 0 for default positional encoding, -1 for none",
    )
    parser.add_argument(
        "--multires",
        type=int,
        default=10,
        help="log2 of max freq for positional encoding (3D location)",
    )
    parser.add_argument(
        "--multires_views",
        type=int,
        default=4,
        help="log2 of max freq for positional encoding (2D direction)",
    )
    parser.add_argument(
        "--raw_noise_std",
        type=float,
        default=0.0,
        help="std dev of noise added to regularize sigma_a output, 1e0 recommended",
    )

    parser.add_argument(
        "--render_only",
        action="store_true",
        help="do not optimize, reload weights and render out render_poses path",
    )
    parser.add_argument(
        "--render_test",
        action="store_true",
        help="render the test set instead of render_poses path",
    )
    parser.add_argument(
        "--render_factor",
        type=int,
        default=0,
        help="downsampling factor to speed up rendering, set 4 or 8 for fast preview",
    )
    parser.add_argument(
        "--render_spiral", action="store_true", help="render a spiral video"
    )

    # training options
    parser.add_argument(
        "--precrop_iters",
        type=int,
        default=0,
        help="number of steps to train on central crops",
    )
    parser.add_argument(
        "--precrop_frac",
        type=float,
        default=0.5,
        help="fraction of img taken for central crops",
    )

    # dataset options
    parser.add_argument(
        "--dataset_type",
        type=str,
        default="llff",
        help="options: llff / blender / deepvoxels",
    )
    parser.add_argument(
        "--testskip",
        type=int,
        default=8,
        help="will load 1/N images from test/val sets, useful for large datasets like deepvoxels",
    )

    ## deepvoxels flags
    parser.add_argument(
        "--shape",
        type=str,
        default="greek",
        help="options : armchair / cube / greek / vase",
    )

    ## blender flags
    parser.add_argument(
        "--white_bkgd",
        action="store_true",
        help="set to render synthetic data on a white bkgd (always use for dvoxels)",
    )
    parser.add_argument(
        "--half_res",
        action="store_true",
        help="load blender synthetic data at 400x400 instead of 800x800",
    )

    ## llff flags
    parser.add_argument(
        "--factor", type=int, default=8, help="downsample factor for LLFF images"
    )
    parser.add_argument(
        "--no_ndc",
        action="store_true",
        help="do not use normalized device coordinates (set for non-forward facing scenes)",
    )
    parser.add_argument(
        "--lindisp",
        action="store_true",
        help="sampling linearly in disparity rather than depth",
    )
    parser.add_argument(
        "--spherify", action="store_true", help="set for spherical 360 scenes"
    )
    parser.add_argument(
        "--llffhold",
        type=int,
        default=8,
        help="will take every 1/N images as LLFF test set, paper uses 8",
    )

    # logging/saving options
    parser.add_argument(
        "--i_print",
        type=int,
        default=100,
        help="frequency of console printout and metric loggin",
    )
    parser.add_argument(
        "--i_img", type=int, default=500, help="frequency of tensorboard image logging"
    )
    parser.add_argument(
        "--i_weights", type=int, default=10000, help="frequency of weight ckpt saving"
    )
    parser.add_argument(
        "--i_testset", type=int, default=50000, help="frequency of testset saving"
    )
    parser.add_argument(
        "--i_video",
        type=int,
        default=50000,
        help="frequency of render_poses video saving",
    )

    return parser


def train():
    parser = config_parser()
    args = parser.parse_args()

    # Load data
    K = None
    if args.dataset_type == "llff":
        # --render_path --testdir should test model on given testset and not split, and not train model.
        if args.testdir:
            images, mask, poses, bds, render_poses, i_test, i_val = load_llff_data(
                args.testdir,
                args.factor,
                recenter=True,
                bd_factor=0.75,
                spherify=args.spherify,
                test=True,
                render_spiral=args.render_spiral,
                maskdir=args.maskdir,
                exp_name=args.expname,
            )
        else:
            images, mask, poses, bds, render_poses, i_test, i_val = load_llff_data(
                args.datadir,
                args.factor,
                recenter=True,
                bd_factor=0.75,
                spherify=args.spherify,
                render_spiral=args.render_spiral,
                maskdir=args.maskdir,
                exp_name=args.expname,
            )
        # OPENCV camera model, hwf = [H, W, fx, fy]
        if poses.shape[1] == 4:
            hwf = poses[0, :4, -1]
        # other camera model, hwf = [H, W, focal]
        else:
            hwf = poses[0, :3, -1]
        poses = poses[:, :3, :4]

        mask_exists = len(mask) != 0

        if args.testdir:
            print("Loaded llff", images.shape, render_poses.shape, hwf, args.testdir)
        else:
            print("Loaded llff", images.shape, render_poses.shape, hwf, args.datadir)

        # If testdir is passed then i_test is empty and should only run on the passed images. No splitting of dataset.
        if not isinstance(i_test, list):
            i_test = [i_test]

        if i_test:
            if args.llffhold > 0:
                print("Auto LLFF holdout,", args.llffhold)
                i_test = np.arange(images.shape[0])[:: args.llffhold]

            # i_val = i_test
            i_train = np.array(
                [
                    i
                    for i in np.arange(int(images.shape[0]))
                    if (i not in i_test and i not in i_val)
                ]
            )
        else:
            i_train = np.array([i for i in np.arange(int(images.shape[0]))])

        print("DEFINING BOUNDS")
        if args.no_ndc:
            near = np.ndarray.min(bds) * 0.9
            far = np.ndarray.max(bds) * 1.0

        else:
            near = 0.0
            far = 1.0
        print("NEAR FAR", near, far)

    elif args.dataset_type == "blender":
        images, poses, render_poses, hwf, i_split = load_blender_data(
            args.datadir, args.half_res, args.testskip
        )
        print("Loaded blender", images.shape, render_poses.shape, hwf, args.datadir)
        i_train, i_val, i_test = i_split

        near = 2.0
        far = 6.0

        if args.white_bkgd:
            images = images[..., :3] * images[..., -1:] + (1.0 - images[..., -1:])
        else:
            images = images[..., :3]

    elif args.dataset_type == "LINEMOD":
        images, poses, render_poses, hwf, K, i_split, near, far = load_LINEMOD_data(
            args.datadir, args.half_res, args.testskip
        )
        print(f"Loaded LINEMOD, images shape: {images.shape}, hwf: {hwf}, K: {K}")
        print(f"[CHECK HERE] near: {near}, far: {far}.")
        i_train, i_val, i_test = i_split

        if args.white_bkgd:
            images = images[..., :3] * images[..., -1:] + (1.0 - images[..., -1:])
        else:
            images = images[..., :3]

    elif args.dataset_type == "deepvoxels":
        images, poses, render_poses, hwf, i_split = load_dv_data(
            scene=args.shape, basedir=args.datadir, testskip=args.testskip
        )

        print("Loaded deepvoxels", images.shape, render_poses.shape, hwf, args.datadir)
        i_train, i_val, i_test = i_split

        hemi_R = np.mean(np.linalg.norm(poses[:, :3, -1], axis=-1))
        near = hemi_R - 1.0
        far = hemi_R + 1.0

    else:
        print("Unknown dataset type", args.dataset_type, "exiting")
        return

    # Cast intrinsics to right types

    # OPENCV camera model, fx != fy
    if len(hwf) == 4:
        H, W, fx, fy = hwf
    # other camera model fx = fy
    else:
        H, W, focal = hwf
        fx, fy = focal, focal

    H, W = int(H), int(W)
    hwf = [H, W, fx, fy]

    if K is None:
        K = np.array([[fx, 0, 0.5 * W], [0, fy, 0.5 * H], [0, 0, 1]])
    # other camera model fx = fy

    if args.render_test and i_test:
        render_poses = np.array(poses[i_test])
    else:
        render_poses = np.array(poses[i_train])

    # Create log dir and copy the config file
    basedir = args.basedir
    expname = args.expname
    os.makedirs(os.path.join(basedir, expname), exist_ok=True)
    f = os.path.join(basedir, expname, "args.txt")

    with open(f, "w") as file:
        for arg in sorted(vars(args)):
            attr = getattr(args, arg)
            file.write("{} = {}\n".format(arg, attr))

    if args.config is not None:
        f = os.path.join(basedir, expname, "config.txt")
        with open(f, "w") as file:
            file.write(open(args.config, "r").read())

    # Create nerf model
    render_kwargs_train, render_kwargs_test, start, grad_vars, optimizer = create_nerf(
        args
    )
    global_step = start

    bds_dict = {
        "near": near,
        "far": far,
    }
    render_kwargs_train.update(bds_dict)
    render_kwargs_test.update(bds_dict)

    # Move testing data to GPU
    render_poses = torch.Tensor(render_poses).to(device)

    # Short circuit if only rendering out from trained model
    if args.render_only:
        print("RENDER ONLY")
        with torch.no_grad():
            if args.render_test and i_test:
                # render_test switches to test poses
                images = images[i_test]
            # else:
            #     # Default is smoother render_poses path
            #     images = None
            if args.testdir:
                testsavedir = os.path.join(
                    args.testdir,
                    expname,
                    "renderonly_{}_{:06d}".format(
                        "test" if args.render_test else "path", start
                    ),
                )
            else:
                testsavedir = os.path.join(
                    basedir,
                    expname,
                    "renderonly_{}_{:06d}".format(
                        "test" if args.render_test else "path", start
                    ),
                )
            os.makedirs(testsavedir, exist_ok=True)
            print("test poses shape", render_poses.shape)

            rgbs, _, depths = render_path(
                render_poses,
                hwf,
                K,
                args.chunk,
                render_kwargs_test,
                gt_imgs=images,
                savedir=testsavedir,
                render_factor=args.render_factor,
                mask=mask,
            )
            print("Done rendering", testsavedir)
            imageio.v3.imwrite(
                os.path.join(testsavedir, "video.mp4"), to8b(rgbs), fps=30, quality=8
            )
            imageio.v3.imwrite(
                os.path.join(testsavedir, "video_depth.mp4"), to8b(depths.transpose(0,2,3,1)), fps=30, quality=8
            )

            return

    # Prepare raybatch tensor if batching random rays
    N_rand = args.N_rand
    use_batching = not args.no_batching
    if use_batching:
        # For random ray batching
        print("get rays")
        rays = np.stack(
            [get_rays_np(H, W, K, p) for p in poses[:, :3, :4]], 0
        )  # [N, ro+rd, H, W, 3]
        print("done, concats")

        if mask_exists:
            expanded_mask = np.repeat(mask[:, None], 3, axis=4)
            rays_rgb = np.concatenate(
                [rays, images[:, None], expanded_mask], 1
            )  # [N, ro+rd+rgb+mask, H, W, 3]
        else:
            rays_rgb = np.concatenate(
                [rays, images[:, None]], 1
            )  # [N, ro+rd+rgb, H, W, 3]
        rays_rgb = np.transpose(
            rays_rgb, [0, 2, 3, 1, 4]
        )  # [N, H, W, ro+rd+rgb/ro+rd+rgb+mask, 3]
        rays_rgb = np.stack([rays_rgb[i] for i in i_train], 0)  # train images only
        rays_rgb = (
            np.reshape(rays_rgb, [-1, 4, 3])
            if mask_exists
            else np.reshape(rays_rgb, [-1, 3, 3])
        )  # [(N-1)*H*W, ro+rd+rgb/ro+rd+rgb+mask, 3]
        rays_rgb = rays_rgb.astype(np.float32)
        print("shuffle rays")
        np.random.shuffle(rays_rgb)

        print("done")
        i_batch = 0

    # Move training data to GPU
    if use_batching:
        images = torch.Tensor(images).to(device)
    poses = torch.Tensor(poses).to(device)
    if use_batching:
        rays_rgb = torch.Tensor(rays_rgb).to(device)

    N_iters = 200000 + 1
    print("Begin")
    print("TRAIN views are", i_train)
    print("TEST views are", i_test)
    print("VAL views are", i_val)

    # Summary writers

    # get last version number and increment
    version_num = "0"
    os.makedirs(os.path.join(basedir,expname, "summaries"), exist_ok=True)
    if os.path.isdir(os.path.join(basedir, expname, "summaries")):
        version_list = sorted(
            [
                int(item.split("_")[-1])
                for item in os.listdir(os.path.join(basedir, expname, "summaries"))
                if os.path.isdir(os.path.join(basedir, expname, "summaries", item))
            ]
        )
        version_num = str(version_list[-1] + 1) if version_list else "0"

    log_dir = os.path.join(basedir, expname, "summaries", f"{expname}_{version_num}")
    writer = SummaryWriter(log_dir)
    print(f"Saving logs to {log_dir}")

    start = start + 1
    for i in trange(start, N_iters):
        time0 = time.time()

        # Sample random ray batch
        if use_batching:
            # Random over all images
            batch = rays_rgb[i_batch : i_batch + N_rand]  # [B, 2+1, 3*?]
            batch = torch.transpose(batch, 0, 1)
            if mask_exists:
                batch_rays, target_s, batch_mask = batch[:2], batch[2], batch[3]
            else:
                batch_rays, target_s = batch[:2], batch[2]

            i_batch += N_rand
            if i_batch >= rays_rgb.shape[0]:
                print("Shuffle data after an epoch!")
                rand_idx = torch.randperm(rays_rgb.shape[0])
                rays_rgb = rays_rgb[rand_idx]
                i_batch = 0

        else:
            # Random from one image
            img_i = np.random.choice(i_train)
            target = images[img_i]
            target = torch.Tensor(target).to(device)
            pose = poses[img_i, :3, :4]

            if N_rand is not None:
                rays_o, rays_d = get_rays(
                    H, W, K, torch.Tensor(pose)
                )  # (H, W, 3), (H, W, 3)

                if i < args.precrop_iters:
                    dH = int(H // 2 * args.precrop_frac)
                    dW = int(W // 2 * args.precrop_frac)
                    coords = torch.stack(
                        torch.meshgrid(
                            torch.linspace(H // 2 - dH, H // 2 + dH - 1, 2 * dH),
                            torch.linspace(W // 2 - dW, W // 2 + dW - 1, 2 * dW),
                        ),
                        -1,
                    )
                    if i == start:
                        print(
                            f"[Config] Center cropping of size {2*dH} x {2*dW} is enabled until iter {args.precrop_iters}"
                        )
                else:
                    coords = torch.stack(
                        torch.meshgrid(
                            torch.linspace(0, H - 1, H), torch.linspace(0, W - 1, W)
                        ),
                        -1,
                    )  # (H, W, 2)

                coords = torch.reshape(coords, [-1, 2])  # (H * W, 2)
                select_inds = np.random.choice(
                    coords.shape[0], size=[N_rand], replace=False
                )  # (N_rand,)
                select_coords = coords[select_inds].long()  # (N_rand, 2)
                rays_o = rays_o[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
                rays_d = rays_d[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
                batch_rays = torch.stack([rays_o, rays_d], 0)
                target_s = target[
                    select_coords[:, 0], select_coords[:, 1]
                ]  # (N_rand, 3)

        #####  Core optimization loop  #####
        rgb, disp, acc, depth, extras = render(
            H,
            W,
            K,
            chunk=args.chunk,
            rays=batch_rays,
            verbose=i < 10,
            retraw=True,
            **render_kwargs_train,
        )

        if mask_exists:
            rgb = rgb * batch_mask

        optimizer.zero_grad()
        img_loss = img2mse(rgb, target_s)

        trans = extras["raw"][..., -1]
        loss = img_loss
        psnr = mse2psnr(img_loss)

        if "rgb0" in extras:
            img_loss0 = (
                img2mse(extras["rgb0"] * batch_mask, target_s)
                if mask_exists
                else img2mse(extras["rgb0"], target_s)
            )
            loss = loss + img_loss0
            psnr0 = mse2psnr(img_loss0)

        if mask_exists:
            img_loss_unmasked = img2mse(rgb, target_s, batch_mask != 0)
            psnr_unmasked = mse2psnr(img_loss_unmasked)

            if "rgb0" in extras:
                img_loss0_unmasked = (
                    img2mse(extras["rgb0"] * batch_mask, target_s, batch_mask != 0)
                    if mask_exists
                    else img2mse(extras["rgb0"], target_s, batch_mask != 0)
                )
                psnr0_unmasked = mse2psnr(img_loss0_unmasked)
        loss.backward()
        optimizer.step()

        # NOTE: IMPORTANT!
        ###   update learning rate   ###
        decay_rate = 0.1
        decay_steps = args.lrate_decay * 1000
        new_lrate = args.lrate * (decay_rate ** (global_step / decay_steps))
        for param_group in optimizer.param_groups:
            param_group["lr"] = new_lrate
        ################################

        dt = time.time() - time0
        # Rest is logging
        if i % args.i_weights == 0:
            path = os.path.join(basedir, expname, "{:06d}.tar".format(i))
            torch.save(
                {
                    "global_step": global_step,
                    "network_fn_state_dict": render_kwargs_train[
                        "network_fn"
                    ].state_dict(),
                    "network_fine_state_dict": render_kwargs_train[
                        "network_fine"
                    ].state_dict(),
                    "optimizer_state_dict": optimizer.state_dict(),
                },
                path,
            )
            print("Saved checkpoints at", path)

        if i % args.i_video == 0 and i > 0:
            # Turn on testing mode
            mask = mask if mask_exists else []
            with torch.no_grad():
                rgbs, disps, depths = render_path(
                    render_poses, hwf, K, args.chunk, render_kwargs_test, mask=mask
                )
            print("Done, saving", rgbs.shape, disps.shape, depths.shape)
            movie_name = (
                "{}_spiral_{:06d}_".format(expname, i)
                if args.render_spiral
                else f"{expname}_{i:06d}_"
            )

            moviebase = os.path.join(basedir, expname, movie_name)
            imageio.v2.mimwrite(moviebase + "rgb.mp4", to8b(rgbs), fps=30, quality=8)
            imageio.v2.mimwrite(moviebase + "depth.mp4", to8b(depths.transpose(0,2,3,1)), fps=30, quality=8)
            imageio.v2.mimwrite(
                moviebase + "disp.mp4", to8b(disps / np.max(disps)), fps=30, quality=8
            )
            print(f"Saved {moviebase}rgb.mp4 and {moviebase}disp.mp4.")

        if i_test and i % args.i_testset == 0 and i > 0:
            testsavedir = os.path.join(basedir, expname, "testset_{:06d}".format(i))
            os.makedirs(testsavedir, exist_ok=True)
            mask = mask if mask_exists else []

            print("test poses shape", poses[i_test].shape)
            with torch.no_grad():
                render_path(
                    torch.Tensor(poses[i_test]).to(device),
                    hwf,
                    K,
                    args.chunk,
                    render_kwargs_test,
                    gt_imgs=images[i_test],
                    savedir=testsavedir,
                    mask=mask,
                )
            print("Saved test set")

        if i % args.i_print == 0:
            if args.N_importance > 0:
                tqdm.write(
                    f"[TRAIN] Iter: {i} Loss: {loss.item()}  PSNR: {psnr.item()} PSNR_COARSE: {psnr0.item()} " + 
                      f"PSNR_UNMASKED: {psnr_unmasked.item() if mask_exists else '-'} PSNR_COARSE_UNMASKED: {psnr0_unmasked.item() if mask_exists else '-'}"
                )
            else:
                tqdm.write(
                    f"[TRAIN] Iter: {i} Loss: {loss.item()}  PSNR: {psnr.item()} PSNR_UNMASKED: {psnr_unmasked.item() if mask_exists else '-'}"
                )

            writer.add_scalar("train/loss", loss.item(), i)
            writer.add_scalar("train/psnr", psnr.item(), i)

            if mask_exists:
                writer.add_scalar("train/psnr_unmasked", psnr_unmasked.item(), i)

            # histogram of raw predictions from model
            # writer.add_histogram("train/trans", trans, i)

            if args.N_importance > 0:
                writer.add_scalar("train/psnr_coarse", psnr0.item(), i)
                if mask_exists:
                    writer.add_scalar(
                        "train/psnr_coarse_unmasked", psnr0_unmasked.item(), i
                    )

            if i % args.i_img == 0:
                # Log a rendered validation view to Tensorboard
                img_i = np.random.choice(i_val)
                target = images[img_i]
                pose = poses[img_i, :3, :4]

                with torch.no_grad():
                    # disp, acc doesn't seem to be working correctly
                    # disregard them
                    rgb, _, _, depth, extras = render(
                        H, W, K, chunk=args.chunk, c2w=pose, **render_kwargs_test
                    )

                # add depth to tensorboard
                depth = visualize_depth(depth)
                writer.add_images("val/depth", torch.stack([depth]),i)
                if mask_exists:
                    rgb_masked = rgb * torch.tensor(mask[0])
                    psnr_val = mse2psnr(img2mse(rgb_masked, target))

                    # Add stack of gt, masked rendered image, rendered image
                    stack = torch.stack(
                        [
                            target.permute(2, 0, 1),
                            rgb_masked.permute(2, 0, 1),
                            rgb.permute(2, 0, 1),
                        ]
                    )  # (3,3,H,W)

                    writer.add_images("val/gt_rgb(masked)_rgb", stack, i)
                    if args.N_importance > 0:
                        rgb0_masked = extras["rgb0"] * torch.tensor(mask[0])
                        psnr0_val = mse2psnr(img2mse(rgb0_masked, target))

                        # Add stack of fine network rendered image
                        stack = torch.stack(
                            [
                                rgb0_masked.permute(2, 0, 1),
                                extras["rgb0"].permute(2, 0, 1),
                            ]
                        )
                        writer.add_images("val/rgb_coarse(masked)_rgb_coarse", stack, i)
                else:
                    psnr_val = mse2psnr(img2mse(rgb, target))

                    # Add stack of gt, rendered image
                    stack = torch.stack(
                        [target.permute(2, 0, 1), rgb.permute(2, 0, 1)]
                    )  # (2,3,H,W)
                    writer.add_images("val/gt_rgb", stack, i)

                    if args.N_importance > 0:
                        psnr0_val = mse2psnr(img2mse(extras["rgb0"], target))

                # stack = torch.stack([disp[None,:], acc[None, :]])
                # writer.add_images("val/fine/disp_acc", stack, i)
                writer.add_scalar("val/psnr", psnr_val.item(), i)

                if args.N_importance > 0:
                    # stack = torch.stack(
                    #     [
                    #         extras["disp0"][None, :],
                    #         extras["acc0"][None, :],
                    #         extras["z_std"][None, :],
                    #     ]
                    # )
                    writer.add_image("val/z_std", extras["z_std"][None, :], i)
                    writer.add_scalar("val/psnr_coarse", psnr0_val.item(), i)

        global_step += 1
    writer.close()


if __name__ == "__main__":
    torch.set_default_tensor_type("torch.cuda.FloatTensor")

    train()
