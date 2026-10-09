"""Bounded image validation/conversion in a short-lived worker process."""
import asyncio
import sys
from pathlib import Path
from .diagnostics import MediaFailure

async def prepare_image(path: Path, *, sticker: bool = False, native: bool = False) -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'shmax.media.image', str(path), 'native' if native else ('sticker' if sticker else 'photo'),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        code = await asyncio.wait_for(proc.wait(), 8)
    except asyncio.CancelledError:
        proc.kill(); await proc.wait()
        raise
    except asyncio.TimeoutError:
        proc.kill(); await proc.wait()
        raise MediaFailure('media.conversion', 'conversion_timeout')
    if code:
        raise MediaFailure('media.validation', 'invalid_image')

if __name__ == '__main__':
    import resource
    from PIL import Image
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    path = Path(sys.argv[1]); sticker = sys.argv[2] in {'sticker', 'native'}
    if path.stat().st_size > (5 if sticker else 20) * 1024 * 1024:
        sys.exit(1)
    Image.MAX_IMAGE_PIXELS = 4_000_000 if sticker else 25_000_000
    with Image.open(path, formats=['JPEG','PNG','WEBP']) as image:
        if image.format not in {'JPEG', 'PNG', 'WEBP'} or image.width * image.height > Image.MAX_IMAGE_PIXELS:
            sys.exit(1)
        image.verify()
    if sticker:
        with Image.open(path, formats=['JPEG','PNG','WEBP']) as image:
            image.seek(0)
            converted = image.convert('RGBA')
            if sys.argv[2] == 'native':
                ratio = 512 / max(converted.size)
                converted = converted.resize((max(1,round(converted.width * ratio)),max(1,round(converted.height * ratio))))
            converted.save(path, format='PNG')
