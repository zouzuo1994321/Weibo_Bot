import os
import numpy as np
from psd_tools import PSDImage
from PIL import Image

ROOT = r"E:\【01】自研软件\【26-11】微博bot小助手（正式版）"
PSD = os.path.join(ROOT, "logo-mix-2.psd")
UI = os.path.join(ROOT, "_ui_v119k")

psd = PSDImage.open(PSD)

print("LAYERS:")
for l in psd.descendants():
    print("  ", repr(l.name), l.bbox)

def find_layer(sub):
    for l in psd.descendants():
        if l.name and sub in l.name:
            return l
    return None

eye_layer = find_layer("眼珠") or find_layer("眼球")
assert eye_layer is not None, "未找到眼珠图层"
print("EYE LAYER:", repr(eye_layer.name), eye_layer.bbox)

THRESH = 235  # 主瞳孔 alpha>=250；幽灵半透明斑 <=225，235 干净切分

# 底版 = 除眼珠外的所有图层（含白色眼白）
base = psd.composite(layer_filter=lambda l: l is not eye_layer)
if base.mode != "RGBA":
    base = base.convert("RGBA")
print("BASE size:", base.size)

# 眼珠图层
eye = eye_layer.composite()
if eye.mode != "RGBA":
    eye = eye.convert("RGBA")
print("EYE raw size:", eye.size)

# 若 composite 返回的是裁切后的 bbox 尺寸，则贴回 PSD 坐标；否则已是画布尺寸
eb = eye_layer.bbox  # (left, top, right, bottom)
eye_full = Image.new("RGBA", base.size, (0, 0, 0, 0))
if eye.size == base.size:
    eye_full.paste(eye, (0, 0))
else:
    eye_full.paste(eye, (eb[0], eb[1]))
eye = eye_full

ea = np.array(eye)
alpha = ea[:, :, 3]
hist = {}
for v in alpha.flatten().tolist():
    hist[int(v)] = hist.get(int(v), 0) + 1
print("EYE alpha hist:", dict(sorted(hist.items())))

# 去除幽灵：alpha < 阈值 清零
mask = alpha < THRESH
ea[mask, 3] = 0
eye_clean = Image.fromarray(ea, "RGBA")
print("EYE clean opaque px:", int((ea[:, :, 3] >= 250).sum()))

# logo.png（完整版）= 底版 + 干净眼珠
logo = base.copy()
logo.paste(eye_clean, (0, 0), eye_clean)

base.save(os.path.join(UI, "logo-base.png"))
eye_clean.save(os.path.join(UI, "logo-eye.png"))
logo.save(os.path.join(UI, "logo.png"))

# ico（多尺寸）
sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
frames = [logo.resize(s, Image.LANCZOS) for s in sizes]
logo.save(os.path.join(UI, "logo.ico"), sizes=[(s[0], s[1]) for s in sizes])
logo.save(os.path.join(ROOT, "logo.ico"), sizes=[(s[0], s[1]) for s in sizes])
logo.save(os.path.join(ROOT, "logo.png"))
print("DONE export_clean_logo")
