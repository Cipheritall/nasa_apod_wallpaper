
import ctypes
import os
import re
from datetime import date
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance


# ============================================================
# CONFIGURATION
# ============================================================

API_URL = "https://science.nasa.gov/wp-json/wp/v2/apod-basic/"
API_KEY = os.getenv("NASA_API_KEY", "DEMO_KEY")

OUT_DIR = Path.home() / "Pictures" / "NASA_APOD"

LEFT_RATIO = 0.60
GAP = 4
BLUR_RADIUS = 22
BACKGROUND_BRIGHTNESS = 0.55

MIN_EXPLANATION_FONT = 16
MAX_EXPLANATION_FONT = 46


# ============================================================
# HTTP SESSION
# ============================================================

for key in (
    "HTTP_PROXY", "HTTPS_PROXY",
    "http_proxy", "https_proxy",
    "ALL_PROXY", "all_proxy",
):
    os.environ.pop(key, None)

session = requests.Session()
session.trust_env = False
session.headers.update({
    "User-Agent": "NASA-APOD-Wallpaper/1.0"
})


# ============================================================
# HTML AND EXPLANATION EXTRACTION
# ============================================================

def clean_title(value):
    return BeautifulSoup(
        str(value or "Astronomy Picture of the Day"),
        "html.parser",
    ).get_text(" ", strip=True)


def html_to_paragraphs(html):
    """
    Extract the actual APOD explanation from HTML.

    Supports:
    - Plain explanation text
    - HTML containing an Explanation: label
    - NASA page HTML containing unrelated headings and navigation
    """
    if not html:
        return []

    soup = BeautifulSoup(str(html), "html.parser")

    # Remove scripts, styles and navigation.
    for tag in soup([
        "script", "style", "noscript", "iframe", "svg",
        "nav", "footer", "header", "form", "button"
    ]):
        tag.decompose()

    # Preserve basic block boundaries before extracting text.
    for tag in soup.find_all("br"):
        tag.replace_with("\n")

    for tag in soup.find_all([
        "p", "div", "section", "article",
        "h1", "h2", "h3", "h4", "blockquote"
    ]):
        tag.insert_before("\n")
        tag.insert_after("\n")

    for tag in soup.find_all("li"):
        tag.insert_before("\n• ")
        tag.insert_after("\n")

    raw = soup.get_text(separator="", strip=False)
    raw = raw.replace("\xa0", " ")

    # Normalize whitespace without losing paragraph boundaries.
    lines = []
    for line in raw.splitlines():
        line = re.sub(r"[ \t\r\f\v]+", " ", line).strip()
        if line:
            lines.append(line)

    # If this is the full NASA webpage, start at the real explanation.
    start = None
    first_line = ""

    for i, line in enumerate(lines):
        match = re.search(
            r"\bExplanation\s*:\s*",
            line,
            re.IGNORECASE,
        )
        if match:
            start = i
            first_line = line[match.end():].strip()
            break

    if start is not None:
        lines = ([first_line] if first_line else []) + lines[start + 1:]

    # Stop before related content and the page footer.
    cleaned = []

    for line in lines:
        if re.search(
            r"Tomorrow['’]s picture\s*:",
            line,
            re.IGNORECASE,
        ):
            break

        if re.match(
            r"^(Archive|Index|Search|"
            r"Discover the cosmos!?|"
            r"Image Credit|Copyright)\b",
            line,
            re.IGNORECASE,
        ):
            continue

        # Skip page title/date repetitions and standalone labels.
        if re.match(
            r"^(Astronomy Picture of the Day|"
            r"Explanation|Copyright)\s*:?\s*$",
            line,
            re.IGNORECASE,
        ):
            continue

        # Remove an inline duplicate label.
        line = re.sub(
            r"^\s*Explanation\s*:\s*",
            "",
            line,
            flags=re.IGNORECASE,
        ).strip()

        if line:
            cleaned.append(line)

    # If the HTML was just a normal explanation without a label,
    # retain its text after removing common boilerplate.
    if start is None:
        joined = " ".join(cleaned)

        if re.search(
            r"Discover the cosmos|Tomorrow['’]s picture|"
            r"\bArchive\b|\bIndex\b|\bSearch\b",
            joined,
            re.IGNORECASE,
        ):
            return []

    return cleaned


# ============================================================
# FETCH APOD
# ============================================================

def fetch_apod():
    response = session.get(
        API_URL,
        params={"api_key": API_KEY},
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()

    if isinstance(data, list):
        if not data:
            raise ValueError("NASA returned an empty APOD response.")
        data = data[0]

    if not isinstance(data, dict):
        raise ValueError("Unexpected NASA API response format.")

    apod_date = data.get("date") or date.today().isoformat()
    title = clean_title(data.get("title"))

    # Prefer dedicated explanation fields.
    paragraphs = []

    for field in ("explanation", "basic_html"):
        value = data.get(field)

        if value:
            paragraphs = html_to_paragraphs(value)
            if paragraphs:
                break

    # Some WordPress responses expose rendered content.
    if not paragraphs:
        content = data.get("content", "")

        if isinstance(content, dict):
            content = content.get("rendered", "")

        if content:
            paragraphs = html_to_paragraphs(content)

    if not paragraphs:
        print("DEBUG: API fields:", list(data.keys()))
        print("DEBUG: explanation:", repr(data.get("explanation", "")[:1500]))
        raise ValueError(
            "Could not extract the APOD explanation. "
            "Inspect the debug output above."
        )

    media_type = data.get("media_type")

    if media_type and media_type != "image":
        raise ValueError(
            f"Today's APOD is not an image: {media_type}. "
            f"Title: {title}"
        )

    image_url = (
        data.get("hdurl")
        or data.get("image_url")
        or data.get("url")
    )

    if not image_url:
        raise ValueError(
            "No image URL found. API fields: "
            + ", ".join(data.keys())
        )

    image_url = urljoin(API_URL, image_url)

    if urlparse(image_url).scheme != "https":
        raise ValueError(f"Unexpected image URL: {image_url}")

    # If the endpoint returned a webpage URL, try extracting an image.
    parsed_path = urlparse(image_url).path.lower()
    extensions = (".jpg", ".jpeg", ".png", ".webp", ".avif")

    if not parsed_path.endswith(extensions):
        content = data.get("basic_html", "")

        if content:
            page = BeautifulSoup(str(content), "html.parser")
            image_tag = page.find("img", src=True)

            if image_tag:
                candidate = urljoin(image_url, image_tag["src"])
                if urlparse(candidate).scheme == "https":
                    image_url = candidate

    return apod_date, title, paragraphs, image_url


# ============================================================
# DOWNLOAD IMAGE
# ============================================================

def download_image(image_url):
    response = session.get(image_url, timeout=90)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()

    if "text/html" in content_type:
        raise ValueError(
            "NASA returned HTML instead of an image. "
            f"Image URL: {image_url}"
        )

    with Image.open(BytesIO(response.content)) as downloaded:
        downloaded.load()
        return downloaded.convert("RGB")


# ============================================================
# SCREEN AND FONTS
# ============================================================

def get_screen_size():
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware()
        return (
            user32.GetSystemMetrics(0),
            user32.GetSystemMetrics(1),
        )
    except Exception:
        return 3840, 2160


TARGET_W, TARGET_H = get_screen_size()
PADDING = max(32, int(TARGET_W * 0.025))


def get_font(size, bold=False):
    candidates = (
        [
            r"C:\Windows\Fonts\segoeuib.ttf",
            r"C:\Windows\Fonts\arialbd.ttf",
        ]
        if bold
        else [
            r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\arial.ttf",
            "DejaVuSans.ttf",
        ]
    )

    for path in candidates:
        try:
            return ImageFont.truetype(path, max(1, size))
        except OSError:
            pass

    return ImageFont.load_default()


# ============================================================
# TEXT WRAPPING AND DYNAMIC FONT FITTING
# ============================================================

def line_height(draw, text, font):
    bbox = draw.textbbox((0, 0), text or "Ag", font=font)
    return bbox[3] - bbox[1]


def wrap_text(draw, text, font, max_width):
    words = text.split()
    lines = []
    current = ""

    for word in words:
        candidate = word if not current else current + " " + word

        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)

        fragment = ""

        for char in word:
            candidate_fragment = fragment + char

            if (
                draw.textbbox(
                    (0, 0), candidate_fragment, font=font
                )[2] <= max_width
            ):
                fragment = candidate_fragment
            else:
                if fragment:
                    lines.append(fragment)
                fragment = char

        current = fragment

    if current:
        lines.append(current)

    return lines


def measure_text(draw, paragraphs, font, width):
    total_height = 0

    for paragraph in paragraphs:
        lines = wrap_text(draw, paragraph, font, width)

        for line in lines:
            total_height += line_height(draw, line, font) + 5

        total_height += 10

    return total_height


def fit_font(draw, paragraphs, width, height):
    """
    Binary search for the largest font that fits the available space.
    """
    low = MIN_EXPLANATION_FONT
    high = MAX_EXPLANATION_FONT
    best = get_font(low)

    while low <= high:
        mid = (low + high) // 2
        font = get_font(mid)
        required = measure_text(draw, paragraphs, font, width)

        if required <= height:
            best = font
            low = mid + 1
        else:
            high = mid - 1

    return best


# ============================================================
# CREATE WALLPAPER
# ============================================================

def make_wallpaper(base_img, title, paragraphs, apod_date):
    base = base_img.convert("RGB")

    # Full-screen blurred background.
    background = base.resize(
        (TARGET_W, TARGET_H),
        Image.Resampling.LANCZOS,
    )
    background = background.filter(
        ImageFilter.GaussianBlur(BLUR_RADIUS)
    )
    background = ImageEnhance.Brightness(
        background
    ).enhance(BACKGROUND_BRIGHTNESS)

    canvas = background.copy()

    # LEFT IMAGE PANEL
    left_w = int(TARGET_W * LEFT_RATIO)

    left_x1 = PADDING
    left_y1 = PADDING
    left_x2 = left_w - GAP
    left_y2 = TARGET_H - PADDING

    image_w = left_x2 - left_x1
    image_h = left_y2 - left_y1

    foreground = base.copy()
    foreground.thumbnail(
        (image_w, image_h),
        Image.Resampling.LANCZOS,
    )

    canvas.paste(
        foreground,
        (
            left_x1 + (image_w - foreground.width) // 2,
            left_y1 + (image_h - foreground.height) // 2,
        ),
    )

    # RIGHT PANEL
    right_x1 = left_w + GAP
    right_y1 = PADDING
    right_x2 = TARGET_W - PADDING
    right_y2 = TARGET_H - PADDING

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    draw.rounded_rectangle(
        (right_x1, right_y1, right_x2, right_y2),
        radius=24,
        fill=(5, 10, 22, 175),
    )

    pad = max(24, int(TARGET_W * 0.012))
    text_x = right_x1 + pad
    text_right = right_x2 - pad
    text_width = text_right - text_x

    y = right_y1 + pad

    # TOP DETAILS
    label_font = get_font(17, bold=True)
    label = "NASA • ASTRONOMY PICTURE OF THE DAY"

    draw.text(
        (text_x, y),
        label,
        font=label_font,
        fill=(130, 190, 255, 255),
    )
    y += line_height(draw, label, label_font) + 8

    title_font = get_font(max(24, int(TARGET_H * 0.027)), bold=True)
    title_lines = wrap_text(draw, title, title_font, text_width)

    while title_font.size > 22 and len(title_lines) > 2:
        title_font = get_font(title_font.size - 1, bold=True)
        title_lines = wrap_text(draw, title, title_font, text_width)

    for line in title_lines[:2]:
        draw.text(
            (text_x, y),
            line,
            font=title_font,
            fill=(255, 255, 255, 255),
        )
        y += line_height(draw, line, title_font) + 2

    # Small metadata.
    meta_font = get_font(13)

    for item in (
        "",
        f"DATE  {apod_date} - {base.width} × {base.height} px",
        ""
    ):
        draw.text(
            (text_x, y + 2),
            item,
            font=meta_font,
            fill=(175, 190, 210, 255),
        )
        y += line_height(draw, item, meta_font) + 3

    y += 8

    draw.line(
        (text_x, y, text_right, y),
        fill=(255, 255, 255, 75),
        width=2,
    )

    # LARGE EXPLANATION BOX BELOW THE DETAILS
    box_x1 = text_x
    box_x2 = text_right
    box_y1 = y + max(14, int(TARGET_H * 0.012))
    box_y2 = right_y2 - pad

    draw.rounded_rectangle(
        (box_x1, box_y1, box_x2, box_y2),
        radius=18,
        fill=(255, 255, 255, 25),
        outline=(255, 255, 255, 60),
        width=1,
    )

    box_pad = max(20, int(TARGET_W * 0.010))
    content_x = box_x1 + box_pad
    content_y = box_y1 + box_pad
    content_width = box_x2 - box_x1 - 2 * box_pad
    content_bottom = box_y2 - box_pad

    heading = "EXPLANATION"
    heading_font = get_font(21, bold=True)

    draw.text(
        (content_x, content_y),
        heading,
        font=heading_font,
        fill=(130, 190, 255, 255),
    )

    content_y += line_height(draw, heading, heading_font) + 16
    available_height = max(40, content_bottom - content_y)

    body_font = fit_font(
        draw,
        paragraphs,
        content_width,
        available_height,
    )

    print(f"Explanation font size: {body_font.size}px")

    # Render explanation paragraphs.
    truncated = False

    for paragraph in paragraphs:
        for line in wrap_text(
            draw, paragraph, body_font, content_width
        ):
            h = line_height(draw, line, body_font)

            if content_y + h > content_bottom:
                truncated = True
                break

            draw.text(
                (content_x, content_y),
                line,
                font=body_font,
                fill=(245, 245, 245, 245),
            )
            content_y += h + 5

        if truncated:
            break

        content_y += 10

    if truncated:
        ellipsis_font = get_font(body_font.size, bold=True)
        ellipsis_h = line_height(draw, "…", ellipsis_font)

        draw.text(
            (content_x, content_bottom - ellipsis_h),
            "…",
            font=ellipsis_font,
            fill=(245, 245, 245, 245),
        )

    return Image.alpha_composite(
        canvas.convert("RGBA"), overlay
    ).convert("RGB")


# ============================================================
# SET WINDOWS WALLPAPER
# ============================================================

def set_wallpaper(path):
    SPI_SETDESKWALLPAPER = 20
    SPIF_UPDATEINIFILE = 0x01
    SPIF_SENDCHANGE = 0x02

    result = ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETDESKWALLPAPER,
        0,
        str(path.resolve()),
        SPIF_UPDATEINIFILE | SPIF_SENDCHANGE,
    )

    if not result:
        raise ctypes.WinError()


# ============================================================
# MAIN
# ============================================================

def main():
    print("Fetching NASA APOD...")

    apod_date, title, paragraphs, image_url = fetch_apod()

    print(f"Date: {apod_date}")
    print(f"Title: {title}")
    print(f"Explanation paragraphs: {len(paragraphs)}")

    image = download_image(image_url)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    wallpaper_path = OUT_DIR / f"apod_{apod_date}.jpg"

    print("Rendering wallpaper...")

    wallpaper = make_wallpaper(
        image,
        title,
        paragraphs,
        apod_date,
    )

    wallpaper.save(
        wallpaper_path,
        format="JPEG",
        quality=95,
        optimize=True,
    )

    print("Applying wallpaper...")
    set_wallpaper(wallpaper_path)

    print(f"Wallpaper updated: {wallpaper_path}")


if __name__ == "__main__":
    main()
