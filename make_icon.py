#!/usr/bin/env python3
"""Generate AppIcon.icns for Koala To ALS."""
import sys, os, struct, zlib, io

def make_png(size):
    """Create a PNG of given size with the app design."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Rounded dark background
        r = size // 6
        bg = (22, 22, 28, 255)
        draw.rounded_rectangle([0, 0, size-1, size-1], radius=r, fill=bg)

        # Koala emoji approximation - dark grey circle (head)
        cx, cy = size//2, size//2 - size//16
        head_r = int(size * 0.32)
        draw.ellipse([cx-head_r, cy-head_r, cx+head_r, cy+head_r],
                     fill=(80, 80, 90, 255))

        # Ears
        ear_r = int(size * 0.14)
        ear_y = cy - head_r + ear_r//2
        for ex in [cx - head_r + ear_r//2, cx + head_r - ear_r//2]:
            draw.ellipse([ex-ear_r, ear_y-ear_r, ex+ear_r, ear_y+ear_r],
                         fill=(80, 80, 90, 255))
            inner_r = int(ear_r * 0.55)
            draw.ellipse([ex-inner_r, ear_y-inner_r, ex+inner_r, ear_y+inner_r],
                         fill=(160, 130, 140, 255))

        # Face
        face_r = int(head_r * 0.75)
        draw.ellipse([cx-face_r, cy-face_r+size//12, cx+face_r, cy+face_r+size//12],
                     fill=(110, 105, 118, 255))

        # Nose
        nose_r = int(size * 0.09)
        draw.ellipse([cx-nose_r, cy-nose_r//2, cx+nose_r, cy+nose_r+nose_r//2],
                     fill=(40, 38, 50, 255))

        # Eyes
        eye_r = int(size * 0.04)
        eye_y = cy - size//18
        for ex in [cx - size//8, cx + size//8]:
            draw.ellipse([ex-eye_r, eye_y-eye_r, ex+eye_r, eye_y+eye_r], fill=(20,20,25,255))
            draw.ellipse([ex-eye_r//2, eye_y-eye_r//2, ex, eye_y], fill=(255,255,255,200))

        buf = io.BytesIO()
        img.save(buf, 'PNG')
        return buf.getvalue()
    except ImportError:
        return make_minimal_png(size)

def make_minimal_png(size):
    sig = b'\x89PNG\r\n\x1a\n'
    def chunk(t, d):
        c = t + d
        return struct.pack('>I', len(d)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    ihdr = chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0))
    rows = []
    for y in range(size):
        row = [0]
        for x in range(size):
            cx, cy = size//2, size//2
            dist = ((x-cx)**2 + (y-cy)**2)**0.5
            if dist < size*0.42:
                row.extend([60, 58, 70])
            else:
                row.extend([22, 22, 28])
        rows.append(bytes(row))
    idat = chunk(b'IDAT', zlib.compress(b''.join(rows)))
    return sig + ihdr + idat + chunk(b'IEND', b'')

def make_icns(out_dir):
    sizes = [16, 32, 64, 128, 256, 512, 1024]
    type_map = {16:'icp4', 32:'icp5', 64:'icp6', 128:'ic07',
                256:'ic08', 512:'ic09', 1024:'ic10'}
    magic = b'icns'
    icons = b''
    for s in sizes:
        png = make_png(s)
        t = type_map[s].encode()
        icons += t + struct.pack('>I', len(png)+8) + png
    total = 8 + len(icons)
    icns = magic + struct.pack('>I', total) + icons
    path = os.path.join(out_dir, 'AppIcon.icns')
    with open(path, 'wb') as f:
        f.write(icns)
    print(f'Icon written: {path} ({len(icns)} bytes)')

if __name__ == '__main__':
    out = sys.argv[1] if len(sys.argv) > 1 else '.'
    make_icns(out)
