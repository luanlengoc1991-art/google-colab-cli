"""Tạo studio/icon.ico: vòng tròn dải xanh ngọc → xanh lá, hai nửa vòng đen (chữ C đôi)."""
from pathlib import Path
from PIL import Image, ImageDraw

S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
grad = Image.new("RGBA", (S, S))
px = grad.load()
for y in range(S):
    for x in range(S):
        t = (x + y) / (2 * S)
        px[x, y] = (int(21 + (47 - 21) * t), int(164 + (234 - 164) * t), int(207 + (116 - 207) * t), 255)
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).ellipse((8, 8, S - 8, S - 8), fill=255)
img.paste(grad, (0, 0), mask)
d = ImageDraw.Draw(img)
w = 44
d.arc((90, 150, 300, 362), 90, 270, fill=(0, 0, 0, 255), width=w)
d.arc((212, 150, 422, 362), 270, 90, fill=(0, 0, 0, 255), width=w)
out = Path(__file__).with_name("icon.ico")
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.resize((256, 256)).save(Path(__file__).with_name("icon.png"))
print(out)
