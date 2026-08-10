import pytest

from axion_wizard import images


def test_all_pinned_images_pass_assert_image_is_pinned() -> None:
    for image in images.ALL_PINNED_IMAGES:
        images.assert_image_is_pinned(image)  # no debe lanzar


def test_assert_image_is_pinned_rejects_latest() -> None:
    with pytest.raises(images.UnpinnedImageError):
        images.assert_image_is_pinned("ghcr.io/wg-easy/wg-easy:latest")


def test_assert_image_is_pinned_rejects_missing_tag() -> None:
    with pytest.raises(images.UnpinnedImageError):
        images.assert_image_is_pinned("nginx")


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("nginx:1.27-alpine", ("nginx", "1.27-alpine")),
        ("ghcr.io/wg-easy/wg-easy:14", ("ghcr.io/wg-easy/wg-easy", "14")),
        ("nginx", ("nginx", None)),
        # el `:` pertenece al puerto del registro, no a una tag
        ("localhost:5000/wg-easy", ("localhost:5000/wg-easy", None)),
        ("localhost:5000/wg-easy:14", ("localhost:5000/wg-easy", "14")),
        ("registry.example.com:443/team/img", ("registry.example.com:443/team/img", None)),
    ],
)
def test_split_image_tag(image: str, expected: tuple[str, str | None]) -> None:
    assert images.split_image_tag(image) == expected


def test_assert_image_is_pinned_rejects_untagged_from_port_qualified_registry() -> None:
    """Regresión: partir por el último `:` tomaba el puerto del registro como
    si fuera la tag, así que una imagen realmente sin fijar pasaba el guard
    de §6.4 sin protestar."""
    with pytest.raises(images.UnpinnedImageError):
        images.assert_image_is_pinned("localhost:5000/wg-easy")


def test_assert_image_is_pinned_accepts_tagged_from_port_qualified_registry() -> None:
    images.assert_image_is_pinned("localhost:5000/wg-easy:14")  # no debe lanzar


def test_assert_image_is_pinned_rejects_latest_from_port_qualified_registry() -> None:
    with pytest.raises(images.UnpinnedImageError):
        images.assert_image_is_pinned("localhost:5000/wg-easy:latest")


@pytest.mark.parametrize(
    ("tag", "expected_major"),
    [("14", 14), ("14.2", 14), ("v14.0.1", 14), ("15", 15), ("v15.3", 15)],
)
def test_parse_wg_easy_major_version(tag: str, expected_major: int) -> None:
    assert images.parse_wg_easy_major_version(tag) == expected_major


def test_parse_wg_easy_major_version_unparseable() -> None:
    assert images.parse_wg_easy_major_version("edge") is None


def test_assert_wg_easy_tag_is_safe_accepts_v14() -> None:
    images.assert_wg_easy_tag_is_safe("14")  # no debe lanzar


def test_assert_wg_easy_tag_is_safe_rejects_v15() -> None:
    with pytest.raises(images.UnsafeWgEasyTagError, match="v15"):
        images.assert_wg_easy_tag_is_safe("15")


def test_assert_wg_easy_tag_is_safe_rejects_latest() -> None:
    with pytest.raises(images.UnsafeWgEasyTagError, match="latest"):
        images.assert_wg_easy_tag_is_safe("latest")


def test_assert_wg_easy_tag_is_safe_rejects_unparseable() -> None:
    with pytest.raises(images.UnsafeWgEasyTagError):
        images.assert_wg_easy_tag_is_safe("edge")
