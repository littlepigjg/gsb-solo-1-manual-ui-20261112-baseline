"""Bounded diagnostics for the inspector: histograms, palette and quality."""
import math
import numpy as np
from PIL import Image
from .imaging import convolution


PERCENTILE_LEVELS = (1, 5, 25, 50, 75, 95, 99)


class AnalysisService:
    def __init__(self, assets):
        self.assets = assets

    def inspect(self, asset_id):
        image, record = self.assets.load(asset_id)
        original_size = image.size
        image.thumbnail((768, 768), Image.Resampling.LANCZOS)
        rgba = np.asarray(image, dtype=np.uint8)
        alpha = rgba[..., 3]
        visible = alpha > 0
        analyzed_pixels = int(visible.sum())
        transparent_percent = round(float((alpha < 255).mean()) * 100, 2)

        # Every statistic below is derived from this single pixel set, so the
        # histogram, colour metrics and suggestions can never disagree.
        pixels = rgba[..., :3][visible]
        gray = np.asarray(image.convert("L"), dtype=np.float32)
        gray_visible = gray[visible]

        empty_histogram = [0] * 256
        if not analyzed_pixels:
            return self._empty_report(
                asset_id, original_size, image.size, transparent_percent
            )

        histograms = {}
        for index, channel in enumerate(("red", "green", "blue")):
            histograms[channel] = np.bincount(
                pixels[:, index], minlength=256
            ).tolist()
        luma_histogram = np.bincount(
            gray_visible.astype(np.uint8), minlength=256
        )
        histograms["luminance"] = luma_histogram.tolist()
        occupied = luma_histogram > 0
        probabilities = luma_histogram[occupied] / luma_histogram.sum()
        entropy = float(-(probabilities * np.log2(probabilities)).sum()) or 0.0

        mean_rgb = pixels.mean(axis=0)
        std_rgb = pixels.std(axis=0)
        brightness = float(gray_visible.mean())
        contrast = float(gray_visible.std())
        percentiles = np.percentile(gray_visible, PERCENTILE_LEVELS)

        # Neutral fill keeps transparent regions from injecting edges; the
        # variance is then taken over the same visible pixels as the rest.
        filled = gray.copy()
        filled[~visible] = brightness
        laplacian = convolution(filled, [[0, 1, 0], [1, -4, 1], [0, 1, 0]])
        laplacian_variance = float(laplacian[visible].var())

        palette = self._palette(pixels, analyzed_pixels)
        suggestions = self._suggestions(
            gray_visible, contrast, entropy, laplacian_variance
        )

        divisor = math.gcd(*original_size)
        return {
            "asset_id": asset_id,
            "width": original_size[0],
            "height": original_size[1],
            "aspect_ratio": f"{original_size[0] // divisor}:{original_size[1] // divisor}",
            "megapixels": round(original_size[0] * original_size[1] / 1_000_000, 2),
            "sample_size": list(image.size),
            "analyzed_pixels": analyzed_pixels,
            "histograms": histograms,
            "mean_rgb": [round(float(value), 2) for value in mean_rgb],
            "std_rgb": [round(float(value), 2) for value in std_rgb],
            "brightness": round(brightness, 2),
            "contrast": round(contrast, 2),
            "entropy_bits": round(entropy, 3),
            "laplacian_variance": round(laplacian_variance or 0.0, 2),
            "percentiles": {
                str(level): round(float(value), 2)
                for level, value in zip(PERCENTILE_LEVELS, percentiles)
            },
            "dark_clipping_percent": round(float((gray_visible <= 3).mean()) * 100, 2),
            "light_clipping_percent": round(float((gray_visible >= 252).mean()) * 100, 2),
            "transparent_percent": transparent_percent,
            "palette": palette,
            "suggestions": suggestions,
        }

    def _empty_report(self, asset_id, original_size, sample_size, transparent_percent):
        divisor = math.gcd(*original_size)
        return {
            "asset_id": asset_id,
            "width": original_size[0],
            "height": original_size[1],
            "aspect_ratio": f"{original_size[0] // divisor}:{original_size[1] // divisor}",
            "megapixels": round(original_size[0] * original_size[1] / 1_000_000, 2),
            "sample_size": list(sample_size),
            "analyzed_pixels": 0,
            "histograms": {
                "red": [0] * 256,
                "green": [0] * 256,
                "blue": [0] * 256,
                "luminance": [0] * 256,
            },
            "mean_rgb": None,
            "std_rgb": None,
            "brightness": None,
            "contrast": None,
            "entropy_bits": None,
            "laplacian_variance": None,
            "percentiles": {str(level): None for level in PERCENTILE_LEVELS},
            "dark_clipping_percent": None,
            "light_clipping_percent": None,
            "transparent_percent": transparent_percent,
            "palette": [],
            "suggestions": [{
                "message": "素材完全透明，没有可参与分析的像素；统计与色谱暂不可用。",
                "operation": None,
            }],
        }

    def _palette(self, pixels, analyzed_pixels):
        # Quantize only the visible pixels so transparent areas never claim a
        # share of the palette; dithering is disabled to avoid inventing colors.
        strip = Image.fromarray(pixels.reshape(1, -1, 3), mode="RGB")
        palette_image = strip.quantize(
            colors=min(6, analyzed_pixels),
            method=Image.Quantize.MEDIANCUT,
            dither=Image.Dither.NONE,
        )
        palette = palette_image.getpalette()
        colors = sorted(palette_image.getcolors() or [], reverse=True)
        dominant = []
        for count, index in colors:
            channels = palette[index * 3:index * 3 + 3]
            dominant.append({
                "hex": "#" + "".join(f"{value:02x}" for value in channels),
                "rgb": channels,
                "percentage": round(count / analyzed_pixels * 100, 1),
            })
        return dominant

    def _suggestions(self, gray_visible, contrast, entropy, laplacian_variance):
        # A single-value image makes every variation-based rule fire at once;
        # report that fact explicitly instead of stacking redundant advice.
        if contrast == 0.0 or entropy == 0.0:
            return [{
                "message": "画面由单一纯色构成，没有明暗或细节变化；可尝试渐变、叠加或艺术滤镜增加层次。",
                "operation": "filter_vintage",
            }]
        suggestions = []
        brightness = float(gray_visible.mean())
        if brightness < 65:
            suggestions.append({"message": "画面偏暗，可尝试曝光补偿或阴影提亮。", "operation": "color_exposure"})
        elif brightness > 195:
            suggestions.append({"message": "画面偏亮，可适当降低曝光。", "operation": "color_exposure"})
        if contrast < 35:
            suggestions.append({"message": "明暗范围较窄，可尝试自动对比度。", "operation": "color_autocontrast"})
        if laplacian_variance < 100:
            suggestions.append({"message": "细节变化较少，可尝试精细锐化；此指标不代表美学质量。", "operation": "enhance_unsharp"})
        if not suggestions:
            suggestions.append({"message": "亮度与反差较均衡，可以从风格滤镜开始探索。", "operation": "filter_vintage"})
        return suggestions

    def compare(self, first_id, second_id):
        first, first_record = self.assets.load(first_id)
        second, second_record = self.assets.load(second_id)
        original_equal = first.size == second.size
        first.thumbnail((768, 768), Image.Resampling.LANCZOS)
        second = second.resize(first.size, Image.Resampling.LANCZOS)
        a = np.asarray(first.convert("RGB"), dtype=np.float32)
        b = np.asarray(second.convert("RGB"), dtype=np.float32)
        difference = a - b
        mse = float(np.mean(difference ** 2))
        mae = float(np.mean(np.abs(difference)))
        psnr = None if mse == 0 else 10 * math.log10(255 ** 2 / mse)
        return {
            "first_id": first_id,
            "second_id": second_id,
            "same_dimensions": original_equal,
            "sample_size": list(first.size),
            "mean_absolute_error": round(mae, 4),
            "mean_squared_error": round(mse, 4),
            "psnr_db": round(psnr, 4) if psnr is not None else None,
            "identical_sample": mse == 0,
            "note": "指标在缩略采样上计算；尺寸不同则先对齐，不用于无损验证。",
        }
