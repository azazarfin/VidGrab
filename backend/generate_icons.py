import os
from PIL import Image, ImageDraw

def create_icon(size):
    # Create an image with transparent background
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # Draw a rounded rectangle with a nice gradient-like background
    # Since Pillow doesn't do complex gradients easily, we'll draw a solid dark purple circle
    # representing the VidGrab theme, with a glowing border.
    margin = max(1, size // 16)
    r = size - margin * 2
    
    # Outer glow
    draw.ellipse([margin, margin, margin + r, margin + r], fill=(108, 92, 231, 255))
    
    # Inner circle
    inner_margin = margin + max(1, size // 12)
    inner_r = size - inner_margin * 2
    draw.ellipse([inner_margin, inner_margin, inner_margin + inner_r, inner_margin + inner_r], fill=(22, 22, 34, 255))
    
    # Draw play symbol (triangle) pointing right
    # Coordinate calculations
    center_x = size // 2
    center_y = size // 2
    
    # Stylized download arrow
    # Stem
    stem_width = max(2, size // 8)
    stem_height = max(4, size // 3)
    draw.rectangle([
        center_x - stem_width // 2, 
        center_y - stem_height // 2 - max(1, size // 12),
        center_x + stem_width // 2,
        center_y + stem_height // 2 - max(1, size // 12)
    ], fill=(162, 155, 254, 255))
    
    # Arrowhead
    arrow_w = max(6, size // 3)
    arrow_h = max(4, size // 5)
    draw.polygon([
        (center_x - arrow_w, center_y + stem_height // 2 - max(1, size // 12)),
        (center_x + arrow_w, center_y + stem_height // 2 - max(1, size // 12)),
        (center_x, center_y + stem_height // 2 + arrow_h - max(1, size // 12))
    ], fill=(162, 155, 254, 255))
    
    # Bottom bracket (tray)
    tray_w = max(8, size // 2)
    tray_h = max(2, size // 10)
    tray_y = center_y + stem_height // 2 + arrow_h
    draw.line([
        (center_x - tray_w // 2, tray_y),
        (center_x + tray_w // 2, tray_y)
    ], fill=(0, 210, 211, 255), width=max(1, size // 16))
    
    # Draw left and right vertical edges of the tray
    tick_h = max(2, size // 12)
    draw.line([
        (center_x - tray_w // 2, tray_y),
        (center_x - tray_w // 2, tray_y - tick_h)
    ], fill=(0, 210, 211, 255), width=max(1, size // 16))
    draw.line([
        (center_x + tray_w // 2, tray_y),
        (center_x + tray_w // 2, tray_y - tick_h)
    ], fill=(0, 210, 211, 255), width=max(1, size // 16))
    
    return img

def main():
    icons_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../extension/icons"))
    os.makedirs(icons_dir, exist_ok=True)
    
    sizes = [16, 32, 48, 128]
    for size in sizes:
        img = create_icon(size)
        img.save(os.path.join(icons_dir, f"icon{size}.png"))
        print(f"Generated icon{size}.png")

if __name__ == "__main__":
    main()
