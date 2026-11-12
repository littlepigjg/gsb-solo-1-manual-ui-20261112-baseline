"""Smoothing, noise reduction and detail enhancement."""
import numpy as np
from PIL import Image, ImageFilter, ImageEnhance, ImageOps
from ..imaging import rgb_array, from_array, with_alpha, blend_effect, convolution


def enhance_gaussian(image, p, context):
    rgb = image.convert("RGB").filter(ImageFilter.GaussianBlur(p["radius"]))
    return with_alpha(rgb, image)


def enhance_box(image, p, context):
    rgb = image.convert("RGB").filter(ImageFilter.BoxBlur(p["radius"]))
    return with_alpha(rgb, image)


def enhance_median(image, p, context):
    result = image.convert("RGB").filter(ImageFilter.MedianFilter(p["size"]))
    return with_alpha(result, image)


def enhance_unsharp(image, p, context):
    result = image.convert("RGB").filter(ImageFilter.UnsharpMask(
        radius=p["radius"], percent=p["percent"], threshold=p["threshold"]))
    return with_alpha(result, image)


def enhance_sharpness(image, p, context):
    result = ImageEnhance.Sharpness(image.convert("RGB")).enhance(p["factor"])
    return with_alpha(result, image)


def enhance_detail(image, p, context):
    result = image.convert("RGB").filter(ImageFilter.DETAIL)
    return blend_effect(image, with_alpha(result, image), p["strength"])


def enhance_bilateral(image, p, context):
    array = rgb_array(image)
    radius = p["radius"]
    height, width = array.shape[:2]
    color_sigma = p["sigma"]
    spatial_sigma = max(radius / 2, 1)
    output = np.empty_like(array)
    # Precompute the spatial kernel once; the same (dx, dy) weight is used in
    # every band so nothing depends on how the image is split for processing.
    offsets = range(-radius, radius + 1)
    spatial_kernel = np.exp(
        -np.array([[dx * dx + dy * dy for dx in offsets] for dy in offsets],
                  dtype=np.float32)
        / (2 * spatial_sigma ** 2)
    )
    # Clamped row/column indices give every pixel the same edge-extended
    # neighborhood it would have in a whole-image pass, including 1-row /
    # 1-column images where the same row or column is repeated at the border.
    column_ids = np.clip(np.arange(-radius, width + radius), 0, width - 1).astype(np.intp)
    # Process in horizontal bands only to bound peak memory. Each band gets a
    # padded window whose core starts at row/column ``radius``; the halo is
    # read through the clamped indices from the whole image rather than from
    # the band itself, so a band is never an independent tile: pixels at band
    # boundaries see exactly the same neighbors as interior ones and no
    # fixed-pitch seam can appear.
    band_height = 64
    for top in range(0, height, band_height):
        bottom = min(top + band_height, height)
        core_height = bottom - top
        row_ids = np.clip(
            np.arange(top - radius, bottom + radius), 0, height - 1
        ).astype(np.intp)
        padded = array[row_ids][:, column_ids]
        stripe = array[top:bottom]
        accum = np.zeros_like(stripe)
        weights = np.zeros(stripe.shape[:2], dtype=np.float32)
        for index, dy in enumerate(offsets):
            row_start = radius + dy
            for jndex, dx in enumerate(offsets):
                neighbor = padded[row_start:row_start + core_height,
                                  radius + dx:radius + dx + width]
                # One scalar weight per pixel, derived from the combined color
                # distance of all channels. Sharing it across channels keeps
                # the channel mix of each neighbor intact instead of letting
                # every channel blend a different neighbor set (color fringing).
                color_distance = ((neighbor - stripe) ** 2).mean(axis=2)
                weight = (spatial_kernel[index, jndex]
                          * np.exp(-color_distance / (2 * color_sigma ** 2)))
                accum += neighbor * weight[..., np.newaxis]
                weights += weight
        output[top:bottom] = accum / np.maximum(weights, 1e-8)[..., np.newaxis]
    return from_array(output, image)


def enhance_motion(image, p, context):
    size = p["size"]
    kernel = np.zeros((size, size), dtype=np.float32)
    if p["direction"] == "horizontal":
        kernel[size // 2, :] = 1
    elif p["direction"] == "vertical":
        kernel[:, size // 2] = 1
    else:
        np.fill_diagonal(kernel, 1)
    kernel /= kernel.sum()
    return from_array(convolution(rgb_array(image), kernel), image)


def enhance_clarity(image, p, context):
    array = rgb_array(image)
    blurred = image.convert("RGB").filter(ImageFilter.GaussianBlur(p["radius"]))
    low_frequency = np.asarray(blurred, dtype=np.float32)
    difference = array - low_frequency
    result = array + difference * p["amount"]
    return from_array(result, image)


def enhance_soft_glow(image, p, context):
    array = rgb_array(image) / 255
    blurred = image.convert("RGB").filter(ImageFilter.GaussianBlur(p["radius"]))
    glow = np.asarray(blurred, dtype=np.float32) / 255
    screened = 1 - (1 - array) * (1 - glow)
    result = array * (1 - p["strength"]) + screened * p["strength"]
    return from_array(result * 255, image)


def enhance_minimum(image, p, context):
    result = image.convert("RGB").filter(ImageFilter.MinFilter(p["size"]))
    return with_alpha(result, image)


def enhance_maximum(image, p, context):
    result = image.convert("RGB").filter(ImageFilter.MaxFilter(p["size"]))
    return with_alpha(result, image)

