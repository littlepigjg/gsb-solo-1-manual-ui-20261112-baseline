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
    output = np.empty_like(array)
    spatial_sigma = max(radius / 2, 1)
    for top in range(0, image.height, 32):
        stripe = array[top:top + 32]
        padded = np.pad(stripe, ((radius, radius), (radius, radius), (0, 0)),
                        mode="reflect" if min(stripe.shape[:2]) > 1 else "edge")
        accum = np.zeros_like(stripe)
        weights = np.zeros_like(stripe)
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                neighbor = padded[radius + dy:radius + dy + len(stripe),
                                  radius + dx:radius + dx + image.width]
                distance = (neighbor - stripe) ** 2
                spatial = np.exp(-(dx * dx + dy * dy) / (2 * spatial_sigma ** 2))
                weight = spatial * np.exp(-distance / (2 * p["sigma"] ** 2))
                accum += neighbor * weight
                weights += weight
        output[top:top + len(stripe)] = accum / np.maximum(weights, 1e-8)
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

