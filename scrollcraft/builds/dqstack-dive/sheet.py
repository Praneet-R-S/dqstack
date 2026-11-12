import sys, os
from PIL import Image, ImageDraw
src, out, cols, tw = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
files = sorted(f for f in os.listdir(src) if f.endswith(".jpg"))
start, end = (int(sys.argv[5]), int(sys.argv[6])) if len(sys.argv) > 6 else (0, len(files))
files = files[start:end]
ims = [Image.open(os.path.join(src, f)) for f in files]
th = round(tw * ims[0].height / ims[0].width)
rows = (len(ims) + cols - 1) // cols
sheet = Image.new("RGB", (cols * tw, rows * (th + 18)), (30, 30, 30))
d = ImageDraw.Draw(sheet)
for i, (f, im) in enumerate(zip(files, ims)):
    x, y = (i % cols) * tw, (i // cols) * (th + 18)
    sheet.paste(im.resize((tw, th)), (x, y + 18)); d.text((x + 4, y + 3), f, fill=(255, 255, 0))
sheet.save(out, quality=85)
