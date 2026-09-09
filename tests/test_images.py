import io

from PIL import Image

from rephemeral.images import prepare
from rephemeral.screens import Screen

SCREEN = Screen(key='test', filename='test.png', label='Test', description='', width=2, height=2)


def test_rgb_color_key_transparency_is_composited():
    raw = io.BytesIO()
    Image.new('RGB', (2, 2), 'red').save(raw, format='PNG', transparency=(255, 0, 0))
    result = prepare(raw.getvalue(), SCREEN)
    with Image.open(io.BytesIO(result.data)) as image:
        assert image.getpixel((0, 0)) == (255, 255, 255)


def test_grayscale_uses_one_channel():
    raw = io.BytesIO()
    Image.new('RGB', (2, 2), 'red').save(raw, format='PNG')
    result = prepare(raw.getvalue(), SCREEN, grayscale=True)
    assert result.mode == 'L'
    with Image.open(io.BytesIO(result.data)) as image:
        assert image.mode == 'L'
        assert image.size == (2, 2)
