from PIL import Image
import numpy as np
import sys

def jpg_to_rgb(image_path):
    img = Image.open(image_path).convert("RGB")

    pixels = np.array(img, dtype=np.float64)

    # Calculate average RGB values (0–255)
    avg_rgb = pixels.mean(axis=(0, 1))

    return {
        "R": round(avg_rgb[0], 2),
        "G": round(avg_rgb[1], 2),
        "B": round(avg_rgb[2], 2)
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python imageTest.py image0.jpg")
        sys.exit(1)

    image_path = sys.argv[1]

    try:
        result = jpg_to_rgb(image_path)
        print(result)
    except FileNotFoundError:
        print(f"Error: File '{image_path}' not found.")
    except Exception as e:
        print(f"Error: {e}")