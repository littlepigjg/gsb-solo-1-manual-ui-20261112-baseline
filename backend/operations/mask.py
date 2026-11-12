"""Mask-weighted compositing that keeps uncovered pixels and alpha intact."""
import numpy as np
from PIL import Image
from ..errors import StudioError


def mask_local_adjust(image, p, context):
    if context.masks is None or context.registry is None:
        raise StudioError("蒙版服务未启用", code="mask_unavailable", status=500)
    coverage = context.masks.coverage(
        p["mask_id"],
        image.size,
        p["mode"],
        p["invert"],
        p["feather"],
    )
    weight = coverage * p["strength"]
    if not np.any(weight):
        return image.copy()
    adjusted = context.registry.apply(image, p["adjustment"], context)
    if adjusted.size != image.size:
        raise StudioError("局部调整不支持改变图像尺寸的操作")
    source = np.asarray(image.convert("RGBA"), dtype=np.float32)
    target = np.asarray(adjusted.convert("RGBA"), dtype=np.float32)
    factor = weight[..., None]
    output = source + (target - source) * factor
    output[..., 3] = source[..., 3]
    return Image.fromarray(np.clip(output, 0, 255).astype(np.uint8), "RGBA")
